"""Eval suite runner — dispatches a registry of named eval callables over a payload.

Used by experiments and golden-eval suites to fan a single trace payload into
multiple named evaluators (correctness checks, regression checks, scoring
heuristics) and collect their results in one structure.  A failing evaluator
records ``{"status": "failed", "error": ...}`` rather than aborting the suite,
so one bad evaluator does not hide the verdicts of the others.
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Mapping
from typing import Any

logger = logging.getLogger(__name__)

EvalCallable = Callable[[Mapping[str, Any]], Any]


class EvalSuiteRunner:
    """Run a registry of named evaluators against a shared payload.

    Args:
        runners: Initial ``{name: callable}`` mapping.  Each callable receives
            the payload and returns a JSON-serialisable result.  Callables may
            be added later with :py:meth:`register`.
    """

    def __init__(self, runners: Mapping[str, EvalCallable] | None = None) -> None:
        self._runners: dict[str, EvalCallable] = dict(runners or {})

    def register(self, name: str, runner: EvalCallable) -> None:
        """Register an evaluator under ``name``.

        Args:
            name: Suite-unique identifier shown in the result dict.
            runner: Callable invoked as ``runner(payload)``.

        Raises:
            ValueError: If ``name`` is empty.
        """
        if not isinstance(name, str) or not name.strip():
            raise ValueError("eval runner name must be a non-empty string")
        self._runners[name] = runner

    def run(self, payload: Mapping[str, Any]) -> dict[str, Any]:
        """Invoke every registered evaluator against ``payload``.

        Args:
            payload: Shared input passed to each evaluator.

        Returns:
            ``{name: result}`` for each registered evaluator.  Failing
            evaluators contribute ``{name: {"status": "failed", "error": str}}``
            so the suite never aborts mid-run.
        """
        results: dict[str, Any] = {}
        for name, runner in self._runners.items():
            try:
                results[name] = runner(payload)
            except Exception as exc:
                logger.warning("Eval suite runner %r raised during run; recording failure status", name, exc_info=True)
                results[name] = {"status": "failed", "error": str(exc)}
        return results


__all__ = ["EvalCallable", "EvalSuiteRunner"]
