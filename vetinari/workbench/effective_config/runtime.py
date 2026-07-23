"""Typed runtime snapshots for requested versus effective Workbench config."""

from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from typing import Any, Literal

from vetinari.security.redaction import is_sensitive_key, redact_value

EffectiveConfigStatus = Literal["ok", "degraded", "blocked"]
_SAMPLE_MODEL_CATALOG_BEFORE = "qwen3-14b"
_SAMPLE_MODEL_CATALOG_AFTER = "qwen3-32b"


class EffectiveConfigError(ValueError):
    """Raised when a snapshot would hide missing governance context."""


@dataclass(frozen=True, slots=True)
class EffectiveConfigEntry:
    """One resolved setting with provenance, authority, and safety context."""

    category: str
    key: str
    requested_value: Any
    effective_value: Any
    source_layer: str
    backend_accepted: bool
    provenance_ref: str
    confidence: float
    safety_ref: str
    budget_ref: str
    authority_ref: str
    persisted_ref: str
    conflicts: tuple[str, ...] = ()
    fallback_reason: str | None = None
    stale: bool = False

    def __post_init__(self) -> None:
        for field_name in (
            "category",
            "key",
            "source_layer",
            "provenance_ref",
            "safety_ref",
            "budget_ref",
            "authority_ref",
            "persisted_ref",
        ):
            value = getattr(self, field_name)
            if not isinstance(value, str) or not value.strip():
                raise EffectiveConfigError(f"{field_name} is required for effective config snapshots")
        if not 0 <= self.confidence <= 1:
            raise EffectiveConfigError("confidence must be between 0 and 1")
        if not isinstance(self.conflicts, tuple):
            raise EffectiveConfigError("conflicts must be a tuple")

    def to_dict(self) -> dict[str, Any]:
        """Return a JSON-serializable dictionary.

        Returns:
            dict[str, Any] value produced by to_dict().
        """
        body = asdict(self)
        body["conflicts"] = list(self.conflicts)
        return body

    def __repr__(self) -> str:
        """Return a compact diagnostic representation."""
        return f"EffectiveConfigEntry(category={self.category!r}, key={self.key!r}, requested_value={self.requested_value!r})"


@dataclass(frozen=True, slots=True)
class EffectiveConfigSnapshot:
    """Runtime-linked snapshot for explaining what actually changed."""

    snapshot_id: str
    run_id: str
    run_kind: str
    captured_at_utc: str
    status: EffectiveConfigStatus
    entries: tuple[EffectiveConfigEntry, ...]
    blockers: tuple[str, ...] = ()
    diff_from_snapshot_id: str | None = None

    def __post_init__(self) -> None:
        for field_name in ("snapshot_id", "run_id", "run_kind", "captured_at_utc"):
            value = getattr(self, field_name)
            if not isinstance(value, str) or not value.strip():
                raise EffectiveConfigError(f"{field_name} is required for effective config snapshots")
        if self.status == "ok" and not self.entries:
            raise EffectiveConfigError("ok snapshots require at least one entry")
        if self.status in {"degraded", "blocked"} and not self.blockers:
            raise EffectiveConfigError("degraded or blocked snapshots require blockers")
        if not isinstance(self.entries, tuple):
            raise EffectiveConfigError("entries must be a tuple")
        if not isinstance(self.blockers, tuple):
            raise EffectiveConfigError("blockers must be a tuple")

    def to_dict(self) -> dict[str, Any]:
        """Return a JSON-serializable dictionary.

        Returns:
            dict[str, Any] value produced by to_dict().
        """
        body = asdict(self)
        body["entries"] = [entry.to_dict() for entry in self.entries]
        body["blockers"] = list(self.blockers)
        return body

    def __repr__(self) -> str:
        """Return a compact diagnostic representation."""
        return f"EffectiveConfigSnapshot(snapshot_id={self.snapshot_id!r}, run_id={self.run_id!r}, run_kind={self.run_kind!r})"


@dataclass(frozen=True, slots=True)
class EffectiveConfigDiff:
    """One requested/effective value delta between two snapshots."""

    category: str
    key: str
    before_effective_value: Any
    after_effective_value: Any
    before_source_layer: str
    after_source_layer: str
    changed_signals: tuple[str, ...]

    def to_dict(self) -> dict[str, Any]:
        """Return a JSON-serializable dictionary.

        Returns:
            dict[str, Any] value produced by to_dict().
        """
        body = asdict(self)
        body["changed_signals"] = list(self.changed_signals)
        return body

    def __repr__(self) -> str:
        """Return a compact diagnostic representation."""
        return f"EffectiveConfigDiff(category={self.category!r}, key={self.key!r}, before_effective_value={self.before_effective_value!r})"


