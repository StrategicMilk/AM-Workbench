"""Trace-to-eval core-loop public surface."""

from __future__ import annotations

from vetinari.workbench.trace_eval_core.case import (
    CoreLoopEventKind,
    EvalCaseProvenance,
    EvalCaseRecord,
    EvalCaseRecordError,
    ReplayCommand,
    ReplayCommandError,
)
from vetinari.workbench.trace_eval_core.consumer_feed import ConsumerFeedRegistry, record_eval_case
from vetinari.workbench.trace_eval_core.eval_suite_runner import EvalCallable, EvalSuiteRunner
from vetinari.workbench.trace_eval_core.promoter import EvalCasePromoter, EvalCasePromoterError
from vetinari.workbench.trace_eval_core.store import EvalCaseStore, EvalCaseStoreError

__all__ = [
    "ConsumerFeedRegistry",
    "CoreLoopEventKind",
    "EvalCallable",
    "EvalCasePromoter",
    "EvalCasePromoterError",
    "EvalCaseProvenance",
    "EvalCaseRecord",
    "EvalCaseRecordError",
    "EvalCaseStore",
    "EvalCaseStoreError",
    "EvalSuiteRunner",
    "ReplayCommand",
    "ReplayCommandError",
    "record_eval_case",
]
