"""Decision tree extraction — identifies embedded decisions in goals before decomposition.

When a goal contains implicit choices (e.g., "build a web app" implies decisions about
framework, database, auth strategy), this module extracts those decisions as explicit
DecisionNode objects.  Resolved decisions flow into task decomposition as context;
unresolved decisions block generation until approved.

This is pipeline step 1.5: Goal -> **Decision Extraction** -> Task Decomposition -> Execution.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from typing import Any

logger = logging.getLogger(__name__)


# -- Constants ----------------------------------------------------------------

# Confidence threshold above which decisions are auto-resolved when care_level="auto"
_AUTO_RESOLVE_THRESHOLD = 0.8

# Domain patterns for keyword-based fallback extraction
_DOMAIN_PATTERNS: dict[str, tuple[str, ...]] = {
    "frontend": ("web app", "website", "frontend", "ui", "interface", "dashboard", "spa"),
    "database": ("database", "data store", "persist", "storage", "sql", "nosql", "schema"),
    "auth": ("auth", "login", "user", "session", "oauth", "sso", "credential"),
    "deployment": ("deploy", "host", "cloud", "server", "infrastructure", "container", "docker"),
    "testing": ("test", "testing", "qa", "quality", "ci", "continuous integration"),
    "api": ("api", "rest", "graphql", "endpoint", "service", "microservice"),
}

# Default options for common domains when no LLM is available
_DEFAULT_OPTIONS: dict[str, list[dict[str, Any]]] = {
    "frontend": [
        {
            "name": "React",
            "description": "Component-based UI library",
            "trade_offs": "Large ecosystem, JSX learning curve",
            "recommended": True,
        },
        {
            "name": "Vue",
            "description": "Progressive framework",
            "trade_offs": "Simpler API, smaller ecosystem",
            "recommended": False,
        },
        {
            "name": "Plain HTML/CSS",
            "description": "No framework",
            "trade_offs": "Simple but limited interactivity",
            "recommended": False,
        },
    ],
    "database": [
        {
            "name": "SQLite",
            "description": "Embedded relational database",
            "trade_offs": "Simple, no server needed, limited concurrency",
            "recommended": True,
        },
        {
            "name": "PostgreSQL",
            "description": "Full-featured relational database",
            "trade_offs": "Powerful, requires server setup",
            "recommended": False,
        },
        {
            "name": "JSON files",
            "description": "File-based storage",
            "trade_offs": "Simplest, no query capability",
            "recommended": False,
        },
    ],
    "auth": [
        {
            "name": "Session-based",
            "description": "Server-side sessions with cookies",
            "trade_offs": "Simple, stateful server",
            "recommended": True,
        },
        {
            "name": "JWT tokens",
            "description": "Stateless token authentication",
            "trade_offs": "Stateless, harder to revoke",
            "recommended": False,
        },
        {
            "name": "OAuth2",
            "description": "Delegated authentication",
            "trade_offs": "Industry standard, complex setup",
            "recommended": False,
        },
    ],
    "deployment": [
        {
            "name": "Local",
            "description": "Run on local machine",
            "trade_offs": "Simplest, not production-ready",
            "recommended": True,
        },
        {
            "name": "Docker",
            "description": "Container-based deployment",
            "trade_offs": "Portable, requires Docker knowledge",
            "recommended": False,
        },
        {
            "name": "Cloud PaaS",
            "description": "Managed platform (Heroku, Railway)",
            "trade_offs": "Easy deployment, vendor lock-in",
            "recommended": False,
        },
    ],
}


# -- Data classes -------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Option:
    """A possible choice for a decision point.

    Attributes:
        name: Short identifier for this option (e.g., "React", "PostgreSQL").
        description: What choosing this option entails.
        trade_offs: Concise pros and cons of this choice.
        recommended: Whether this is the suggested default when auto-resolving.
    """

    name: str
    description: str
    trade_offs: str
    recommended: bool = False

    def __repr__(self) -> str:
        return f"Option(name={self.name!r}, recommended={self.recommended!r})"


@dataclass(slots=True)
class DecisionNode:
    """An explicit decision point extracted from a goal.

    Mutable so that ``resolution`` and ``confidence`` can be updated after
    extraction (e.g., by auto-resolution or user approval).

    Attributes:
        question: The decision question (e.g., "Which frontend framework?").
        domain: Problem domain this decision belongs to (e.g., "frontend", "database").
        options: Available choices with descriptions and trade-offs.
        resolution: Name of the chosen option, or None if unresolved.
        confidence: How confident the auto-resolver is in the recommended choice (0.0-1.0).
        confidence_source: Provenance for the confidence value.
        auto_resolved: Whether this decision was resolved automatically vs. by user.
    """

    question: str
    domain: str
    options: list[Option] = field(default_factory=list)
    resolution: str | None = None
    confidence: float = 0.0
    confidence_source: str = "unknown"
    auto_resolved: bool = False

    def __repr__(self) -> str:
        """Show key decision fields without dumping all options."""
        status = f"resolved={self.resolution!r}" if self.resolution else "UNRESOLVED"
        return f"DecisionNode(domain={self.domain!r}, {status}, confidence={self.confidence:.2f})"


@dataclass(frozen=True, slots=True)
class DecisionTreeResult:
    """Aggregated result from decision extraction.

    Attributes:
        decisions: All extracted decision nodes.
        all_resolved: True only when every decision has a non-None resolution.
        resolved_context: Mapping of domain -> resolution for resolved decisions.
        blocking_decisions: Unresolved decisions that prevent decomposition.
    """

    decisions: tuple[DecisionNode, ...]
    all_resolved: bool
    resolved_context: dict[str, str]
    blocking_decisions: tuple[DecisionNode, ...]

    def __repr__(self) -> str:
        return (
            f"DecisionTreeResult(all_resolved={self.all_resolved!r}, "
            f"decisions={len(self.decisions)}, "
            f"blocking={len(self.blocking_decisions)})"
        )


# -- Private helpers ----------------------------------------------------------


def _extract_decisions_llm(goal: str) -> list[DecisionNode] | None:
    """Use cascade router to extract decisions via LLM.

    Returns None when the LLM is unavailable, signaling the caller to
    fall back to keyword extraction.

    Args:
        goal: The user goal to analyze.

    Returns:
        List of DecisionNode objects, or None if LLM is unavailable.
    """
    try:
        # Fix: import path corrected from vetinari.inference.cascade_router to vetinari.cascade_router (FSA-0516)
        from vetinari.cascade_router import get_cascade_router

        router = get_cascade_router()
    except Exception:
        logger.warning("Cascade router unavailable — falling back to keyword extraction")
        return None

    prompt = f"""Analyze this goal and identify any embedded decisions that need to be made
