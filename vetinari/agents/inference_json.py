"""JSON parsing and retry support for agent inference."""

from __future__ import annotations

import json
import logging
import re
import threading
from typing import Any

from vetinari.exceptions import InferenceError, ModelUnavailableError

logger = logging.getLogger(__name__)


_JSON_EXTRACT_RE = re.compile(r"(\{.*\}|\[.*\])", re.DOTALL)
_MAX_JSON_RETRIES = 3

_json_retry_counts: dict[str, list[int]] = {}
_json_retry_lock = threading.Lock()
_last_infer_json_fallback = False


def _set_last_infer_json_fallback(value: bool) -> None:
    global _last_infer_json_fallback
    with _json_retry_lock:
        _last_infer_json_fallback = value


def was_last_infer_json_fallback() -> bool:
    """Return whether the most recent JSON inference returned its fallback.

    Returns:
        True when the most recent JSON inference returned the configured
        fallback instead of parsed model output.
    """
    with _json_retry_lock:
        return _last_infer_json_fallback


class _JsonInferenceBehavior:
    """JSON inference behavior mixed into ``InferenceBehavior``."""

    def _infer_json(
        self,
        prompt: str,
        system_prompt: str | None = None,
        model_id: str | None = None,
        fallback: Any | None = None,
        **kwargs: Any,
    ) -> Any:
        """Call _infer() and parse the result as JSON, retrying with error feedback.

        On JSON parse failure, feeds the validation error back to the model
        and retries up to ``_MAX_JSON_RETRIES`` total attempts. Tracks retry
        rate per model to identify models poor at structured output.

        Args:
            prompt: The user/task prompt.
            system_prompt: Optional system prompt override.
            model_id: Optional model override.
            fallback: Value to return if LLM output cannot be parsed as JSON.
                If None, returns None on parse failure.
            **kwargs: Additional arguments passed to _infer().

        Returns:
            Parsed JSON (dict or list), or ``fallback`` on failure.
        """
        kwargs.pop("expect_json", None)

        current_prompt = prompt
        last_error = ""
        retries_used = 0
        effective_model = model_id or getattr(self, "_last_model_id", "unknown")

        for attempt in range(_MAX_JSON_RETRIES):
            raw = self._infer_json_raw(current_prompt, system_prompt, model_id, kwargs, effective_model, retries_used)
            if raw is None:
                _set_last_infer_json_fallback(True)
                return fallback

            if not raw:
                if attempt < _MAX_JSON_RETRIES - 1:
                    current_prompt = self._empty_json_retry_prompt(prompt)
                    retries_used += 1
                    continue
                self._record_json_retries(effective_model, retries_used)
                _set_last_infer_json_fallback(True)
                return fallback

            parsed, last_error = self._parse_json_candidate(raw, effective_model, retries_used, attempt)
            if parsed is not None:
                _set_last_infer_json_fallback(False)
                return parsed

            if attempt < _MAX_JSON_RETRIES - 1:
                retries_used += 1
                current_prompt = self._json_parse_retry_prompt(prompt, last_error, raw)
                logger.info(
                    "JSON retry %d/%d for %s - parse error: %s",
                    retries_used,
                    _MAX_JSON_RETRIES - 1,
                    self.agent_type.value,
                    last_error,
                )

        self._log(
            "warning",
            "Could not parse LLM output as JSON after %d attempts - using fallback",
            _MAX_JSON_RETRIES,
        )
        logger.warning(
            "JSON parse failed for %s after %d attempts - last error: %s",
            self.agent_type.value,
            _MAX_JSON_RETRIES,
            last_error,
        )
        self._record_json_retries(effective_model, retries_used)
        _set_last_infer_json_fallback(True)
        return fallback

    @staticmethod
    def _empty_json_retry_prompt(prompt: str) -> str:
        return (
            f"{prompt}\n\n"
            "IMPORTANT: Your previous response was empty. "
            "You MUST respond with valid JSON. "
            "Do not include any text outside the JSON object/array."
        )

    @staticmethod
    def _json_parse_retry_prompt(prompt: str, last_error: str, raw: str) -> str:
        return (
            f"{prompt}\n\n"
            "IMPORTANT: Your previous response was not valid JSON.\n"
            f"Parse error: {last_error}\n"
            f"Your raw output started with: {raw[:100]!r}\n\n"
            "You MUST respond with ONLY valid JSON - no markdown fences, "
            "no explanatory text, no comments. Start with {{ or [."
        )

    def _infer_json_raw(
        self,
        current_prompt: str,
        system_prompt: str | None,
        model_id: str | None,
        kwargs: dict[str, Any],
        effective_model: str,
        retries_used: int,
    ) -> str | None:
        try:
            return self._infer(
                current_prompt,
                system_prompt=system_prompt,
                model_id=model_id,
                expect_json=True,
                **kwargs,
            )
        except (ModelUnavailableError, InferenceError) as exc:
            self._log("warning", "Inference failed for JSON request - returning fallback value")
            logger.warning(
                "JSON inference failed for %s (%s: %s) - using fallback",
                self.agent_type.value,
                type(exc).__name__,
                exc,
            )
            self._record_json_retries(effective_model, retries_used)
            return None

    def _parse_json_candidate(
        self,
        raw: str,
        effective_model: str,
        retries_used: int,
        attempt: int,
    ) -> tuple[Any | None, str]:
        try:
            result = json.loads(raw)
            self._record_json_retries(effective_model, retries_used)
            return result, ""
        except json.JSONDecodeError as parse_err:
            last_error = str(parse_err)

        match = _JSON_EXTRACT_RE.search(raw)
        if match:
            try:
                result = json.loads(match.group(1))
                self._record_json_retries(effective_model, retries_used)
                return result, last_error
            except json.JSONDecodeError:
                logger.warning(
                    "Regex-extracted JSON from %s was invalid on attempt %d - retrying with feedback to model",
                    effective_model,
                    attempt + 1,
                )
        return None, last_error

    @staticmethod
    def _record_json_request(model_id: str, retries: int) -> None:
        """Record every JSON request so retry rates include successful first attempts.

        Args:
            model_id: The model that was used for the request.
            retries: Number of retries needed (0 = first attempt succeeded).
        """
        with _json_retry_lock:
            if model_id not in _json_retry_counts:
                _json_retry_counts[model_id] = []
            counts = _json_retry_counts[model_id]
            counts.append(retries)
            if len(counts) > 100:
                _json_retry_counts[model_id] = counts[-100:]
            if len(counts) >= 5:
                avg = sum(counts[-10:]) / len(counts[-10:])
                if avg >= _MAX_JSON_RETRIES - 1:
                    logger.warning(
                        "Model %s is poor at structured output - average %.1f retries per JSON request",
                        model_id,
                        avg,
                    )

    _record_json_retries = _record_json_request


def get_json_retry_stats() -> dict[str, Any]:
    """Get JSON retry statistics per model for structured output reliability analysis.

    Reads the module-level ``_json_retry_counts`` dict under lock and returns
    a snapshot of per-model retry metrics. Intended for monitoring dashboards
    and kaizen reviews to identify models that struggle with structured output.

    Returns:
        Dict mapping model_id to retry statistics.
    """
    with _json_retry_lock:
        stats: dict[str, Any] = {}
        for model_id, counts in _json_retry_counts.items():
            avg = sum(counts) / len(counts) if counts else 0.0
            stats[model_id] = {
                "total_retries": sum(counts),
                "total_requests": len(counts),
                "retry_events": sum(1 for count in counts if count > 0),
                "retry_rate": round(sum(1 for count in counts if count > 0) / len(counts), 4) if counts else 0.0,
                "average_retries": round(avg, 2),
                "flagged_poor_structured_output": (avg >= _MAX_JSON_RETRIES - 1 and len(counts) >= 5),
            }
        return stats
