"""Batch processing backends for Anthropic and OpenAI APIs.

Internal module containing provider-specific batch submission and polling logic.
Imported by :mod:`vetinari.adapters.batch_processor`.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable
from typing import Any

from vetinari.adapters.base import InferenceResponse
from vetinari.constants import (
    ANTHROPIC_API_BASE_URL,
    INFERENCE_STATUS_OK,
    OPENAI_API_BASE_URL,
    TIMEOUT_LONG,
    TIMEOUT_MEDIUM,
    TIMEOUT_VERY_LONG,
)
from vetinari.types import StatusEnum

logger = logging.getLogger(__name__)


class _AnthropicBatchBackend:
    """Submits batches to Anthropic /v1/messages/batches."""

    def __init__(
        self,
        api_key: str,
        api_version: str = "2023-06-01",
        *,
        monotonic_clock: Callable[[], float] | None = None,
        sleep_fn: Callable[[float], None] | None = None,
    ):
        self._api_key = api_key
        self._api_version = api_version
        self._base_url = ANTHROPIC_API_BASE_URL
        self._monotonic_clock = monotonic_clock or time.monotonic
        self._sleep = sleep_fn or time.sleep

    def _headers(self) -> dict[str, str]:
        return {
            "x-api-key": self._api_key,
            "anthropic-version": self._api_version,
            "Content-Type": "application/json",
        }

    def submit(self, items: list[Any]) -> dict[str, Any]:
        """Submit batch to Anthropic. Returns mapping item_id -> BatchResult.

        Args:
            items: List of BatchItem objects to submit.

        Returns:
            Mapping from each item's ``item_id`` to a ``BatchResult``
            containing either a successful ``InferenceResponse`` or an
            error string if the request failed or timed out.
        """
        import requests as _req  # local import to avoid module-level dependency

        from vetinari.adapters.batch_processor import BatchResult

        batch_requests = []
        for item in items:
            req = item.request
            payload: dict[str, Any] = {
                "model": req.model_id,
                "max_tokens": req.max_tokens,
                "messages": [{"role": "user", "content": req.prompt}],
                "temperature": req.temperature,
                "top_p": req.top_p,
                "top_k": req.top_k,
            }
            if req.system_prompt:
                payload["system"] = req.system_prompt
            if req.stop_sequences:
                payload["stop_sequences"] = req.stop_sequences

            batch_requests.append({
                "custom_id": item.item_id,
                "params": payload,
            })

        try:
            response = _req.post(
                f"{self._base_url}/messages/batches",
                headers=self._headers(),
                json={"requests": batch_requests},
                timeout=TIMEOUT_LONG,
            )
            response.raise_for_status()
            batch_data = response.json()
            batch_id = batch_data["id"]
            logger.info("[Anthropic] Batch submitted: %s (%d items)", batch_id, len(items))

            # Poll for results
            return self._poll_results(batch_id, item_ids={i.item_id for i in items})

        except Exception as exc:
            logger.error("[Anthropic] Batch submission failed: %s — falling back to sync", exc)
            # Return error results for all items
            return {item.item_id: BatchResult(item_id=item.item_id, response=None, error=str(exc)) for item in items}

    def _poll_results(
        self,
        batch_id: str,
        item_ids: set,
        poll_interval: float = 5.0,
        timeout: float = 600.0,
    ) -> dict[str, Any]:
        """Poll Anthropic batch until complete.

        Args:
            batch_id: The batch ID to poll.
            item_ids: Set of item IDs to track.
            poll_interval: Seconds between polls.
            timeout: Max seconds to wait for completion.

        Returns:
            Mapping from item_id to BatchResult.
        """
        import json

        import requests as _req

        from vetinari.adapters.batch_processor import BatchResult

        deadline = self._monotonic_clock() + timeout
        while self._monotonic_clock() < deadline:
            try:
                resp = _req.get(
                    f"{ANTHROPIC_API_BASE_URL}/messages/batches/{batch_id}",
                    headers=self._headers(),
                    timeout=TIMEOUT_MEDIUM,
                )
                resp.raise_for_status()
                status_data = resp.json()

                if status_data.get("processing_status") == "ended":
                    # Fetch results
                    results_resp = _req.get(
                        f"{ANTHROPIC_API_BASE_URL}/messages/batches/{batch_id}/results",
                        headers=self._headers(),
                        timeout=TIMEOUT_LONG,
                    )
                    results_resp.raise_for_status()
                    batch_results: dict[str, BatchResult] = {}
                    for line in results_resp.text.strip().splitlines():
                        item_result = json.loads(line)
                        item_id = item_result.get("custom_id", "")
                        if item_result.get("result", {}).get("type") == "succeeded":
                            msg = item_result["result"]["message"]
                            output = ""
                            if msg.get("content"):
                                output = msg["content"][0].get("text", "")
                            tokens = msg.get("usage", {}).get("input_tokens", 0) + msg.get("usage", {}).get(
                                "output_tokens",
                                0,
                            )
                            cached = msg.get("usage", {}).get("cache_read_input_tokens", 0) > 0
                            inf_resp = InferenceResponse(
                                model_id=msg.get("model", ""),
                                output=output,
                                latency_ms=0,
                                tokens_used=tokens,
                                status=INFERENCE_STATUS_OK,
                            )
                            batch_results[item_id] = BatchResult(item_id=item_id, response=inf_resp, cached=cached)
                        else:
                            err = str(item_result.get("result", {}).get("error", "unknown"))
                            batch_results[item_id] = BatchResult(item_id=item_id, response=None, error=err)
                    return batch_results
            except Exception as poll_exc:
                logger.warning("[Anthropic] Batch poll error: %s", poll_exc)

            self._sleep(poll_interval)

        # Timeout
        return {
            item_id: BatchResult(item_id=item_id, response=None, error="Batch poll timeout") for item_id in item_ids
        }


class _OpenAIBatchBackend:
    """Submits batches to OpenAI /v1/batches."""

    def __init__(
        self,
        api_key: str,
        *,
        monotonic_clock: Callable[[], float] | None = None,
        sleep_fn: Callable[[float], None] | None = None,
    ):
        self._api_key = api_key
        self._base_url = OPENAI_API_BASE_URL
        self._monotonic_clock = monotonic_clock or time.monotonic
        self._sleep = sleep_fn or time.sleep

    def _headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self._api_key}",
            "Content-Type": "application/json",
        }

    def _fetch_output_file_results(self, output_file_id: str) -> dict[str, Any]:
        """Download and parse a completed OpenAI batch output file."""
        import json

        import requests as _req

        from vetinari.adapters.batch_processor import BatchResult

        file_resp = _req.get(
            f"{self._base_url}/files/{output_file_id}/content",
            headers={"Authorization": f"Bearer {self._api_key}"},
            timeout=TIMEOUT_VERY_LONG,
        )
        file_resp.raise_for_status()

        batch_results: dict[str, BatchResult] = {}
        for line in file_resp.text.strip().splitlines():
            item_result = json.loads(line)
            item_id = item_result.get("custom_id", "")
            resp_body = item_result.get("response", {}).get("body", {})
            if resp_body.get("choices"):
                output = resp_body["choices"][0].get("message", {}).get("content", "")
                tokens = resp_body.get("usage", {}).get("total_tokens", 0)
                cached = resp_body.get("usage", {}).get("prompt_tokens_details", {}).get("cached_tokens", 0) > 0
                inf_resp = InferenceResponse(
                    model_id=resp_body.get("model", ""),
                    output=output,
                    latency_ms=0,
                    tokens_used=tokens,
                    status=INFERENCE_STATUS_OK,
                )
                batch_results[item_id] = BatchResult(item_id=item_id, response=inf_resp, cached=cached)
            else:
                err = str(item_result.get("error", "unknown"))
                batch_results[item_id] = BatchResult(item_id=item_id, response=None, error=err)
        return batch_results

    def submit(self, items: list[Any]) -> dict[str, Any]:
        """Submit batch to OpenAI. Returns mapping item_id -> BatchResult.

        Args:
            items: List of BatchItem objects to submit.

        Returns:
            Mapping from each item's ``item_id`` to a ``BatchResult``
            containing either a successful ``InferenceResponse`` or an
            error string if the request failed, timed out, or was cancelled.
        """
        import io
        import json

        import requests as _req

        from vetinari.adapters.batch_processor import BatchResult

        # Build JSONL file
        jsonl_lines = []
        for item in items:
            req = item.request
            messages = []
            if req.system_prompt:
                messages.append({"role": "system", "content": req.system_prompt})
            messages.append({"role": "user", "content": req.prompt})

            body: dict[str, Any] = {
                "model": req.model_id,
                "messages": messages,
                "temperature": req.temperature,
                "top_p": req.top_p,
                "max_tokens": req.max_tokens,
            }
            if req.stop_sequences:
                body["stop"] = req.stop_sequences[:4]

            jsonl_lines.append(
                json.dumps({
                    "custom_id": item.item_id,
                    "method": "POST",
                    "url": "/v1/chat/completions",
                    "body": body,
                }),
            )

        jsonl_content = "\n".join(jsonl_lines).encode("utf-8")

        try:
            # Upload file
            upload_resp = _req.post(
                f"{self._base_url}/files",
                headers={"Authorization": f"Bearer {self._api_key}"},
                files={"file": ("batch.jsonl", io.BytesIO(jsonl_content), "application/jsonl")},
                data={"purpose": "batch"},
                timeout=TIMEOUT_LONG,
            )
            upload_resp.raise_for_status()
            file_id = upload_resp.json()["id"]

            # Create batch
            batch_resp = _req.post(
                f"{self._base_url}/batches",
                headers=self._headers(),
                json={
                    "input_file_id": file_id,
                    "endpoint": "/v1/chat/completions",
                    "completion_window": "24h",
                },
                timeout=TIMEOUT_MEDIUM,
            )
            batch_resp.raise_for_status()
            batch_id = batch_resp.json()["id"]
            logger.info("[OpenAI] Batch submitted: %s (%d items)", batch_id, len(items))

            return self._poll_results(batch_id, item_ids={i.item_id for i in items})

        except Exception as exc:
            logger.error("[OpenAI] Batch submission failed: %s — falling back to sync", exc)
            return {item.item_id: BatchResult(item_id=item.item_id, response=None, error=str(exc)) for item in items}

    def _poll_results(
        self,
        batch_id: str,
        item_ids: set,
        poll_interval: float = 10.0,
        timeout: float = 86400.0,
    ) -> dict[str, Any]:
        """Poll OpenAI batch until complete.

        Args:
            batch_id: The batch ID to poll.
            item_ids: Set of item IDs to track.
            poll_interval: Seconds between polls.
            timeout: Max seconds to wait for completion.

        Returns:
            Mapping from item_id to BatchResult.
        """
        import requests as _req

        from vetinari.adapters.batch_processor import BatchResult

        deadline = self._monotonic_clock() + timeout
        while self._monotonic_clock() < deadline:
            try:
                resp = _req.get(
                    f"{self._base_url}/batches/{batch_id}",
                    headers=self._headers(),
                    timeout=TIMEOUT_MEDIUM,
                )
                resp.raise_for_status()
                batch_data = resp.json()
                status = batch_data.get("status", "")

                if status == StatusEnum.COMPLETED.value:
                    output_file_id = batch_data.get("output_file_id")
                    if not output_file_id:
                        break

                    return self._fetch_output_file_results(output_file_id)

                if status in (StatusEnum.FAILED.value, "expired", StatusEnum.CANCELLED.value):
                    break

            except Exception as poll_exc:
                logger.warning("[OpenAI] Batch poll error: %s", poll_exc)

            self._sleep(poll_interval)

        return {
            item_id: BatchResult(item_id=item_id, response=None, error="Batch poll timeout/failed")
            for item_id in item_ids
        }
