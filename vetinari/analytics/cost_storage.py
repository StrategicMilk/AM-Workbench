"""Cost analytics persistence and correlation helpers."""

from __future__ import annotations

import hashlib
import json
import logging
import math
import os
import time
from collections import deque
from collections.abc import Sequence
from dataclasses import dataclass
from importlib import import_module
from importlib.util import find_spec
from pathlib import Path
from typing import Any

from vetinari.analytics._private_jsonl import (
    _bounded_jsonl_paths,
    _cost_ledger_transaction,
    _ensure_private_parent,
    _open_private_append,
    _open_private_read,
    _rewrite_compacted_ledgers,
    _rotate_jsonl_if_needed,
)
from vetinari.analytics.cost_models import CostEntry
from vetinari.constants import get_user_dir
from vetinari.logging_context import get_correlation_ids

logger = logging.getLogger(__name__)
_COST_ENTRY_SCHEMA_VERSION = 2
_LEGACY_COST_ENTRY_SCHEMA_VERSION = 1
_COST_PERSISTENCE_MAX_BYTES = 5 * 1024 * 1024
_COST_PERSISTENCE_BACKUP_COUNT = 3
_COST_RETENTION_DAYS = 30
_COST_COMPACTION_INTERVAL_SECONDS = 6 * 60 * 60
_CORRELATION_REF_DIGEST_BYTES = 16
_DEFAULT_COST_BUDGET_USD = 100.0
_NEXT_COMPACTION_BY_PATH: dict[Path, float] = {}


def _load_genai_observability() -> Any | None:
    """Load the project-owned GenAI observability module only when packaged."""
    module_name = "vetinari.observability.otel_genai"
    if find_spec(module_name) is None:
        return None
    return import_module(module_name)


@dataclass(frozen=True, slots=True)
class CostPersistenceConfig:
    """Resolved cost persistence settings."""

    entries_path: Path
    budget_alerts_path: Path
    max_bytes: int
    backup_count: int
    budget_limit_usd: float
    retention_days: int = _COST_RETENTION_DAYS
    compaction_interval_seconds: int = _COST_COMPACTION_INTERVAL_SECONDS

    def __repr__(self) -> str:
        return (
            f"{type(self).__name__}("
            f"entries_path={self.entries_path!r}, "
            f"budget_alerts_path={self.budget_alerts_path!r}, "
            f"max_bytes={self.max_bytes!r}, "
            f"backup_count={self.backup_count!r}, "
            f"retention_days={self.retention_days!r}, "
            f"compaction_interval_seconds={self.compaction_interval_seconds!r}"
            ")"
        )


@dataclass(frozen=True, slots=True)
class CostCompactionReceipt:
    """Auditable result of one cost-ledger retention pass."""

    scanned_records: int
    retained_records: int
    expired_records: int
    capacity_pruned_records: int
    files_rewritten: int
    cutoff_timestamp: float

    def __repr__(self) -> str:
        """Return a compact receipt representation for operator diagnostics."""
        return (
            f"{type(self).__name__}("
            f"scanned_records={self.scanned_records!r}, "
            f"retained_records={self.retained_records!r}, "
            f"expired_records={self.expired_records!r}, "
            f"capacity_pruned_records={self.capacity_pruned_records!r}, "
            f"files_rewritten={self.files_rewritten!r}, "
            f"cutoff_timestamp={self.cutoff_timestamp!r}"
            ")"
        )


def build_cost_persistence_config() -> CostPersistenceConfig:
    """Resolve cost persistence settings from environment and defaults.

    Returns:
        Immutable persistence configuration.
    """
    return CostPersistenceConfig(
        entries_path=_cost_entries_path(),
        budget_alerts_path=_cost_budget_alerts_path(),
        max_bytes=_positive_int_env("VETINARI_COST_PERSISTENCE_MAX_BYTES", _COST_PERSISTENCE_MAX_BYTES),
        backup_count=_positive_int_env("VETINARI_COST_PERSISTENCE_BACKUP_COUNT", _COST_PERSISTENCE_BACKUP_COUNT),
        budget_limit_usd=_non_negative_float_env("VETINARI_COST_BUDGET_USD", _DEFAULT_COST_BUDGET_USD),
        retention_days=_COST_RETENTION_DAYS,
        compaction_interval_seconds=_COST_COMPACTION_INTERVAL_SECONDS,
    )


