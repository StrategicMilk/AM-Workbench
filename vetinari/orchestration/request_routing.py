"""Request routing — production levelling and goal classification.

Contains:
- ``RequestQueue``: Priority queue implementing Heijunka (production leveling,
  Dept 4.7) for burst-arrival smoothing.
- ``classify_goal``: Keyword-based goal category classifier.
- ``get_goal_routing``: Maps a goal string to ``(agent_type, mode, model_tier)``.

Priority constants are also exported so callers can reference named values
instead of magic numbers.
"""

from __future__ import annotations

import heapq
import logging
import re
import threading

from vetinari.types import AgentType

logger = logging.getLogger(__name__)

DEVELOPER_WORKFLOW_CONTRACT_ID = "wave3-rcg0014p06-scope-followup-P09"
REQUEST_ROUTING_WORKFLOW_GUARDS: tuple[str, ...] = (
    "queue backpressure raises QueueFullError at max depth",
    "complete ignores unknown execution ids without freeing capacity",
    "high-confidence keyword routing does not invoke LLM fallback",
    "routing decisions fall back deterministically when LLM classification fails",
)


def developer_workflow_contract() -> dict[str, object]:
    """Return request-routing workflow guarantees verified by this follow-up pack."""
    return {
        "pack": DEVELOPER_WORKFLOW_CONTRACT_ID,
        "surface": "vetinari/orchestration/request_routing.py",
        "guards": REQUEST_ROUTING_WORKFLOW_GUARDS,
    }


# ---------------------------------------------------------------------------
# Priority constants — lower value = higher priority
# ---------------------------------------------------------------------------

PRIORITY_REWORK = 2  # Fix defects before making new ones (lean principle)
PRIORITY_EXPRESS = 3  # Fast, get them through
PRIORITY_STANDARD = 5  # Normal
PRIORITY_CUSTOM = 7  # Long-running, don't block express


# ---------------------------------------------------------------------------
# Request Queue — Production Leveling (Dept 4.7 Heijunka)
# ---------------------------------------------------------------------------


class QueueFullError(Exception):
    """Raised when the request queue is at capacity and cannot accept new work."""


class RequestQueue:
    """Priority queue for incoming requests. Prevents resource contention from burst arrivals.

    Implements production leveling (Heijunka) — smooths burst work over time by
    queuing and prioritizing requests instead of processing all at once.
    Includes backpressure: rejects new requests when queue depth exceeds
    ``max_depth`` to prevent unbounded resource consumption.

    Args:
        max_concurrent: Maximum concurrent executions allowed.
        max_depth: Maximum queue depth before rejecting new requests (backpressure).
    """

    def __init__(self, max_concurrent: int = 3, max_depth: int = 10) -> None:
        self._queue: list[tuple[int, float, str, str, dict]] = []  # (priority, timestamp, exec_id, goal, context)
        self.active_count: int = 0
        self._max_concurrent = max_concurrent
        self._max_depth = max_depth  # Backpressure threshold
        self._lock = threading.Lock()
        self._counter = 0  # Tiebreaker for equal priorities
        # Tracks exec_ids currently in-flight so complete() can reject unknown/duplicate calls
        self._active_ids: set[str] = set()

    def enqueue(self, goal: str, context: dict, priority: int = PRIORITY_STANDARD) -> str:
        """Add request to queue. Priority 1 (highest) to 10 (lowest).

        Raises QueueFullError when the queue depth has reached max_depth,
        signalling the caller to return a 429 (Too Many Requests) response.

        Args:
            goal: The user's goal string.
            context: Pipeline context dict.
            priority: Request priority (lower = higher priority).

        Returns:
            Execution ID for tracking.

        Raises:
            QueueFullError: When queue depth >= max_depth (backpressure).
        """
        import uuid as _uuid

        exec_id = str(_uuid.uuid4())[:8]
        with self._lock:
            if len(self._queue) >= self._max_depth:
                logger.warning(
                    "[RequestQueue] Backpressure: rejecting %s (depth=%d >= max_depth=%d)",
                    exec_id,
                    len(self._queue),
                    self._max_depth,
                )
                raise QueueFullError(f"Request queue at capacity ({self._max_depth}). Try again later.")
            self._counter += 1
            heapq.heappush(self._queue, (priority, self._counter, exec_id, goal, context))
        logger.info("[RequestQueue] Enqueued %s (priority=%d, depth=%d)", exec_id, priority, len(self._queue))
        return exec_id

    def dequeue(self) -> tuple[str, str, dict] | None:
        """Get next request if under concurrency limit.

        Returns:
            Tuple of (exec_id, goal, context) or None if at capacity or empty.
        """
        with self._lock:
            if self.active_count >= self._max_concurrent:
                return None
            if not self._queue:
                return None
            _priority, _counter, exec_id, goal, context = heapq.heappop(self._queue)
            self.active_count += 1
            self._active_ids.add(exec_id)
            logger.info("[RequestQueue] Dequeued %s (active=%d/%d)", exec_id, self.active_count, self._max_concurrent)
            return (exec_id, goal, context)

    def complete(self, exec_id: str) -> None:
        """Mark request complete, allowing next queued request to start.

        Only decrements the active count for exec_ids that were actually dequeued.
        Unknown or already-completed exec_ids are logged and ignored — callers that
        double-complete or supply a phantom ID must not inadvertently free a slot.

        Args:
            exec_id: The execution that completed.
        """
        with self._lock:
            if exec_id not in self._active_ids:
                logger.warning(
                    "[RequestQueue] complete() called for unknown or already-completed exec_id %s — ignoring",
                    exec_id,
                )
                return
            self._active_ids.discard(exec_id)
            self.active_count = max(0, self.active_count - 1)
        logger.info("[RequestQueue] Completed %s (active=%d/%d)", exec_id, self.active_count, self._max_concurrent)

    @property
    def depth(self) -> int:
        """Number of requests waiting in queue."""
        return len(self._queue)