before implementation can begin. For each decision, provide:
- question: What needs to be decided?
- domain: Category (frontend, database, auth, deployment, testing, api, other)
- options: List of 2-4 options, each with name, description, trade_offs, and recommended (bool)
- confidence: How confident you are in the recommended option (0.0-1.0)

Goal: {goal}

Return a JSON array of decision objects. If no decisions are needed, return [].
Example: [{{"question": "Which database?", "domain": "database", "options": [...], "confidence": 0.9}}]"""

    try:
        result = router.route(prompt, task_type="planning")
        if not result:
            return None

        response_text = result if isinstance(result, str) else str(result)

        # Parse JSON from response
        parsed = json.loads(response_text)
        if not isinstance(parsed, list):
            return None

        decisions: list[DecisionNode] = []
        for item in parsed:
            if not isinstance(item, dict):
                continue
            options = [
                Option(
                    name=opt.get("name", "Unknown"),
                    description=opt.get("description", ""),
                    trade_offs=opt.get("trade_offs", ""),
                    recommended=bool(opt.get("recommended", False)),
                )
                for opt in item.get("options", [])
                if isinstance(opt, dict)
            ]
            decisions.append(
                DecisionNode(
                    question=item.get("question", ""),
                    domain=item.get("domain", "other"),
                    options=options,
                    confidence=float(item.get("confidence", 0.5)),
                    confidence_source="llm_uncalibrated",
                )
            )
        return decisions
    except (json.JSONDecodeError, TypeError, ValueError):
        logger.warning("Failed to parse LLM decision extraction response")
        return None
    except Exception:
        logger.warning("LLM decision extraction failed — using keyword fallback", exc_info=True)
        return None


def _extract_decisions_keyword(goal: str) -> list[DecisionNode]:
    """Keyword-based fallback for decision extraction.

    Scans the goal for domain keywords and generates default decision nodes
    with pre-built option lists. Less accurate than LLM but always available.

    Args:
        goal: The user goal to analyze.

    Returns:
        List of DecisionNode objects for detected domains.
    """
    goal_lower = goal.lower()
    decisions: list[DecisionNode] = []

    for domain, keywords in _DOMAIN_PATTERNS.items():
        if any(kw in goal_lower for kw in keywords):
            default_opts = _DEFAULT_OPTIONS.get(domain, [])
            options = [
                Option(
                    name=opt["name"],
                    description=opt["description"],
                    trade_offs=opt["trade_offs"],
                    recommended=opt.get("recommended", False),
                )
                for opt in default_opts
            ]

            # Find recommended option's confidence
            has_recommended = any(o.recommended for o in options)
            confidence = 0.7 if has_recommended else 0.3

            question_map = {
                "frontend": "Which frontend framework should be used?",
                "database": "Which database technology should be used?",
                "auth": "Which authentication strategy should be used?",
                "deployment": "Which deployment strategy should be used?",
                "testing": "Which testing strategy should be used?",
                "api": "Which API style should be used?",
            }

            decisions.append(
                DecisionNode(
                    question=question_map.get(domain, f"What approach for {domain}?"),
                    domain=domain,
                    options=options,
                    confidence=confidence,
                    confidence_source="heuristic",
                )
            )

    return decisions


# -- Public API ---------------------------------------------------------------


def extract_decisions(
    goal: str,
    domain_care_levels: dict[str, str] | None = None,
) -> DecisionTreeResult:
    """Extract embedded decisions from a goal and auto-resolve where appropriate.

    First attempts LLM-based extraction via the cascade router. Falls back
    to keyword-based extraction when the LLM is unavailable. After extraction,
    auto-resolves decisions where the domain care level is "auto" and
    confidence exceeds the threshold.

    Args:
        goal: The user goal string to analyze for embedded decisions.
        domain_care_levels: Maps domain names to care levels:
            - "auto": Auto-resolve when confidence >= threshold (default)
            - "review": Require human review before proceeding
            - "manual": Always require explicit user choice
            Defaults to all "auto" when None.

    Returns:
        DecisionTreeResult with extracted decisions, resolution status,
        and blocking decisions that prevent decomposition.
    """
    if domain_care_levels is None:
        domain_care_levels = {}

    # Try LLM extraction first, fall back to keywords
    decisions = _extract_decisions_llm(goal)
    if decisions is None:
        logger.info("Using keyword-based decision extraction for goal")
        decisions = _extract_decisions_keyword(goal)

    if not decisions:
        return DecisionTreeResult(
            decisions=(),
            all_resolved=True,
            resolved_context={},
            blocking_decisions=(),
        )

    # Auto-resolve eligible decisions
    decisions = auto_resolve_decisions(decisions, domain_care_levels)

    # Build result
    resolved_context = get_resolved_context(decisions)
    blocking = [d for d in decisions if d.resolution is None]

    return DecisionTreeResult(
        decisions=tuple(decisions),
        all_resolved=len(blocking) == 0,
        resolved_context=resolved_context,
        blocking_decisions=tuple(blocking),
    )


class DecisionTreeExtractor:
    """Compatibility wrapper exposing decision extraction as an object."""

    def extract(
        self,
        goal: str,
        domain_care_levels: dict[str, str] | None = None,
    ) -> DecisionTreeResult:
        return extract_decisions(goal, domain_care_levels)


def auto_resolve_decisions(
    decisions: list[DecisionNode],
    care_levels: dict[str, str],
) -> list[DecisionNode]:
    """Auto-resolve decisions where care level is "auto" and confidence is high enough.

    For each decision: if the domain's care level is "auto" (or defaulted) and
    the confidence is >= _AUTO_RESOLVE_THRESHOLD, set the resolution to the
    recommended option's name.

    Args:
        decisions: List of extracted decisions to potentially resolve.
        care_levels: Domain-to-care-level mapping ("auto", "review", "manual").

    Returns:
        The same list with eligible decisions resolved in-place.
    """
    for decision in decisions:
        if decision.resolution is not None:
            continue  # Already resolved

        care_level = care_levels.get(decision.domain, "auto")

        confidence_calibrated = decision.confidence_source != "llm_uncalibrated"
        if care_level == "auto" and confidence_calibrated and decision.confidence >= _AUTO_RESOLVE_THRESHOLD:
            recommended = next((o for o in decision.options if o.recommended), None)
            if recommended:
                decision.resolution = recommended.name
                decision.auto_resolved = True
                logger.info(
                    "Auto-resolved decision '%s' -> '%s' (confidence=%.2f)",
                    decision.question,
                    recommended.name,
                    decision.confidence,
                )
        elif care_level == "auto" and not confidence_calibrated:
            logger.info(
                "Decision '%s' requires review because confidence source %s is not calibrated",
                decision.question,
                decision.confidence_source,
            )
        elif care_level in ("review", "manual"):
            logger.info(
                "Decision '%s' requires %s — blocking decomposition",
                decision.question,
                care_level,
            )

    return decisions


def get_resolved_context(decisions: list[DecisionNode]) -> dict[str, str]:
    """Build a domain-to-resolution mapping from resolved decisions.

    Args:
        decisions: List of decisions (resolved and unresolved).

    Returns:
        Dictionary mapping domain names to their chosen resolution strings.
        Only includes decisions that have been resolved.
    """
    return {d.domain: d.resolution for d in decisions if d.resolution is not None}


def enrich_context_with_decisions(
    goal: str,
    context: dict[str, Any],
    domain_care_levels: dict[str, str] | None = None,
) -> dict[str, Any]:
    """Extract decisions from a goal and merge resolved ones into decomposition context.

    Intended to be called before ``decompose_goal_llm()`` so that resolved
    decisions are available as context for task decomposition.

    Args:
        goal: The user goal string.
        context: Existing context dict to enrich (not modified in place).
        domain_care_levels: Optional domain care level overrides.

    Returns:
        New context dict with ``_decisions`` and ``_resolved_decisions`` keys added.
        If decisions are blocking, ``_blocking_decisions`` is also set.
    """
    result = extract_decisions(goal, domain_care_levels)

    enriched = dict(context)
    enriched["_decisions"] = [
        {
            "question": d.question,
            "domain": d.domain,
            "resolution": d.resolution,
            "confidence": d.confidence,
            "auto_resolved": d.auto_resolved,
        }
        for d in result.decisions
    ]
    enriched["_resolved_decisions"] = result.resolved_context

    if result.blocking_decisions:
        enriched["_blocking_decisions"] = [
            {"question": d.question, "domain": d.domain} for d in result.blocking_decisions
        ]

    return enriched
