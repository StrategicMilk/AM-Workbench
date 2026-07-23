"""Lightweight LLM call helpers for classification, scoring, and analysis.

Provides simple functions for making quick LLM calls used by the
intelligence upgrade modules (goal classification, defect analysis,
complexity routing, etc.). These are thin wrappers around the
AdapterManager that handle unavailability gracefully.

All functions return None when the LLM is unavailable, letting callers
fall back to heuristic methods.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from typing import Any

from vetinari.boundary_guards import require_score_in_range
from vetinari.config.inference_config import get_inference_config
from vetinari.security.fail_closed import UntrustedInputError, sanitize_untrusted_text
from vetinari.security.redaction import redact_text

logger = logging.getLogger(__name__)
_CATEGORY_MAX_TOKENS = 20
_CONFIDENCE_SCORE_MAX_TOKENS = 10


# Cached system prompt prefixes — allocated once, reused across calls
_GOAL_CLASSIFICATION_PROMPT = (
    "You are a task classifier. Given a user's goal, classify it into exactly one category.\n"
    "Categories: code, research, docs, creative, security, data, devops, ui, image, general.\n"
    "Respond with ONLY the category name, nothing else."
)

_DEFECT_DIAGNOSIS_PROMPT = (
    "You are a quality assurance analyst. A task output was rejected by the Inspector.\n"
    "Classify the root cause as exactly one of: hallucinated_import, ambiguous_spec, "
    "model_limitation, insufficient_context, output_format, runtime_error, quality_below_threshold.\n"
    "Then provide a one-sentence explanation.\n"
    "Format: CATEGORY: explanation"
)

_COMPLEXITY_ASSESSMENT_PROMPT = (
    "You are a software complexity estimator. Given a task description, estimate:\n"
    "1. Number of files likely touched (1-50)\n"
    "2. Number of dependencies involved (0-20)\n"
    "3. Risk level: LOW, MEDIUM, or HIGH\n"
    "4. Overall classification: SIMPLE, MODERATE, or COMPLEX\n"
    "Format your response as: FILES=N DEPS=N RISK=X CLASS=Y"
)

_AMBIGUITY_CHECK_PROMPT = (
    "You are an ambiguity detector. Determine if the following request is ambiguous "
    "enough to need clarification before a developer can work on it.\n"
    "Answer YES or NO. If YES, state the most important clarifying question.\n"
    "Format: YES/NO: <clarifying question if YES>"
)

_CONFIDENCE_SCORING_PROMPT = (
    "You are a response quality assessor. Given a task description and its response, "
    "score how complete and confident the response is on a scale of 0.0 to 1.0.\n"
    "Consider: Does it fully address the task? Is it correct? Is it actionable?\n"
    "Respond with only a decimal number between 0.0 and 1.0."
)

_RETRY_BRIEF_PROMPT = (
    "You are a retry advisor. A previous attempt at a task failed.\n"
    "Generate exactly 3 specific things the next attempt should do differently.\n"
    "Be concrete and actionable. Format as a numbered list."
)


@dataclass(frozen=True, slots=True)
class LLMCallResult:
    """LLM helper result with response provenance."""

    output: str | None
    model_id: str
    status: str
    latency_ms: int
    tokens_used: int
    response_metadata: dict[str, Any]
    error: str | None = None

    def __repr__(self) -> str:
        return (
            "LLMCallResult("
            f"model_id={self.model_id!r}, status={self.status!r}, "
            f"latency_ms={self.latency_ms!r}, tokens_used={self.tokens_used!r}, "
            f"has_output={self.output is not None!r}, error={self.error!r})"
        )


def quick_llm_call(
    prompt: str,
    system_prompt: str = "",
    max_tokens: int = 200,
    temperature: float = 0.1,
) -> str | None:
    """Make a lightweight LLM call, returning None if unavailable.

    This is the core helper used by all intelligence upgrade functions.
    It keeps token usage low by enforcing short max_tokens defaults and
    low temperature for classification tasks.

    Args:
        prompt: The user/task prompt to send to the LLM.
        system_prompt: System-level instructions for the model.
        max_tokens: Maximum tokens to generate (keep low for classification).
        temperature: Sampling temperature (low for deterministic classification).

    Returns:
        The LLM response text, or None if the LLM is unavailable.
    """
    return quick_llm_call_with_provenance(
        prompt=prompt,
        system_prompt=system_prompt,
        max_tokens=max_tokens,
        temperature=temperature,
    ).output


def quick_llm_call_with_provenance(
    prompt: str,
    system_prompt: str = "",
    max_tokens: int = 200,
    temperature: float = 0.1,
) -> LLMCallResult:
    """Make a lightweight LLM call and return response provenance.

    Args:
        prompt: User/task prompt to send after redaction.
        system_prompt: Optional system prompt for the model.
        max_tokens: Maximum output tokens to request.
        temperature: Sampling temperature to request.

    Returns:
        LLMCallResult with output, model id, status, latency, token usage, and error metadata.
    """
    try:
        safe_prompt = sanitize_untrusted_text(prompt, max_length=20_000)
        safe_system_prompt = sanitize_untrusted_text(system_prompt, max_length=4_000) if system_prompt else ""
        from vetinari.adapter_manager import get_adapter_manager
        from vetinari.adapters.base import InferenceRequest

        mgr = get_adapter_manager()

        request = InferenceRequest(
            model_id="",  # Empty = let AdapterManager select the best available model
            prompt=redact_text(safe_prompt),
            system_prompt=safe_system_prompt,
            max_tokens=max_tokens,
            temperature=temperature,
        )
        response = mgr.infer(request)
        response_status = getattr(response, "status", "error")
        response_error = getattr(response, "error", None)
        response_model_id = getattr(response, "model_id", None) or getattr(request, "model_id", "")
        response_latency_ms = getattr(response, "latency_ms", 0)
        response_tokens_used = getattr(response, "tokens_used", 0)
        response_metadata = dict(getattr(response, "metadata", None) or {})

        if response_status == "error":
            logger.debug("LLM call returned error status: %s", response_error)
            return LLMCallResult(
                output=None,
                model_id=response_model_id,
                status=response_status,
                latency_ms=response_latency_ms,
                tokens_used=response_tokens_used,
                response_metadata=response_metadata,
                error=response_error,
            )

        response_output = getattr(response, "output", None)
        output = response_output.strip() if response_output else None
        return LLMCallResult(
            output=output,
            model_id=response_model_id,
            status=response_status,
            latency_ms=response_latency_ms,
            tokens_used=response_tokens_used,
            response_metadata=response_metadata,
            error=response_error,
        )

    except ImportError:
        logger.warning("AdapterManager not available - LLM helpers disabled, using heuristic fallback")
        return LLMCallResult(None, "", "unavailable", 0, 0, {}, "adapter_manager_unavailable")
    except UntrustedInputError as exc:
        logger.warning("LLM call rejected untrusted prompt input: %s", exc)
        return LLMCallResult(None, "", "blocked", 0, 0, {}, "untrusted_input")
    except Exception as exc:
        logger.warning("LLM call failed - falling back to heuristic", exc_info=True)
        return LLMCallResult(None, "", "error", 0, 0, {}, str(exc))


def classify_goal_via_llm(goal: str) -> str | None:
    """Classify a goal into a category, using TaskClassifier before any LLM call.

    Checks TaskClassifier confidence first.  When confidence >= CONFIDENCE_THRESHOLD
    the category is returned directly without making an LLM call.  Only falls back
    to the LLM when the classifier is uncertain (cold-start or low-confidence).

    Args:
        goal: The user's goal string.

    Returns:
        Category string (e.g. "code", "security") or None if both paths unavailable.
    """
    # --- Tier 1: TaskClassifier (no LLM needed) ---
    try:
        from vetinari.ml.task_classifier import CONFIDENCE_THRESHOLD, TaskClassifier

        category, confidence = TaskClassifier().classify(goal)
        if confidence >= CONFIDENCE_THRESHOLD:
            logger.debug(
                "classify_goal_via_llm: TaskClassifier returned %r (confidence=%.3f) — skipping LLM",
                category,
                confidence,
            )
            return category
        logger.debug(
            "classify_goal_via_llm: TaskClassifier confidence %.3f < %.2f — falling back to LLM",
            confidence,
            CONFIDENCE_THRESHOLD,
        )
    except Exception as exc:
        logger.warning(
            "classify_goal_via_llm: TaskClassifier unavailable (%s) — falling back to LLM",
            exc,
        )

    # --- Tier 2: LLM fallback ---
    result = quick_llm_call(
        prompt=f"Goal: {goal}",
        system_prompt=_GOAL_CLASSIFICATION_PROMPT,
        max_tokens=_CATEGORY_MAX_TOKENS,
    )
    if result:
        category = result.strip().lower().split()[0] if result.strip() else None
        valid_categories = {
            "code",
            "research",
            "docs",
            "creative",
            "security",
            "data",
            "devops",
            "ui",
            "image",
            "general",
        }
        if category in valid_categories:
            # Feed the LLM-confirmed label back to TaskClassifier as a training example
            try:
                from vetinari.ml.task_classifier import TaskClassifier

                TaskClassifier().add_example(goal, category)
            except Exception as exc:
                logger.warning("classify_goal_via_llm: could not record training example (%s)", exc)
            return category
        logger.debug("LLM returned invalid category %r, ignoring", result)
    return None


def diagnose_defect_via_llm(
    task_description: str,
    rejection_reason: str,
    agent_type: str = "",
) -> tuple[str, str] | None:
    """Diagnose a defect root cause using LLM analysis.

    Args:
        task_description: What the task was trying to do.
        rejection_reason: Why the Inspector rejected the output.
        agent_type: Which agent produced the rejected output.

    Returns:
        Tuple of (category, explanation) or None if LLM unavailable.
    """
    prompt = f"Task: {task_description}\nRejection reason: {rejection_reason}\n"
    if agent_type:
        prompt += f"Agent type: {agent_type}\n"
    prompt += "Classify the root cause and explain in one sentence."

    cfg = get_inference_config().get_profile("reasoning")
    result = quick_llm_call(
        prompt=prompt,
        system_prompt=_DEFECT_DIAGNOSIS_PROMPT,
        max_tokens=cfg.max_tokens,
    )
    if result and ":" in result:
        parts = result.split(":", 1)
        category = parts[0].strip().lower()
        explanation = parts[1].strip()
        return (category, explanation)
    return None


def assess_complexity_static(description: str) -> dict[str, Any] | None:
    """Assess task complexity using AST + structural analysis — no LLM call.

    Delegates to the AST complexity helper in the routing module. Returns the
    same dict shape as the old LLM-based function so callers need no changes.

    Args:
        description: Task description to assess.

    Returns:
        Dict with ``files``, ``risk``, ``classification``, and ``cyclomatic``
        keys, or None if the description is empty.
    """
    try:
        from vetinari.routing.complexity_router import _ast_complexity_from_description

        return _ast_complexity_from_description(description)
    except Exception as exc:
        logger.warning(
            "assess_complexity_static: AST analysis failed (%s) — returning None",
            exc,
        )
        return None


# Backward-compatible alias — callers that used the old LLM variant still work
assess_complexity_via_llm = assess_complexity_static


def check_ambiguity_via_llm(request_text: str) -> tuple[bool, str] | None:
    """Check if a request is ambiguous using LLM judgment.

    Args:
        request_text: The user's request text.

    Returns:
        Tuple of (is_ambiguous, clarifying_question) or None if unavailable.
    """
    cfg = get_inference_config().get_profile("reasoning")
    result = quick_llm_call(
        prompt=f"Request: {request_text}",
        system_prompt=_AMBIGUITY_CHECK_PROMPT,
        max_tokens=cfg.max_tokens,
    )
    if not result:
        return None

    upper = result.upper().strip()
    if upper.startswith("YES"):
        question = result.split(":", 1)[1].strip() if ":" in result else ""
        return (True, question)
    if upper.startswith("NO"):
        return (False, "")
    return None


def score_confidence_structural(task_description: str, response_text: str) -> float | None:
    """Score response confidence using structural heuristics — no LLM call.

    Checks three signals: code block presence, output format matching, and
    keyword overlap between task description and response.  Returns a score
    in [0.0, 1.0] when the signals are strong enough to be conclusive, or
    None when the signals are ambiguous and LLM judgment is needed.

    Args:
        task_description: The original task description.
        response_text: The response to score.

    Returns:
        Confidence score 0.0-1.0 if the structural check is conclusive, or
        None when the response needs LLM assessment.
    """
    if not task_description or not response_text:
        return 0.0

    desc_lower = task_description.lower()
    resp_lower = response_text.lower()
    score = 0.5  # neutral start

    # Signal 1: code block presence when the task asks for code
    task_wants_code = any(
        kw in desc_lower for kw in ("implement", "write", "create", "build", "function", "class", "code")
    )
    has_code_block = "```" in response_text or re.search(r"\bdef\s+\w+\s*\(", response_text) is not None
    if task_wants_code:
        if has_code_block:
            score += 0.2
        else:
            score -= 0.25

    # Signal 2: output format match — task asks for JSON and response contains it
    task_wants_json = "json" in desc_lower or "dict" in desc_lower
    has_json = bool(re.search(r"\{[^}]+\}", response_text))
    if task_wants_json:
        if has_json:
            score += 0.15
        else:
            score -= 0.2

    # Signal 3: keyword overlap between task and response
    task_words = set(re.findall(r"\b[a-z]{4,}\b", desc_lower))
    resp_words = set(re.findall(r"\b[a-z]{4,}\b", resp_lower))
    if task_words:
        overlap = len(task_words & resp_words) / len(task_words)
        score += (overlap - 0.3) * 0.4  # +0.08 for 50% overlap, -0.12 for 0% overlap

    score = max(0.0, min(1.0, score))

    # Return None when the score is in the uncertain middle — let LLM decide
    if 0.35 < score < 0.65:
        return None

    logger.debug(
        "score_confidence_structural: task=%r -> score=%.3f (code=%s, json=%s)",
        task_description[:60],
        score,
        has_code_block,
        has_json,
    )
    return round(score, 3)


def score_confidence_via_llm(task_description: str, response_text: str) -> float | None:
    """Score response confidence, using structural check before any LLM call.

    Calls :func:`score_confidence_structural` first.  If the structural check
    is conclusive (returns a non-None score), returns that directly without
    making an LLM call.  Falls back to the LLM only when the structural score
    is in the ambiguous middle range.

    Args:
        task_description: The original task.
        response_text: The response to score.

    Returns:
        Confidence score 0.0-1.0, or None if unavailable.
    """
    # --- Tier 1: structural heuristic (no LLM needed) ---
    structural_score = score_confidence_structural(task_description, response_text)
    if structural_score is not None:
        structural_score = require_score_in_range(structural_score, field_name="confidence")
        if structural_score == 0.0:
            logger.warning("score_confidence_via_llm: structural-only confidence is 0.0")
        logger.debug(
            "score_confidence_via_llm: structural check conclusive (%.3f) — skipping LLM",
            structural_score,
        )
        return structural_score

    # --- Tier 2: LLM fallback for ambiguous cases ---
    result = quick_llm_call(
        prompt=f"Task: {task_description}\n\nResponse: {response_text[:500]}",
        system_prompt=_CONFIDENCE_SCORING_PROMPT,
        max_tokens=_CONFIDENCE_SCORE_MAX_TOKENS,
    )
    if result:
        try:
            score = float(result.strip())
            score = require_score_in_range(score, field_name="confidence")
            if score == 0.0:
                logger.warning("score_confidence_via_llm: LLM confidence is 0.0")
            return score
        except ValueError:
            logger.warning(
                "Could not parse confidence score %r as float — returning None (no score available)",
                result.strip(),
            )
    return None


# Pre-written retry briefs keyed by defect category.
# Each value is a 3-item numbered list matching the format the LLM would produce.
_RETRY_TEMPLATES: dict[str, str] = {
    "hallucinated_import": (
        "1. Verify every import against the project's pyproject.toml before adding it.\n"
        "2. Use only modules already imported elsewhere in the codebase; search with grep before introducing new ones.\n"
        "3. If a third-party library is genuinely needed, add it to pyproject.toml dependencies first."
    ),
    "ambiguous_spec": (
        "1. Ask one focused clarifying question before writing any code.\n"
        "2. State your interpretation of the requirement as a comment at the top of the implementation.\n"
        "3. Implement the simplest interpretation; note alternatives in a docstring for the reviewer."
    ),
    "model_limitation": (
        "1. Break the task into smaller, independently verifiable sub-tasks.\n"
        "2. Reduce context size by summarising background material rather than including it verbatim.\n"
        "3. Provide explicit worked examples for the most complex part of the task."
    ),
    "insufficient_context": (
        "1. Read the relevant source files before attempting the implementation.\n"
        "2. Identify every variable, function, and class referenced in the task and confirm they exist.\n"
        "3. List missing dependencies explicitly and request them before proceeding."
    ),
    "output_format": (
        "1. Re-read the output format specification and produce a skeleton before filling in content.\n"
        "2. Validate the output structure (JSON parse, Python syntax check) before submitting.\n"
        "3. Include all required fields — missing fields are the most common format failure."
    ),
    "runtime_error": (
        "1. Trace the error to its root cause; do not patch the symptom.\n"
        "2. Add a targeted test that reproduces the error before making any code change.\n"
        "3. Run the full test suite after the fix to confirm no regressions."
    ),
    "quality_below_threshold": (
        "1. Address every specific issue raised in the Inspector's feedback before resubmitting.\n"
        "2. Run ruff check and fix all lint errors; add missing docstrings and type hints.\n"
        "3. Self-review against the project's Definition of Done checklist before submitting."
    ),
    "logic_error": (
        "1. Add a minimal reproducing test case that exposes the incorrect behaviour.\n"
        "2. Trace the data flow from input to output and identify the first point of divergence.\n"
        "3. Fix the root cause, not a downstream symptom — confirm the test now passes."
    ),
    "style_violation": (
        "1. Run ruff check --fix and ruff format on the changed files before resubmitting.\n"
        "2. Ensure all public functions have Google-style docstrings with Args/Returns sections.\n"
        "3. Replace any bare except clauses, print() calls, and magic numbers."
    ),
}


def generate_retry_brief(
    error_description: str,
    inspector_feedback: str = "",
    defect_category: str = "",
) -> str | None:
    """Generate a retry brief with 3 specific changes for the next attempt.

    Uses a pre-written template for known defect categories, falling back to
    the LLM only when the category is unknown or not provided.  This eliminates
    LLM calls for the majority of retries where the defect category is clear.

    Args:
        error_description: What went wrong in the previous attempt.
        inspector_feedback: Inspector's rejection reason, if available.
        defect_category: Defect category string from DefectClassifier. When
            this matches a key in ``_RETRY_TEMPLATES``, no LLM call is made.

    Returns:
        Retry brief text with 3 action items, or None if unavailable.
    """
    # --- Tier 1: pre-written template (no LLM needed) ---
    if defect_category and defect_category in _RETRY_TEMPLATES:
        brief = _RETRY_TEMPLATES[defect_category]
        logger.debug(
            "generate_retry_brief: using template for category %r — skipping LLM",
            defect_category,
        )
        return brief

    # --- Tier 2: LLM fallback for unknown categories ---
    prompt = f"Previous error: {error_description}\n"
    if inspector_feedback:
        prompt += f"Inspector feedback: {inspector_feedback}\n"
    if defect_category:
        prompt += f"Defect category: {defect_category}\n"
    prompt += "Generate 3 specific things to do differently."

    _profile = get_inference_config().get_profile("error_handling")
    return quick_llm_call(
        prompt=prompt,
        system_prompt=_RETRY_BRIEF_PROMPT,
        max_tokens=_profile.max_tokens,
        temperature=_profile.temperature,
    )