def _positive_int_env(name: str, default: int) -> int:
    """Read a positive integer environment override.

    Args:
        name: Environment variable name.
        default: Value to use when unset.

    Returns:
        Positive integer value.
    """
    raw = os.environ.get(name)
    if raw is None:
        return default
    try:
        value = int(raw)
    except ValueError as exc:
        raise ValueError(f"{name} must be a positive integer") from exc
    if value <= 0:
        raise ValueError(f"{name} must be a positive integer")
    return value


def _non_negative_float_env(name: str, default: float) -> float:
    """Read a non-negative finite float environment override.

    Args:
        name: Environment variable name.
        default: Value to use when unset.

    Returns:
        Non-negative finite float value.
    """
    raw = os.environ.get(name)
    if raw is None:
        return default
    try:
        value = float(raw)
    except ValueError as exc:
        raise ValueError(f"{name} must be a non-negative finite number") from exc
    if value < 0.0 or not math.isfinite(value):
        raise ValueError(f"{name} must be a non-negative finite number")
    return value


def _cost_entries_path() -> Path:
    """Resolve the durable cost-entry JSONL path.

    Returns:
        Operator-owned path for persisted cost entries.
    """
    override = os.environ.get("VETINARI_COST_ENTRIES_PATH")
    if override:
        return Path(override)
    return Path(get_user_dir()) / "analytics" / "cost_entries.jsonl"


def _cost_budget_alerts_path() -> Path:
    """Resolve the durable cost-budget alert JSONL path.

    Returns:
        Operator-owned path for persisted budget alerts.
    """
    override = os.environ.get("VETINARI_COST_BUDGET_ALERTS_PATH")
    if override:
        return Path(override)
    return Path(get_user_dir()) / "analytics" / "cost_budget_alerts.jsonl"


def cost_entry_to_json(entry: CostEntry) -> dict[str, object]:
    """Convert a CostEntry into the persisted JSONL schema.

    Args:
        entry: Cost entry to serialize.

    Returns:
        JSON-serializable dictionary.
    """
    raw: dict[str, object] = dict(entry.to_dict())
    raw.pop("trace_id", None)
    raw.pop("span_id", None)
    correlation_ref = _correlation_ref(entry.trace_id, entry.span_id)
    if correlation_ref is not None:
        raw["correlation_ref"] = correlation_ref
    raw["schema_version"] = _COST_ENTRY_SCHEMA_VERSION
    return raw


def cost_entry_from_json(raw: dict[str, object]) -> CostEntry:
    """Rebuild a CostEntry from the persisted JSONL schema.

    Args:
        raw: Decoded JSON object.

    Returns:
        Rehydrated cost entry without durable raw correlation identifiers.

    Raises:
        KeyError: If a required provider or model field is missing.
        ValueError: If a field or schema version is invalid.
    """
    schema_version = _int_json_field(raw, "schema_version", _COST_ENTRY_SCHEMA_VERSION)
    if schema_version not in {_LEGACY_COST_ENTRY_SCHEMA_VERSION, _COST_ENTRY_SCHEMA_VERSION}:
        raise ValueError(f"unsupported cost entry schema_version {schema_version!r}")
    return CostEntry(
        provider=str(raw["provider"]),
        model=str(raw["model"]),
        input_tokens=_int_json_field(raw, "input_tokens", 0),
        output_tokens=_int_json_field(raw, "output_tokens", 0),
        agent=_optional_str(raw.get("agent")),
        task_id=_optional_str(raw.get("task_id")),
        project_id=_optional_str(raw.get("project_id")),
        trace_id=None,
        span_id=None,
        timestamp=_float_json_field(raw, "timestamp", time.time()),
        cost_usd=_float_json_field(raw, "cost_usd", 0.0) if raw.get("cost_usd") is not None else None,
        latency_ms=_float_json_field(raw, "latency_ms", 0.0),
    )


