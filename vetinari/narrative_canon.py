"""Validation helpers for the AM Workbench narrative canon."""

from __future__ import annotations

REQUIRED_FRONTMATTER = {
    "schema_version: 1",
    "document: am-workbench-narrative-canon",
    "content_owner: user",
    "audit_consumers: read-only",
}

REQUIRED_SECTIONS = [
    "## Ownership Header",
    "## Origin",
    "## Mission",
    "## Voice",
    "## Audience",
    "## Anti-Persona",
    "## Narrative Pillars",
    "## Non-Claims",
    "## Read-Only Audit Use",
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


def validate_narrative(text: str) -> list[str]:
    """Return fail-closed validation errors for the narrative canon document.

    Args:
        text: Narrative markdown text.

    Returns:
        Validation error strings. An empty list means the document passes.
    """
    errors: list[str] = []
    if not text.startswith("---\n"):
        errors.append("missing frontmatter")
    frontmatter = text.split("---", 2)[1] if text.startswith("---\n") else ""
    errors.extend(f"missing frontmatter row: {row}" for row in REQUIRED_FRONTMATTER if row not in frontmatter)
    for heading in REQUIRED_SECTIONS:
        body = section_body(text, heading)
        if not body:
            errors.append(f"missing or empty section: {heading}")
    if "user owns the narrative content" not in text:
        errors.append("missing user authorship declaration")
    if "read-only consumer" not in text:
        errors.append("missing read-only audit consumer declaration")
    if "| Pillar | Meaning | Evidence expectation |" not in text:
        errors.append("missing narrative pillars table")
    return errors