# ---------------------------------------------------------------------------
# Goal categorisation (Phase 7.6 — Oh-My-OpenCode task category pattern)
# ---------------------------------------------------------------------------

# Keyword lists for the 9 goal categories.  Order matters: more specific
# categories should be checked first (e.g. "security audit" before "audit").

_GOAL_CATEGORY_KEYWORDS: dict[str, list[str]] = {
    "workflow": ["workflow audit", "workflow", "program audit", "routing audit", "process audit", "agent workflow"],
    "security": [
        "security",
        "security audit",
        "audit",
        "vulnerability",
        "pentest",
        "cve",
        "owasp",
        "exploit",
        "review pr",
    ],
    "devops": ["deploy", "ci/cd", "docker", "dockerize", "kubernetes", "pipeline", "devops", "terraform", "helm"],
    "image": ["logo", "icon", "mockup", "diagram", "image", "illustration", "screenshot"],
    "creative": ["story", "poem", "fiction", "narrative", "campaign", "creative writ", "novel"],
    "data": ["database", "schema", "migration", "etl", "sql", "nosql", "data model"],
    "ui": ["ui", "ux", "frontend", "design", "wireframe", "layout", "responsive", "css"],
    "docs": ["document", "documentation", "readme", "api docs", "manual", "changelog", "guide", "tutorial"],
    "research": ["research", "analyze", "investigate", "study", "explore", "survey", "compare", "explain"],
    "code": ["code", "implement", "build", "develop", "fix", "refactor", "function", "class", "module", "test", "api"],
    # Version control operations — checked after security to avoid "audit" collision
    "git": ["git", "branch", "merge", "commit", "pr", "pull request", "release", "changelog", "version control"],
    # Agent modes without a high-level routing category — keyword-driven disambiguation
    "lateral_thinking": [
        "lateral thinking",
        "creative approach",
        "unconventional",
        "brainstorm",
        "alternative solution",
    ],
    "ontological_analysis": ["ontological", "ontology", "knowledge graph", "semantic", "taxonomy", "classify concepts"],
    "suggest": ["suggest", "recommend", "propose options", "what should", "give me ideas", "best approach"],
    "devops_ops": ["runbook", "incident", "on-call", "ops", "site reliability", "sre", "postmortem", "alert"],
    "monitor": ["monitor", "observe", "metrics", "dashboard", "tracing", "telemetry", "health check", "uptime"],
    "cost_analysis": ["cost", "budget", "spend", "pricing", "resource usage", "optimize spend", "billing"],
}

