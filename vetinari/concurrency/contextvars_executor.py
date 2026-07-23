"""Executor helpers that preserve ``contextvars`` across thread boundaries."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from concurrent.futures import Executor, Future
from contextvars import copy_context
from functools import partial
from typing import Any, TypeVar

from vetinari.exceptions import MissingCorrelationContext
from vetinari.logging_context import get_correlation_ids, run_with_correlation_ids

T = TypeVar("T")
REQUIRED_CORRELATION_NAMES = frozenset({"trace_id", "span_id"})


def _raise_if_missing_required(correlation_ids: dict[str, str | None], required: frozenset[str]) -> None:
    missing = sorted(name for name in required if correlation_ids.get(name) is None)
    if missing:
        joined = ", ".join(missing)
        raise MissingCorrelationContext(f"missing required correlation ContextVar(s): {joined}")


def submit_with_context(
    executor: Executor,
    fn: Callable[..., T],
    *args: Any,
    require_correlation: bool = True,
    **kwargs: Any,
) -> Future[T]:
    """Submit ``fn`` with the caller's Vetinari correlation ContextVars.

    Args:
        executor: Executor that will run the callable.
        fn: Callable to execute.
        *args: Positional arguments for ``fn``.
        require_correlation: Whether trace/span IDs must be present.
        **kwargs: Keyword arguments for ``fn``.

    Returns:
        Future for the submitted callable.
    """
    correlation_ids = copy_context().run(get_correlation_ids)
    if require_correlation:
        _raise_if_missing_required(correlation_ids, REQUIRED_CORRELATION_NAMES)
    return executor.submit(partial(run_with_correlation_ids, correlation_ids, fn, *args, **kwargs))


def run_in_executor_with_context(
    loop: asyncio.AbstractEventLoop,
    executor: Executor | None,
    fn: Callable[..., T],
    *args: Any,
    require_correlation: bool = True,
) -> asyncio.Future[T]:
    """Run ``fn`` in an executor with the caller's Vetinari correlation ContextVars.

    Args:
        loop: Event loop used to schedule executor work.
        executor: Executor to run in, or ``None`` for the loop default.
        fn: Callable to execute.
        *args: Positional arguments for ``fn``.
        require_correlation: Whether trace/span IDs must be present.

    Returns:
        Future for the executor call.
    """
    correlation_ids = copy_context().run(get_correlation_ids)
    if require_correlation:
        _raise_if_missing_required(correlation_ids, REQUIRED_CORRELATION_NAMES)
    return loop.run_in_executor(executor, partial(run_with_correlation_ids, correlation_ids, fn, *args))
