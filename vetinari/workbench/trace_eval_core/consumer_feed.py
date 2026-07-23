"""Read-only consumer feed registry for core-loop eval cases.

This pack provides the read API (``snapshot_for_target`` and ``snapshot_all``).
Consuming packs for model routing, prompt promotion, red-team fixtures,
benchmark import, failure intelligence, and automation approval gates own their
subscribers and invocation timing. This module does not register routes, hooks,
scheduler subscriptions, background polling loops, file handles, or sockets.
"""

from __future__ import annotations

from vetinari.workbench.trace_eval_core.case import _FEED_TARGETS, CoreLoopEventKind, EvalCaseRecord
from vetinari.workbench.trace_eval_core.promoter import EvalCasePromoter
from vetinari.workbench.trace_eval_core.store import EvalCaseStore


class ConsumerFeedRegistry:
    """Read eval cases grouped by downstream consumer feed."""

    def __init__(self, *, store: EvalCaseStore) -> None:
        self._store = store

    def snapshot_for_target(self, target: str) -> tuple[EvalCaseRecord, ...]:
        return self._store.list_by_consumer_feed_target(target)

    def snapshot_all(self) -> dict[str, tuple[EvalCaseRecord, ...]]:
        return {target: self.snapshot_for_target(target) for target in sorted(_FEED_TARGETS)}

    def count_by_event_kind(self) -> dict[str, int]:
        """Execute the count by event kind operation.

        Returns:
            dict[str, int] value produced by count_by_event_kind().
        """
        counts = {event_kind.value: 0 for event_kind in CoreLoopEventKind}
        for record in self._store.read_all():
            counts[record.provenance.source_event_kind.value] += 1
        return counts


def record_eval_case(
    promoter: EvalCasePromoter,
    store: EvalCaseStore,
    record: EvalCaseRecord,
) -> EvalCaseRecord:
    """Append a promoter-produced record through the canonical wiring helper.

    Args:
        promoter: Promoter value consumed by record_eval_case().
        store: Store value consumed by record_eval_case().
        record: Typed record consumed by the operation.

    Returns:
        Outcome produced by record_eval_case().

    Raises:
        Exception: Propagates validation or runtime failures from the underlying operation.
    """
    if not isinstance(promoter, EvalCasePromoter):
        raise TypeError("promoter must be EvalCasePromoter")
    return store.append(record)


__all__ = ["ConsumerFeedRegistry", "record_eval_case"]