# Maps GoalCategory value -> (primary agent type, default mode, model tier hint)
# v0.5.0: Updated to use 3-agent factory pipeline
_GOAL_ROUTING_TABLE: dict[str, tuple] = {
    "code": (AgentType.WORKER.value, "build", "coder"),
    "research": (AgentType.WORKER.value, "domain_research", "general"),
    "docs": (AgentType.WORKER.value, "documentation", "general"),
    "creative": (AgentType.WORKER.value, "creative_writing", "general"),
    "security": (AgentType.INSPECTOR.value, "security_audit", "coder"),
    "workflow": (AgentType.WORKER.value, "workflow_audit", "general"),
    "data": (AgentType.WORKER.value, "database", "general"),
    "devops": (AgentType.WORKER.value, "devops", "coder"),
    "ui": (AgentType.WORKER.value, "ui_design", "vision"),
    "image": (AgentType.WORKER.value, "image_generation", "general"),
    "general": (AgentType.FOREMAN.value, "plan", "general"),
    # Version control workflow — Worker handles branching, merging, and release prep
    "git": (AgentType.WORKER.value, "git_workflow", "coder"),
    # Extended mode routing — maps keyword-detected intents to specific Worker modes
    "lateral_thinking": (AgentType.WORKER.value, "lateral_thinking", "general"),
    "ontological_analysis": (AgentType.WORKER.value, "ontological_analysis", "general"),
    "suggest": (AgentType.FOREMAN.value, "suggest", "general"),
    "devops_ops": (AgentType.WORKER.value, "devops_ops", "coder"),
    "monitor": (AgentType.WORKER.value, "monitor", "general"),
    "cost_analysis": (AgentType.WORKER.value, "cost_analysis", "general"),
}

_DETERMINISTIC_CONFIDENCE_THRESHOLD = 0.70
_AMBIGUOUS_LLM_HINTS = frozenset({"do", "make", "create", "improve", "help", "stuff", "thing", "things"})


def classify_goal(goal: str) -> str:
    """Classify a goal string into one of the 9 categories.

    Returns the GoalCategory *value* string (e.g. ``"code"``, ``"security"``).
    Falls back to ``"general"`` when no keywords match.

    Args:
        goal: The user's goal string to classify.

    Returns:
        Category string such as ``"code"``, ``"security"``, or ``"general"``.
    """
    result = classify_goal_detailed(goal)
    return result["category"]


def classify_goal_detailed(goal: str) -> dict:
    """Classify a goal with confidence score and complexity assessment.

    Runs deterministic keyword classification first. The LLM is consulted only
    for ambiguous low-confidence goals with some actionable but non-specific
    signal; high-confidence deterministic matches never call the LLM.

    Args:
        goal: The user's goal string to classify.

    Returns:
        Dict with keys: ``category``, ``confidence`` (0.0-1.0),
        ``complexity`` (``"express"``, ``"standard"``, or ``"custom"``),
        ``cross_cutting`` (list of secondary categories detected),
        ``matched_keywords`` (list of keywords that fired),
        ``source`` (``"llm"`` or ``"keyword"``).
    """
    goal_lower = goal.lower()
    word_count = len(goal.split())

    category_scores: dict[str, list[str]] = {}
    for category, keywords in _GOAL_CATEGORY_KEYWORDS.items():
        hits = [kw for kw in keywords if _keyword_matches(goal_lower, kw)]
        if hits:
            category_scores[category] = hits

    if not category_scores:
        keyword_result = {
            "category": "general",
            "confidence": 0.3,
            "complexity": _assess_complexity(word_count, 0),
            "cross_cutting": [],
            "matched_keywords": [],
            "source": "keyword",
        }
        if not (_terms(goal_lower) & _AMBIGUOUS_LLM_HINTS):
            return keyword_result
        return _try_llm_goal_classification(goal, word_count, keyword_result)

    ranked = sorted(category_scores.items(), key=lambda kv: len(kv[1]), reverse=True)
    primary_cat, primary_hits = ranked[0]

    total_hits = sum(len(v) for v in category_scores.values())
    confidence = round(len(primary_hits) / max(total_hits, 1), 2)
    if len(ranked) == 1:
        confidence = min(1.0, confidence + 0.3)

    cross_cutting = [cat for cat, _ in ranked[1:]]
    if primary_cat == "workflow" and any("workflow" in hit for hit in primary_hits):
        confidence = max(confidence, _DETERMINISTIC_CONFIDENCE_THRESHOLD)
    keyword_result = {
        "category": primary_cat,
        "confidence": round(min(1.0, confidence), 2),
        "complexity": _assess_complexity(word_count, len(cross_cutting)),
        "cross_cutting": cross_cutting,
        "matched_keywords": primary_hits,
        "source": "keyword",
    }
    if keyword_result["confidence"] >= _DETERMINISTIC_CONFIDENCE_THRESHOLD:
        return keyword_result
    return _try_llm_goal_classification(goal, word_count, keyword_result)