def _correlation_ref(trace_id: str | None, span_id: str | None) -> str | None:
    """Return a bounded one-way reference without persisting raw correlation IDs."""
    if not trace_id and not span_id:
        return None
    digest = hashlib.sha256()
    digest.update((trace_id or "").encode("utf-8"))
    digest.update(b"\x00")
    digest.update((span_id or "").encode("utf-8"))
    return f"corr-{digest.hexdigest()[: _CORRELATION_REF_DIGEST_BYTES * 2]}"


def _int_json_field(raw: dict[str, object], key: str, default: int) -> int:
    value = raw.get(key, default)
    if isinstance(value, (str, bytes, bytearray, int, float)):
        return int(value)
    raise ValueError(f"{key} must be integer-compatible")


def _float_json_field(raw: dict[str, object], key: str, default: float) -> float:
    value = raw.get(key, default)
    if isinstance(value, (str, bytes, bytearray, int, float)):
        return float(value)
    raise ValueError(f"{key} must be numeric")


def entry_with_correlation(entry: CostEntry) -> CostEntry:
    """Attach trace/span metadata to a cost entry when omitted.

    Args:
        entry: Original cost entry.

    Returns:
        Entry with correlation IDs when available.
    """
    if entry.trace_id and entry.span_id:
        return entry
    correlation_ids = _current_cost_correlation()
    trace_id = entry.trace_id or correlation_ids.get("trace_id")
    span_id = entry.span_id or correlation_ids.get("span_id")
    if trace_id == entry.trace_id and span_id == entry.span_id:
        return entry
    return CostEntry(
        provider=entry.provider,
        model=entry.model,
        input_tokens=entry.input_tokens,
        output_tokens=entry.output_tokens,
        agent=entry.agent,
        task_id=entry.task_id,
        project_id=entry.project_id,
        trace_id=trace_id,
        span_id=span_id,
        timestamp=entry.timestamp,
        cost_usd=entry.cost_usd,
        latency_ms=entry.latency_ms,
    )


def annotate_correlated_span(entry: CostEntry) -> None:
    """Attach recorded cost to the matching in-process GenAI span.

    Args:
        entry: Recorded cost entry.
    """
    if not entry.trace_id or not entry.span_id or entry.cost_usd is None:
        return
    observability = _load_genai_observability()
    if observability is None:
        logger.warning(
            "Cost span annotation skipped — observability.otel_genai unavailable "
            "(trace_id=%s span_id=%s); persisted cost remains available but span attribution is incomplete; "
            "cost stays in the private JSONL ledger only.",
            entry.trace_id,
            entry.span_id,
        )
        return
    observability._record_span_cost(entry.trace_id, entry.span_id, float(entry.cost_usd))


