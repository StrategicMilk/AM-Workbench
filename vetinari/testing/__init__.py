"""Vetinari testing infrastructure — canary tests, benchmarks, and self-testing.

Provides canonical test suites that Vetinari runs against itself:
- Canary tests: fixed prompt/output pairs to detect model regression
- Evolved benchmarks: automatically reframed eval prompts
- Adversarial tests: prompt injection and edge-case probes
- Context window tests: effective vs declared context measurement
"""

from __future__ import annotations

from vetinari.testing.adversarial_tests import (
    AdversarialCase,
    AdversarialCaseResult,
    AdversarialReport,
    AdversarialTestRunner,
)

__all__ = [
    "AdversarialCase",
    "AdversarialCaseResult",
    "AdversarialReport",
    "AdversarialTestRunner",
]