def _try_llm_goal_classification(goal: str, word_count: int, keyword_result: dict) -> dict:
    """Try LLM classification for ambiguous goals, falling back deterministically."""
    try:
        from vetinari.llm_helpers import classify_goal_via_llm

        llm_category = classify_goal_via_llm(goal)
        if llm_category and llm_category in _GOAL_ROUTING_TABLE:
            logger.info("LLM classified ambiguous goal as %r", llm_category)
            return {
                "category": llm_category,
                "confidence": 0.90,
                "complexity": _assess_complexity(word_count, 0),
                "cross_cutting": [],
                "matched_keywords": keyword_result.get("matched_keywords", []),
                "source": "llm",
            }
    except Exception:
        logger.warning("LLM goal classification failed for ambiguous goal; using deterministic keyword result")
    return keyword_result


def _keyword_matches(text: str, keyword: str) -> bool:
    return re.search(rf"(?<![a-z0-9]){re.escape(keyword.lower())}(?![a-z0-9])", text) is not None


def _terms(text: str) -> set[str]:
    return {part for part in text.split() if part}


# Complexity assessment
EXPRESS_WORD_THRESHOLD = 12  # Goals shorter than this are likely express tasks
CUSTOM_WORD_THRESHOLD = 40  # Goals longer than this are likely custom/complex


def _assess_complexity(word_count: int, cross_cutting_count: int) -> str:
    """Determine goal complexity from word count and cross-cutting category count.

    Args:
        word_count: Number of words in the goal string.
        cross_cutting_count: Number of secondary categories detected.

    Returns:
        One of ``"express"``, ``"standard"``, or ``"custom"``.
    """
    if cross_cutting_count >= 2 or word_count > CUSTOM_WORD_THRESHOLD:
        return "custom"
    if word_count <= EXPRESS_WORD_THRESHOLD and cross_cutting_count == 0:
        return "express"
    return "standard"


def get_goal_routing(goal: str, category: str = "") -> tuple:
    """Return ``(agent_type, mode, model_tier)`` for a goal string.

    When *category* is provided (e.g. from the intake form), it is used
    directly instead of re-classifying via keyword matching.  This respects
    the user's explicit category choice.

    Args:
        goal: The user's goal string.
        category: Explicit category override from the UI (optional).

    Returns:
        Tuple of ``(agent_type_str, mode_str, model_tier_str)``.
    """
    resolved = category.strip() if category else ""
    if not resolved or resolved not in _GOAL_ROUTING_TABLE:
        detail = classify_goal_detailed(goal)
        resolved = detail["category"]
        logger.info(
            "Goal classified: category=%s confidence=%.2f complexity=%s cross_cutting=%s",
            resolved,
            detail["confidence"],
            detail["complexity"],
            detail["cross_cutting"],
        )

        # Log classification decision to audit trail (US-023)
        try:
            from vetinari.audit import get_audit_logger

            get_audit_logger().log_decision(
                decision_type="tier_classification",
                choice=resolved,
                reasoning=(
                    f"Keyword classification with confidence {detail['confidence']:.2f}, "
                    f"complexity={detail['complexity']}"
                ),
                alternatives=detail.get("cross_cutting", []),
                context={
                    "confidence": detail["confidence"],
                    "complexity": detail["complexity"],
                    "goal_preview": goal[:120],
                },
            )
        except Exception:
            logger.warning("Audit logging failed during classification", exc_info=True)

    return _GOAL_ROUTING_TABLE.get(resolved, _GOAL_ROUTING_TABLE["general"])