def load_persisted_cost_entries(
    entries: deque[CostEntry],
    path: Path,
    backup_count: int,
    *,
    max_bytes: int | None = None,
    retention_days: int | None = None,
) -> None:
    """Load persisted entries into the in-memory reporting window.

    Args:
        entries: Target in-memory bounded deque.
        path: Active JSONL path.
        backup_count: Number of rotated ledgers to read.
        max_bytes: Per-file byte bound used while compacting persisted rows.
        retention_days: Maximum age of detailed cost rows.

    Raises:
        ValueError: Propagated when validation, persistence, or execution fails.
        OSError: If an existing ledger is unsafe or cannot be compacted privately.
    """
    resolved_max_bytes = max_bytes or _positive_int_env(
        "VETINARI_COST_PERSISTENCE_MAX_BYTES",
        _COST_PERSISTENCE_MAX_BYTES,
    )
    resolved_retention_days = retention_days or _COST_RETENTION_DAYS
    _validate_compaction_limits(
        backup_count=backup_count,
        max_bytes=resolved_max_bytes,
        retention_days=resolved_retention_days,
    )
    with _cost_ledger_transaction(path):
        current_time = time.time()
        _compact_cost_ledgers_locked(
            path,
            backup_count=backup_count,
            max_bytes=resolved_max_bytes,
            retention_days=resolved_retention_days,
            current_time=current_time,
        )
        _NEXT_COMPACTION_BY_PATH[path.resolve(strict=False)] = current_time + _COST_COMPACTION_INTERVAL_SECONDS
        for ledger_path in _bounded_jsonl_paths(path, backup_count=backup_count):
            with _open_private_read(ledger_path) as fh:
                for line_no, raw_line in enumerate(fh, start=1):
                    stripped = raw_line.strip()
                    if not stripped:
                        continue
                    try:
                        raw = json.loads(stripped)
                        if not isinstance(raw, dict):
                            raise TypeError("cost entry must be a JSON object")
                        entries.append(cost_entry_from_json(raw))
                    except (json.JSONDecodeError, KeyError, TypeError, ValueError) as exc:
                        raise ValueError(f"Malformed cost entry on line {line_no} of {ledger_path}") from exc


def _compact_cost_ledgers(
    path: Path,
    *,
    backup_count: int,
    max_bytes: int,
    retention_days: int,
    now: float | None = None,
) -> CostCompactionReceipt:
    """Prune expired cost rows and compact survivors into bounded private ledgers.

    Legacy schema-v1 rows are privacy-migrated during compaction: raw trace and
    span identifiers are removed and replaced by a bounded one-way correlation
    reference. The oldest rows beyond the configured rotated-file capacity are
    also removed so compaction cannot defeat the existing byte bound.

    Args:
        path: Active cost JSONL path.
        backup_count: Number of rotated backup files retained beside the active file.
        max_bytes: Maximum encoded bytes per active or rotated file.
        retention_days: Maximum age of detailed rows in days.
        now: Optional Unix timestamp for deterministic retention decisions.

    Returns:
        Structured receipt describing the retention and compaction result.

    Raises:
        ValueError: If limits or an existing ledger row are malformed.
        OSError: If private-file verification or durable replacement fails.
    """
    _validate_compaction_limits(backup_count=backup_count, max_bytes=max_bytes, retention_days=retention_days)
    current_time = time.time() if now is None else now
    if not math.isfinite(current_time) or current_time < 0.0:
        raise ValueError("cost compaction timestamp must be finite and non-negative")
    with _cost_ledger_transaction(path):
        return _compact_cost_ledgers_locked(
            path,
            backup_count=backup_count,
            max_bytes=max_bytes,
            retention_days=retention_days,
            current_time=current_time,
        )


def _validate_compaction_limits(*, backup_count: int, max_bytes: int, retention_days: int) -> None:
    """Reject retention settings that cannot preserve bounded private storage."""
    if backup_count < 0 or max_bytes <= 0 or retention_days <= 0:
        raise ValueError("cost compaction limits must be non-negative backups and positive byte/day bounds")


