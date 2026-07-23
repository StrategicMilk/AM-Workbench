"""Validation helpers for the internal AM Workbench voice guide."""

from __future__ import annotations

REQUIRED_SECTIONS = [
    "## Tone",
    "## Register",
    "## Microcopy Matrix",
    "## Do And Dont Examples",
    "## Declarative Vs Imperative",
    "## Error Messages",
    "## Empty States",
    "## Progress Messages",
    "## Default Button Voice",
]

REQUIRED_PHRASES = [
    "factual, calm, specific, and evidence-driven",
    "professional and operational",
    "What failed.",
    "What is absent and what creates the first item",
    "verb-object labels",
]

FORBIDDEN_OPERATOR_COPY = [
    "Almost there",
    "Something went wrong.",
]


def section_body(text: str, heading: str) -> str:
    """Return the markdown body under a second-level heading.

    Args:
        text: Markdown document text.
        heading: Heading marker including the leading ``##``.

    Returns:
        Section body text, or an empty string when the heading is absent.
    """
    marker = f"{heading}\n"
    start = text.find(marker)
    if start == -1:
        return ""
    start += len(marker)
    next_heading = text.find("\n## ", start)
    if next_heading == -1:
        next_heading = len(text)
    return text[start:next_heading].strip()


def validate_voice_guide(text: str) -> list[str]:
    """Return fail-closed validation errors for the voice guide.

    Args:
        text: Voice-guide markdown text.

    Returns:
        Validation error strings. An empty list means the guide passes.
    """
    errors: list[str] = []
    for heading in REQUIRED_SECTIONS:
        body = section_body(text, heading)
        if not body:
            errors.append(f"missing or empty section: {heading}")
    errors.extend(f"missing required phrase: {phrase}" for phrase in REQUIRED_PHRASES if phrase not in text)
    if "| Do | Dont | Reason |" not in text:
        errors.append("missing do/dont examples table")
    if "| Situation | Pattern | Example |" not in text:
        errors.append("missing microcopy matrix table")
    tone_body = section_body(text, "## Tone")
    errors.extend(
        f"forbidden vague operator copy in tone section: {forbidden}"
        for forbidden in FORBIDDEN_OPERATOR_COPY
        if forbidden in tone_body
    )
    return errors