def capture_model_selection_config_snapshot(
    request: Any, config: dict[str, Any], target: Any
) -> EffectiveConfigSnapshot:
    """Capture the effective model/runtime/budget config for a selection call.

    Args:
        request: Request object sent through the operation.
        config: Config value consumed by capture_model_selection_config_snapshot().
        target: Target object or path updated by the operation.

    Returns:
        EffectiveConfigSnapshot value produced by capture_model_selection_config_snapshot().
    """
    capability = str(getattr(request, "capability", "unknown-capability"))
    run_id = f"model-selection:{capability}:{getattr(target, 'model', 'unknown-model')}"
    policy_ref = str(config.get("policy_ref") or f"policy://inference/{capability}")
    persisted_ref = str(config.get("config_ref") or "config/compute_routing.yaml")
    return _snapshot(
        run_id,
        "model-selection",
        (
            _entry(
                category="model",
                key="model_id",
                requested_value=capability,
                effective_value=getattr(target, "model", ""),
                source_layer="compute-routing.capabilities",
                accepted=True,
                provenance_ref=f"inference-request:{capability}",
                confidence=float(config.get("confidence", 0.85)),
                safety_ref=f"quality-floor:{getattr(request, 'quality_floor', 'standard')}",
                budget_ref=f"latency-budget:{getattr(request, 'latency_budget_s', 'unknown')}s",
                authority_ref=policy_ref,
                persisted_ref=persisted_ref,
            ),
            _entry(
                category="runtime",
                key="compute",
                requested_value=getattr(request, "lane", ""),
                effective_value=getattr(target, "compute", ""),
                source_layer="compute-routing.targets",
                accepted=True,
                provenance_ref=f"caller:{getattr(request, 'caller_subsystem', '') or 'unknown'}",
                confidence=0.8,
                safety_ref="cpu-tier-status-cache",
                budget_ref=f"estimated-latency:{getattr(target, 'estimated_latency_s', 'unknown')}s",
                authority_ref=policy_ref,
                persisted_ref=persisted_ref,
            ),
        ),
    )


def capture_embedding_config_snapshot(
    config: Any, *, accepted: bool, fallback_reason: str | None = None
) -> EffectiveConfigSnapshot:
    """Capture the effective embedding model/device configuration.

    Returns:
        EffectiveConfigSnapshot value produced by capture_embedding_config_snapshot().
    """
    model_id = getattr(config, "model_id", None) if not isinstance(config, dict) else config.get("embed_model")
    batch_size = getattr(config, "batch_size", None) if not isinstance(config, dict) else config.get("batch_size", 1)
    device = getattr(config, "device", None) if not isinstance(config, dict) else config.get("device", "cpu")
    blockers = ("embedding-backend-unavailable",) if not accepted else ()
    return _snapshot(
        f"embedding:{model_id or 'missing-model'}",
        "embedding",
        (
            _entry(
                category="retrieval",
                key="embed_model",
                requested_value=model_id,
                effective_value=model_id,
                source_layer="embedder-config",
                accepted=accepted,
                provenance_ref="embedder-runtime",
                confidence=0.82 if accepted else 0.2,
                safety_ref="local-embedding-policy",
                budget_ref=f"batch-size:{batch_size}",
                authority_ref="policy://retrieval/embedding",
                persisted_ref=f"embedder-config:{model_id or 'missing'}",
                fallback_reason=fallback_reason,
            ),
            _entry(
                category="runtime",
                key="device",
                requested_value=device,
                effective_value=device or "cpu",
                source_layer="embedder-config",
                accepted=accepted,
                provenance_ref="embedder-runtime",
                confidence=0.8,
                safety_ref="cpu-tier-runtime",
                budget_ref="embedding-device-budget",
                authority_ref="policy://runtime/local",
                persisted_ref=f"embedder-config:{model_id or 'missing'}",
            ),
        ),
        blockers=blockers,
    )


def capture_retrieval_config_snapshot(
    query: str,
    *,
    k: int,
    max_chars: int,
    category: str | None,
    backend: str,
    fallback_reason: str | None,
) -> EffectiveConfigSnapshot:
    """Capture the effective retrieval and prompt-budget settings.

    Returns:
        EffectiveConfigSnapshot value produced by capture_retrieval_config_snapshot().
    """
    accepted = fallback_reason is None
    return _snapshot(
        f"retrieval:{_stable_snapshot_suffix(query, k, max_chars, category)}",
        "retrieval",
        (
            _entry(
                category="retrieval",
                key="backend",
                requested_value="vector",
                effective_value=backend,
                source_layer="knowledge-base.query",
                accepted=accepted,
                provenance_ref="rag-query",
                confidence=0.78 if accepted else 0.45,
                safety_ref="ssrf-and-local-embedding-policy",
                budget_ref=f"max-chars:{max_chars}",
                authority_ref="policy://rag/retrieval",
                persisted_ref="knowledge-base-sqlite",
                fallback_reason=fallback_reason,
            ),
            _entry(
                category="budget",
                key="top_k",
                requested_value=k,
                effective_value=k,
                source_layer="knowledge-base.query",
                accepted=True,
                provenance_ref="rag-query",
                confidence=0.9,
                safety_ref="prompt-budget",
                budget_ref=f"max-chars:{max_chars}",
                authority_ref="policy://rag/retrieval",
                persisted_ref="knowledge-base-sqlite",
            ),
        ),
        blockers=("retrieval-vector-fallback",) if fallback_reason else (),
    )