def _compact_cost_ledgers_locked(
    path: Path,
    *,
    backup_count: int,
    max_bytes: int,
    retention_days: int,
    current_time: float,
) -> CostCompactionReceipt:
    """Compact ledgers while the path's process and cross-process locks are held."""
    cutoff = current_time - (retention_days * 86_400)
    retained_lines: list[str] = []
    scanned_records = 0
    expired_records = 0
    _ensure_private_parent(path)
    existing_paths = _bounded_jsonl_paths(path, backup_count=backup_count)
    for ledger_path in existing_paths:
        with _open_private_read(ledger_path) as fh:
            for line_no, raw_line in enumerate(fh, start=1):
                if not raw_line.strip():
                    continue
                scanned_records += 1
                try:
                    raw = json.loads(raw_line)
                    if not isinstance(raw, dict):
                        raise TypeError("cost ledger row must be a JSON object")
                    schema_version = _int_json_field(
                        raw,
                        "schema_version",
                        _LEGACY_COST_ENTRY_SCHEMA_VERSION,
                    )
                    if schema_version not in {
                        _LEGACY_COST_ENTRY_SCHEMA_VERSION,
                        _COST_ENTRY_SCHEMA_VERSION,
                    }:
                        raise ValueError(f"unsupported cost entry schema_version {schema_version!r}")
                    timestamp = _float_json_field(raw, "timestamp", 0.0)
                    if not math.isfinite(timestamp) or timestamp < 0.0:
                        raise ValueError("timestamp must be finite and non-negative")
                except (json.JSONDecodeError, TypeError, ValueError) as exc:
                    raise ValueError(f"Malformed cost entry on line {line_no} of {ledger_path}") from exc
                if timestamp < cutoff:
                    expired_records += 1
                    continue
                retained_lines.append(json.dumps(_sanitize_durable_record(raw), separators=(",", ":")) + "\n")

    chunks, capacity_pruned_records = _bounded_compaction_chunks(
        retained_lines,
        max_bytes=max_bytes,
        file_count=backup_count + 1,
    )
    _rewrite_compacted_ledgers(path, chunks, backup_count=backup_count)
    resolved_path = path.resolve(strict=False)
    _NEXT_COMPACTION_BY_PATH[resolved_path] = current_time + _COST_COMPACTION_INTERVAL_SECONDS

    receipt = CostCompactionReceipt(
        scanned_records=scanned_records,
        retained_records=sum(len(chunk) for chunk in chunks),
        expired_records=expired_records,
        capacity_pruned_records=capacity_pruned_records,
        files_rewritten=len(chunks),
        cutoff_timestamp=cutoff,
    )
    logger.info(
        "Cost ledger compaction receipt: path=%r scanned=%d retained=%d expired=%d capacity_pruned=%d files=%d",
        path,
        receipt.scanned_records,
        receipt.retained_records,
        receipt.expired_records,
        receipt.capacity_pruned_records,
        receipt.files_rewritten,
    )
    return receipt


def persist_cost_entry(entry: CostEntry, config: CostPersistenceConfig) -> None:
    """Persist one cost entry to the bounded JSONL ledger.

    Args:
        entry: Recorded cost entry.
        config: Persistence settings.
    """
    line = json.dumps(cost_entry_to_json(entry), separators=(",", ":")) + "\n"
    _persist_bounded_jsonl(config.entries_path, line, config)


def persist_cost_entries(
    entries: Sequence[CostEntry],
    config: CostPersistenceConfig,
    *,
    consent_class: str | None = None,
) -> None:
    """Persist an engine-derived cost batch with one durable append cycle.

    ``vetinari.engine.events`` is the sole production caller for engine-derived
    entries.  Treating the serialized batch as one append unit preserves the
    byte-cap rotation boundary while using exactly one open, flush, and fsync
    cycle for the batch.

    Args:
        entries: Ordered cost entries to append. An empty sequence is a no-op.
        config: Persistence settings.
        consent_class: Optional producer-side data-governance classification
            stamped into each serialized record without changing ``CostEntry``.
    """
    if not entries:
        return
    serialized: list[str] = []
    for entry in entries:
        raw = cost_entry_to_json(entry)
        if consent_class is not None:
            raw["consent_class"] = consent_class
        serialized.append(json.dumps(raw, separators=(",", ":")) + "\n")
    lines = "".join(serialized)
    _persist_bounded_jsonl(config.entries_path, lines, config)


