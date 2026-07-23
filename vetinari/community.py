"""Public community resource directory — printed by `python -m vetinari community`.

This module is a CLI-printing utility, not part of the inference pipeline.
`print()` is intentional here: the caller is always a human reading a terminal.
"""

from __future__ import annotations

import logging
import sys
from dataclasses import dataclass

logger = logging.getLogger(__name__)

# -- Community resource catalog --
# Written once at module load; read by print_community_resources() and any
# other consumer that wants the canonical URL list. No lock needed — frozen
# dataclasses in a module-level tuple are effectively immutable after import.


@dataclass(frozen=True, slots=True)
class CommunityResource:
    """A single community touchpoint with a name, URL, and one-line purpose."""

    name: str  # Human-readable display name
    url: str  # Canonical URL for this resource
    purpose: str  # One-line description of what the resource is for


# Canonical list of community resources for AM Workbench.
# To add a new entry: append a CommunityResource here.
# Consumers: print_community_resources() (CLI) and any docs generator.
COMMUNITY_RESOURCES: tuple[CommunityResource, ...] = (
    CommunityResource(
        "GitHub Discussions",
        "https://github.com/StrategicMilk/AM-Workbench/discussions",
        "Q&A, ideas, showcase",
    ),
    CommunityResource(
        "Issue tracker",
        "https://github.com/StrategicMilk/AM-Workbench/issues",
        "Bug reports and feature requests",
    ),
    CommunityResource(
        "Showcase template",
        "https://github.com/StrategicMilk/AM-Workbench/discussions/new?category=showcase",
        "Share a workflow",
    ),
    CommunityResource(
        "Contributing guide",
        "https://github.com/StrategicMilk/AM-Workbench/blob/main/CONTRIBUTING.md",
        "How to contribute code",
    ),
)


def print_community_resources() -> None:
    """Print every community resource to stdout for the CLI `vetinari community` command.

    Outputs one line per resource in the format:
        <name>: <url>  - <purpose>

    This function is the sole exit point for the `community` subcommand in
    `vetinari/__main__.py`. It never writes to the logger; the audience is the
    human running the CLI, not a log aggregator.
    """
    for r in COMMUNITY_RESOURCES:
        sys.stdout.write(f"{r.name}: {r.url}  - {r.purpose}\n")