def capture_tool_use_config_snapshot(
    tool_name: str, requested: dict[str, Any], *, success: bool
) -> EffectiveConfigSnapshot:
    """Capture tool-use policy and backend acceptance for one tool call.

    Args:
        tool_name: Name used to identify the target object.
        requested: Request object sent through the operation.
        success: Success value consumed by capture_tool_use_config_snapshot().

    Returns:
        EffectiveConfigSnapshot value produced by capture_tool_use_config_snapshot().
    """
    safe_requested = {_key: _redact_requested_value(_key, value) for _key, value in sorted(requested.items())}
    return _snapshot(
        f"tool-use:{tool_name}:{_stable_snapshot_suffix(tool_name, safe_requested)}",
        "tool-use",
        (
            _entry(
                category="tool",
                key=tool_name,
                requested_value=safe_requested,
                effective_value="accepted" if success else "rejected",
                source_layer="tool-registry",
                accepted=success,
                provenance_ref=f"tool-call:{tool_name}",
                confidence=0.8 if success else 0.35,
                safety_ref="tool-permission-policy",
                budget_ref="tool-execution-budget",
                authority_ref="policy://tools/registry",
                persisted_ref="tool-registry-runtime",
                fallback_reason=None if success else "tool-execution-failed",
            ),
        ),
        blockers=() if success else ("tool-execution-failed",),
    )


def capture_training_config_snapshot(
    request: Any, *, status: str, blockers: tuple[str, ...]
) -> EffectiveConfigSnapshot:
    """Capture the effective training-plan resource and governance settings.

    Returns:
        EffectiveConfigSnapshot value produced by capture_training_config_snapshot().
    """
    recipe = request.recipe
    gate = request.dataset_gate
    return _snapshot(
        f"training:{getattr(request, 'request_id', 'missing-request')}",
        "training-plan",
        (
            _entry(
                category="training",
                key="base_model_id",
                requested_value=getattr(recipe, "base_model_id", ""),
                effective_value=getattr(recipe, "base_model_id", ""),
                source_layer="training-recipe",
                accepted=not blockers,
                provenance_ref=getattr(gate, "lineage_ref", ""),
                confidence=0.86 if not blockers else 0.4,
                safety_ref="training-data-quality-gate",
                budget_ref=f"max-cost:{getattr(recipe.resource_plan, 'max_cost_usd', 'unknown')}",
                authority_ref="policy://training/recipe-harness",
                persisted_ref=f"training-recipe:{getattr(recipe, 'recipe_id', 'missing')}",
                fallback_reason=";".join(blockers) if blockers else None,
            ),
            _entry(
                category="budget",
                key="local_vram_gb",
                requested_value=getattr(request, "local_vram_gb", 0),
                effective_value=getattr(request, "local_vram_gb", 0),
                source_layer="training-request",
                accepted=True,
                provenance_ref=f"operator:{getattr(request, 'operator', '')}",
                confidence=0.84,
                safety_ref="scheduler-training-lane",
                budget_ref=f"min-vram:{getattr(recipe.resource_plan, 'min_vram_gb', 'unknown')}",
                authority_ref="policy://runtime/training",
                persisted_ref=f"training-request:{getattr(request, 'request_id', 'missing')}",
            ),
        ),
        blockers=blockers if status == "blocked" else (),
    )