def persist_budget_alert(entry: CostEntry, projected_total: float, config: CostPersistenceConfig) -> None:
    """Persist a budget alert when the cost cap is crossed.

    Args:
        entry: Entry that crossed the cap.
        projected_total: Total cost after applying the entry.
        config: Persistence settings.
    """
    alert = {
        "schema_version": _COST_ENTRY_SCHEMA_VERSION,
        "timestamp": time.time(),
        "provider": entry.provider,
        "model": entry.model,
        "agent": entry.agent,
        "task_id": entry.task_id,
        "project_id": entry.project_id,
        "entry_cost_usd": entry.cost_usd or 0.0,
        "projected_total_usd": projected_total,
        "budget_limit_usd": config.budget_limit_usd,
    }
    correlation_ref = _correlation_ref(entry.trace_id, entry.span_id)
    if correlation_ref is not None:
        alert["correlation_ref"] = correlation_ref
    line = json.dumps(alert, separators=(",", ":")) + "\n"
    _persist_bounded_jsonl(config.budget_alerts_path, line, config)


def _current_cost_correlation() -> dict[str, str | None]:
    """Return active or just-ended GenAI correlation IDs for cost attribution."""
    observability = _load_genai_observability()
    if observability is None:
        logger.warning(
            "Cost correlation lookup skipped — observability.otel_genai unavailable; "
            "falling back to logging-context IDs; trace/span IDs may be absent.",
        )
        active = get_correlation_ids()
        return {
            "trace_id": _optional_str(active.get("trace_id")),
            "span_id": _optional_str(active.get("span_id")),
        }

    recent = observability._pop_recent_span_correlation()
    correlation_ids = {
        "trace_id": _optional_str(recent.get("trace_id")),
        "span_id": _optional_str(recent.get("span_id")),
    }
    if correlation_ids.get("trace_id") or correlation_ids.get("span_id"):
        return correlation_ids
    active = get_correlation_ids()
    return {
        "trace_id": _optional_str(active.get("trace_id")),
        "span_id": _optional_str(active.get("span_id")),
    }


def _maybe_compact_cost_ledgers(path: Path, config: CostPersistenceConfig) -> None:
    """Run retention compaction once per configured interval and process."""
    _validate_compaction_limits(
        backup_count=config.backup_count,
        max_bytes=config.max_bytes,
        retention_days=config.retention_days,
    )
    with _cost_ledger_transaction(path):
        _maybe_compact_cost_ledgers_locked(path, config)


def _maybe_compact_cost_ledgers_locked(path: Path, config: CostPersistenceConfig) -> None:
    """Run due compaction while the path's full transaction lock is held."""
    current_time = time.time()
    resolved_path = path.resolve(strict=False)
    if current_time < _NEXT_COMPACTION_BY_PATH.get(resolved_path, 0.0):
        return
    _compact_cost_ledgers_locked(
        path,
        backup_count=config.backup_count,
        max_bytes=config.max_bytes,
        retention_days=config.retention_days,
        current_time=current_time,
    )
    _NEXT_COMPACTION_BY_PATH[resolved_path] = current_time + config.compaction_interval_seconds


def _persist_bounded_jsonl(path: Path, line: str, config: CostPersistenceConfig) -> None:
    """Compact if due and append one bounded unit in one cross-process transaction."""
    _validate_append_unit(line, max_bytes=config.max_bytes, backup_count=config.backup_count)
    _validate_compaction_limits(
        backup_count=config.backup_count,
        max_bytes=config.max_bytes,
        retention_days=config.retention_days,
    )
    with _cost_ledger_transaction(path):
        _maybe_compact_cost_ledgers_locked(path, config)
        _append_bounded_jsonl_locked(
            path,
            line,
            max_bytes=config.max_bytes,
            backup_count=config.backup_count,
        )


