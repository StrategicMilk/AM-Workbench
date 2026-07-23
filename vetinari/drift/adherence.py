"""Goal adherence compatibility checks."""

from __future__ import annotations

import re

_TOKEN_RE = re.compile(r"[A-Za-z0-9_]+")
_STOPWORDS = {
    "a",
    "an",
    "and",
    "are",
    "as",
    "for",
    "in",
    "is",
    "of",
    "or",
    "the",
    "to",
}


def _keywords(text: str) -> set[str]:
    return {token.lower() for token in _TOKEN_RE.findall(text) if token.lower() not in _STOPWORDS}


def check_goal_adherence(goal: str, plan: str) -> dict[str, float | str]:
    """Score whether a plan appears aligned with a goal.

    This is a fast heuristic that uses keyword token overlap only. It does
    not perform embedding-based semantic comparison, structural alignment, or
    intent matching beyond shared non-stopword tokens.

    Args:
        goal: Goal text.
        plan: Plan text.

    Returns:
        Numeric adherence score and description.
    """
    goal_terms = _keywords(goal)
    plan_terms = _keywords(plan)
    if not goal_terms or not plan_terms:
        return {"score": 0.0, "description": "missing goal or plan content"}

    matched = goal_terms & plan_terms
    missing = sorted(goal_terms - plan_terms)
    score = round(len(matched) / len(goal_terms), 3)
    if missing:
        description = f"matched {len(matched)}/{len(goal_terms)} goal terms; missing: {', '.join(missing[:8])}"
    else:
        description = f"matched all {len(goal_terms)} goal terms"
    return {"score": score, "description": description}


__all__ = ["check_goal_adherence"]