def diff_effective_config_snapshots(
    before: EffectiveConfigSnapshot,
    after: EffectiveConfigSnapshot,
) -> tuple[EffectiveConfigDiff, ...]:
    """Diff two snapshots by category/key.

    Args:
        before: Before value consumed by diff_effective_config_snapshots().
        after: After value consumed by diff_effective_config_snapshots().

    Returns:
        tuple[EffectiveConfigDiff, ...] value produced by diff_effective_config_snapshots().
    """
    before_entries = {(entry.category, entry.key): entry for entry in before.entries}
    changes: list[EffectiveConfigDiff] = []
    for after_entry in after.entries:
        before_entry = before_entries.get((after_entry.category, after_entry.key))
        if before_entry is None:
            changes.append(
                EffectiveConfigDiff(
                    category=after_entry.category,
                    key=after_entry.key,
                    before_effective_value=None,
                    after_effective_value=after_entry.effective_value,
                    before_source_layer="absent",
                    after_source_layer=after_entry.source_layer,
                    changed_signals=("added",),
                )
            )
            continue
        changed_signals = [
            name
            for name in ("requested_value", "effective_value", "source_layer", "backend_accepted", "stale")
            if getattr(before_entry, name) != getattr(after_entry, name)
        ]
        if changed_signals:
            changes.append(
                EffectiveConfigDiff(
                    category=after_entry.category,
                    key=after_entry.key,
                    before_effective_value=before_entry.effective_value,
                    after_effective_value=after_entry.effective_value,
                    before_source_layer=before_entry.source_layer,
                    after_source_layer=after_entry.source_layer,
                    changed_signals=tuple(changed_signals),
                )
            )
    return tuple(changes)


def sample_effective_config_explorer(project_id: str = "default") -> dict[str, Any]:
    """Return a deterministic explorer payload for UI and smoke tests.

    Returns:
        dict[str, Any] value produced by sample_effective_config_explorer().
    """
    before = _snapshot(
        f"sample:{project_id}:before",
        "sample-run",
        (
            _entry(
                category="model",
                key="model_id",
                requested_value="default",
                effective_value=_SAMPLE_MODEL_CATALOG_BEFORE,
                source_layer="project-default",
                accepted=True,
                provenance_ref=f"project:{project_id}",
                confidence=0.8,
                safety_ref="sample-safety",
                budget_ref="sample-budget",
                authority_ref="sample-authority",
                persisted_ref="sample-persisted",
            ),
        ),
    )
    after = _snapshot(
        f"sample:{project_id}:after",
        "sample-run",
        (
            _entry(
                category="model",
                key="model_id",
                requested_value="default",
                effective_value=_SAMPLE_MODEL_CATALOG_AFTER,
                source_layer="runtime-override",
                accepted=True,
                provenance_ref=f"project:{project_id}",
                confidence=0.9,
                safety_ref="sample-safety",
                budget_ref="sample-budget",
                authority_ref="sample-authority",
                persisted_ref="sample-persisted",
            ),
        ),
        diff_from_snapshot_id=before.snapshot_id,
    )
    return {
        "project_id": project_id,
        "status": "ok",
        "snapshots": [before.to_dict(), after.to_dict()],
        "diff": [row.to_dict() for row in diff_effective_config_snapshots(before, after)],
    }


def _snapshot(
    run_id: str,
    run_kind: str,
    entries: tuple[EffectiveConfigEntry, ...],
    *,
    blockers: tuple[str, ...] = (),
    diff_from_snapshot_id: str | None = None,
) -> EffectiveConfigSnapshot:
    status: EffectiveConfigStatus = "ok" if not blockers else "degraded"
    return EffectiveConfigSnapshot(
        snapshot_id=f"effective-config:{run_id}",
        run_id=run_id,
        run_kind=run_kind,
        captured_at_utc=datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        status=status,
        entries=entries,
        blockers=blockers,
        diff_from_snapshot_id=diff_from_snapshot_id,
    )


def _stable_snapshot_suffix(*parts: Any) -> str:
    payload = json.dumps(parts, sort_keys=True, separators=(",", ":"), default=str)
    digest = hashlib.sha256(payload.encode("utf-8")).hexdigest()
    return f"{int(digest[:16], 16) % 10_000_000:07d}"


def _entry(
    *,
    category: str,
    key: str,
    requested_value: Any,
    effective_value: Any,
    source_layer: str,
    accepted: bool,
    provenance_ref: str,
    confidence: float,
    safety_ref: str,
    budget_ref: str,
    authority_ref: str,
    persisted_ref: str,
    conflicts: tuple[str, ...] = (),
    fallback_reason: str | None = None,
    stale: bool = False,
) -> EffectiveConfigEntry:
    return EffectiveConfigEntry(
        category=category,
        key=key,
        requested_value=requested_value,
        effective_value=effective_value,
        source_layer=source_layer,
        backend_accepted=accepted,
        provenance_ref=provenance_ref,
        confidence=confidence,
        safety_ref=safety_ref,
        budget_ref=budget_ref,
        authority_ref=authority_ref,
        persisted_ref=persisted_ref,
        conflicts=conflicts,
        fallback_reason=fallback_reason,
        stale=stale,
    )


def _redact_requested_value(key: str, value: Any) -> Any:
    if is_sensitive_key(key):
        if isinstance(value, str):
            return f"<redacted:{len(value)} chars>"
        return f"<redacted:{type(value).__name__}>"
    return redact_value(value)