def _append_bounded_jsonl(path: Path, line: str, *, max_bytes: int, backup_count: int) -> None:
    """Append one JSONL unit under the ledger's full transaction lock."""
    incoming_bytes = _validate_append_unit(line, max_bytes=max_bytes, backup_count=backup_count)
    with _cost_ledger_transaction(path):
        _append_bounded_jsonl_locked(
            path,
            line,
            max_bytes=max_bytes,
            backup_count=backup_count,
            incoming_bytes=incoming_bytes,
        )


def _validate_append_unit(line: str, *, max_bytes: int, backup_count: int) -> int:
    """Return encoded size after rejecting an append unit that cannot fit one ledger."""
    if max_bytes <= 0 or backup_count < 0:
        raise ValueError("cost persistence requires a positive byte cap and non-negative backup count")
    incoming_bytes = len(line.encode("utf-8"))
    if incoming_bytes > max_bytes:
        raise ValueError(f"cost ledger append unit ({incoming_bytes} bytes) exceeds byte cap ({max_bytes} bytes)")
    return incoming_bytes


def _append_bounded_jsonl_locked(
    path: Path,
    line: str,
    *,
    max_bytes: int,
    backup_count: int,
    incoming_bytes: int | None = None,
) -> None:
    """Append one bounded unit while the path's process and file locks are held."""
    resolved_incoming_bytes = incoming_bytes
    if resolved_incoming_bytes is None:
        resolved_incoming_bytes = _validate_append_unit(
            line,
            max_bytes=max_bytes,
            backup_count=backup_count,
        )
    _ensure_private_parent(path)
    _rotate_jsonl_if_needed(
        path,
        resolved_incoming_bytes,
        max_bytes=max_bytes,
        backup_count=backup_count,
    )
    with _open_private_append(path) as fh:
        fh.write(line)
        fh.flush()
        os.fsync(fh.fileno())


def _sanitize_durable_record(raw: dict[str, object]) -> dict[str, object]:
    """Migrate one durable record to the privacy-safe schema."""
    sanitized = dict(raw)
    trace_id = _optional_str(sanitized.pop("trace_id", None))
    span_id = _optional_str(sanitized.pop("span_id", None))
    correlation_ref = _correlation_ref(trace_id, span_id)
    if correlation_ref is not None:
        sanitized["correlation_ref"] = correlation_ref
    elif not _is_correlation_ref(sanitized.get("correlation_ref")):
        sanitized.pop("correlation_ref", None)
    sanitized["schema_version"] = _COST_ENTRY_SCHEMA_VERSION
    return sanitized


def _is_correlation_ref(value: object) -> bool:
    """Return whether a persisted correlation reference has the fixed opaque shape."""
    if not isinstance(value, str) or len(value) != 5 + (_CORRELATION_REF_DIGEST_BYTES * 2):
        return False
    return value.startswith("corr-") and all(character in "0123456789abcdef" for character in value[5:])


def _bounded_compaction_chunks(
    lines: Sequence[str],
    *,
    max_bytes: int,
    file_count: int,
) -> tuple[list[list[str]], int]:
    """Split retained rows into byte-bounded files and prune oldest overflow."""
    chunks: list[list[str]] = []
    current: list[str] = []
    current_bytes = 0
    for line in lines:
        line_bytes = len(line.encode("utf-8"))
        if line_bytes > max_bytes:
            raise ValueError(f"cost ledger retained record ({line_bytes} bytes) exceeds byte cap ({max_bytes} bytes)")
        if current and current_bytes + line_bytes > max_bytes:
            chunks.append(current)
            current = []
            current_bytes = 0
        current.append(line)
        current_bytes += line_bytes
    if current:
        chunks.append(current)
    if len(chunks) <= file_count:
        return chunks, 0
    pruned = sum(len(chunk) for chunk in chunks[:-file_count])
    return chunks[-file_count:], pruned


def _optional_str(value: object) -> str | None:
    """Normalize optional persisted text fields."""
    return None if value is None else str(value)
