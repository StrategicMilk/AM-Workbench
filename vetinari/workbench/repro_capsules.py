"""Portable, redacted reproduction capsules for Workbench runs.

This module derives sealed run capsules from the Workbench metadata spine. It
is read-only against the spine, performs no import-time I/O, and fails closed
when any proof required for a portable rerun is missing or damaged.
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
import threading
from dataclasses import dataclass, field
from enum import Enum
from typing import Any

from vetinari.security.redaction import redact_route_payload
from vetinari.workbench.assets import WorkbenchAsset
from vetinari.workbench.evals import EvalResult
from vetinari.workbench.metadata_spine import WorkbenchSpine, WorkbenchSpineCorrupt, get_workbench_spine
from vetinari.workbench.runs import RunStatus, WorkbenchRun
from vetinari.workbench.security_primitives import shell_safe_token
from vetinari.workbench.traces import TraceSpan, WorkbenchTrace

logger = logging.getLogger(__name__)


_PROJECT_ID_RE: re.Pattern[str] = re.compile(r"[A-Za-z0-9_-]{1,64}")
_TRAVERSAL_MARKERS: tuple[str, ...] = ("/", "\\", "..", "\x00", " ", ";")
_SHA256_RE: re.Pattern[str] = re.compile(r"(?:sha256:)?[A-Fa-f0-9]{64}")
_REPRODUCTION_COMMAND_PREFIX = "vetinari workbench reproduce"
_READ_LOCK: threading.RLock = threading.RLock()


class ReproCapsuleProjectIdRejected(ValueError):
    """Raised when a project id is not canonical."""

    def __init__(self, value: object) -> None:
        super().__init__(f"invalid project_id {value!r}; use [A-Za-z0-9_-] up to 64 characters")
        self.value = value


class ReproCapsuleError(Exception):
    """Raised when a capsule cannot be built or trusted."""


class CapsuleProofStatus(str, Enum):
    """Fail-closed proof status for a capsule."""

    SEALED = "sealed"
    BLOCKED = "blocked"


@dataclass(frozen=True, slots=True)
class CapsuleAssetRef:
    """Revision-pinned asset included in a repro capsule."""

    asset_id: str
    kind: str
    name: str
    revision: str
    provenance: tuple[tuple[str, str], ...]

    def __post_init__(self) -> None:
        _require_non_empty(self.asset_id, "asset_id")
        _require_non_empty(self.kind, "kind")
        _require_non_empty(self.name, "name")
        _require_non_empty(self.revision, "revision")
        if not dict(self.provenance).get("source", "").strip():
            raise ReproCapsuleError(f"asset {self.asset_id!r} is missing provenance.source")

    def __repr__(self) -> str:
        """Return a compact diagnostic representation."""
        return f"CapsuleAssetRef(asset_id={self.asset_id!r}, kind={self.kind!r}, name={self.name!r})"


@dataclass(frozen=True, slots=True)
class CapsuleTraceRef:
    """Trace proof included in a repro capsule."""

    trace_id: str
    root_span_id: str
    captured_at_utc: str
    spans: tuple[tuple[str, str | None, str, str, str], ...]

    def __post_init__(self) -> None:
        _require_non_empty(self.trace_id, "trace_id")
        _require_non_empty(self.root_span_id, "root_span_id")
        _require_non_empty(self.captured_at_utc, "captured_at_utc")
        if not self.spans:
            raise ReproCapsuleError("trace evidence must contain at least one span")

    def __repr__(self) -> str:
        """Return a compact diagnostic representation."""
        return f"CapsuleTraceRef(trace_id={self.trace_id!r}, root_span_id={self.root_span_id!r}, captured_at_utc={self.captured_at_utc!r})"


@dataclass(frozen=True, slots=True)
class CapsuleEvalRef:
    """Eval proof included in a repro capsule."""

    eval_id: str
    kind: str
    asset_id: str
    asset_revision: str
    captured_at_utc: str
    scores: tuple[tuple[str, float, float, bool], ...]

    def __post_init__(self) -> None:
        _require_non_empty(self.eval_id, "eval_id")
        _require_non_empty(self.kind, "kind")
        _require_non_empty(self.asset_id, "asset_id")
        _require_non_empty(self.asset_revision, "asset_revision")
        _require_non_empty(self.captured_at_utc, "captured_at_utc")
        if not self.scores:
            raise ReproCapsuleError(f"eval {self.eval_id!r} has no scores")

    def __repr__(self) -> str:
        """Return a compact diagnostic representation."""
        return f"CapsuleEvalRef(eval_id={self.eval_id!r}, kind={self.kind!r}, asset_id={self.asset_id!r})"


@dataclass(frozen=True, slots=True)
class ReproCapsuleManifest:
    """Canonical, hashable manifest for reproducing one Workbench run."""

    schema_version: int
    capsule_id: str
    project_id: str
    run_id: str
    run_kind: str
    run_status: str
    actor_agent_type: str
    shard_kind: str
    started_at_utc: str
    finished_at_utc: str
    assets: tuple[CapsuleAssetRef, ...]
    runtime_policy: tuple[tuple[str, str], ...]
    traces: tuple[CapsuleTraceRef, ...]
    evals: tuple[CapsuleEvalRef, ...]
    output_evidence: tuple[tuple[str, str], ...]
    reproduction_command: str
    redactions_applied: tuple[str, ...] = ()
    metadata: dict[str, str] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.schema_version != 1:
            raise ReproCapsuleError("repro capsule schema_version must be 1")
        _require_non_empty(self.capsule_id, "capsule_id")
        _canonicalize_project_id(self.project_id)
        _require_non_empty(self.run_id, "run_id")
        _require_non_empty(self.run_kind, "run_kind")
        _require_non_empty(self.actor_agent_type, "actor_agent_type")
        _require_non_empty(self.shard_kind, "shard_kind")
        _require_non_empty(self.started_at_utc, "started_at_utc")
        _require_non_empty(self.finished_at_utc, "finished_at_utc")
        _require_non_empty(self.reproduction_command, "reproduction_command")
        if self.run_status != RunStatus.SUCCEEDED.value:
            raise ReproCapsuleError(f"run {self.run_id!r} is not successful")
        if not self.assets:
            raise ReproCapsuleError("capsule requires at least one asset")
        if not self.runtime_policy:
            raise ReproCapsuleError("capsule requires runtime policy evidence")
        if not self.traces:
            raise ReproCapsuleError("capsule requires trace evidence")
        if not self.evals:
            raise ReproCapsuleError("capsule requires eval evidence")
        if not self.output_evidence:
            raise ReproCapsuleError("capsule requires output evidence")
        if any(not passed for eval_ref in self.evals for _name, _value, _threshold, passed in eval_ref.scores):
            raise ReproCapsuleError("capsule eval evidence contains failing scores")

    def __repr__(self) -> str:
        """Return a compact diagnostic representation."""
        return f"ReproCapsuleManifest(schema_version={self.schema_version!r}, capsule_id={self.capsule_id!r}, project_id={self.project_id!r})"


@dataclass(frozen=True, slots=True)
class ReproCapsuleExport:
    """Sealed capsule plus deterministic manifest hash."""

    manifest: ReproCapsuleManifest
    manifest_hash_sha256: str
    proof_status: CapsuleProofStatus

    def __post_init__(self) -> None:
        _require_non_empty(self.manifest_hash_sha256, "manifest_hash_sha256")
        if len(self.manifest_hash_sha256) != 64:
            raise ReproCapsuleError("manifest_hash_sha256 must be a full SHA-256 hex digest")
        if not isinstance(self.proof_status, CapsuleProofStatus):
            raise ReproCapsuleError("proof_status must be CapsuleProofStatus")

    def to_redacted_dict(self) -> dict[str, Any]:
        """Return a JSON-serializable redacted export payload."""
        return {
            "manifest": _manifest_to_dict(self.manifest),
            "manifest_hash_sha256": self.manifest_hash_sha256,
            "proof_status": self.proof_status.value,
        }


def _require_non_empty(value: str, field_name: str) -> None:
    if not isinstance(value, str) or not value.strip():
        raise ReproCapsuleError(f"{field_name} must be non-empty")


def _canonicalize_project_id(value: str | None) -> str:
    """Return a canonical project id or fail closed."""
    if not isinstance(value, str):
        raise ReproCapsuleProjectIdRejected(value)
    if not value or len(value) > 64 or _PROJECT_ID_RE.fullmatch(value) is None:
        raise ReproCapsuleProjectIdRejected(value)
    if any(marker in value for marker in _TRAVERSAL_MARKERS):
        raise ReproCapsuleProjectIdRejected(value)
    return value


def _redacted_pairs(values: dict[str, str]) -> tuple[tuple[str, str], ...]:
    redacted = redact_route_payload(values)
    if not isinstance(redacted, dict):
        raise ReproCapsuleError("redaction returned unexpected provenance shape")
    return tuple(sorted((str(key), str(value)) for key, value in redacted.items() if str(value).strip()))


def _redactions_applied(source: Any, redacted: Any) -> tuple[str, ...]:
    source_text = json.dumps(source, sort_keys=True, default=str)
    redacted_text = json.dumps(redacted, sort_keys=True, default=str)
    markers: list[str] = []
    if source_text != redacted_text:
        markers.append("private fields redacted")
    if "[REDACTED_PATH]" in redacted_text:
        markers.append("workspace paths redacted")
    if "[REDACTED]" in redacted_text or "[REDACTED_URL]" in redacted_text:
        markers.append("secrets redacted")
    return tuple(sorted(set(markers)))


def _asset_ref(asset: WorkbenchAsset) -> CapsuleAssetRef:
    return CapsuleAssetRef(
        asset_id=asset.asset_id,
        kind=asset.kind.value,
        name=asset.name,
        revision=asset.revision,
        provenance=_redacted_pairs(asset.provenance),
    )


def _trace_ref(trace: WorkbenchTrace) -> CapsuleTraceRef:
    spans: list[tuple[str, str | None, str, str, str]] = []
    for span in trace.spans:
        _validate_span_hashes(span)
        spans.append((span.span_id, span.parent_span_id, span.tool_name, span.inputs_hash, span.outputs_hash))
    return CapsuleTraceRef(
        trace_id=trace.trace_id,
        root_span_id=trace.root_span_id,
        captured_at_utc=trace.captured_at_utc,
        spans=tuple(spans),
    )


def _eval_ref(eval_result: EvalResult) -> CapsuleEvalRef:
    return CapsuleEvalRef(
        eval_id=eval_result.eval_id,
        kind=eval_result.kind.value,
        asset_id=eval_result.asset_id,
        asset_revision=eval_result.asset_revision,
        captured_at_utc=eval_result.captured_at_utc,
        scores=tuple((score.metric_name, score.value, score.threshold, score.passed) for score in eval_result.scores),
    )


def _validate_span_hashes(span: TraceSpan) -> None:
    if _SHA256_RE.fullmatch(span.inputs_hash.strip()) is None:
        raise ReproCapsuleError(f"span {span.span_id!r} inputs_hash must be a SHA-256 digest")
    if _SHA256_RE.fullmatch(span.outputs_hash.strip()) is None:
        raise ReproCapsuleError(f"span {span.span_id!r} outputs_hash must be a SHA-256 digest")


def _runtime_policy(run: WorkbenchRun) -> tuple[tuple[str, str], ...]:
    if run.shard_kind is None:
        raise ReproCapsuleError(f"run {run.run_id!r} missing shard_kind runtime policy")
    return (
        ("actor_agent_type", run.actor_agent_type.value),
        ("run_kind", run.kind.value),
        ("shard_kind", run.shard_kind.value),
        ("status", run.status.value),
    )


def _output_evidence(run: WorkbenchRun) -> tuple[tuple[str, str], ...]:
    if run.status is not RunStatus.SUCCEEDED:
        raise ReproCapsuleError(f"run {run.run_id!r} did not succeed")
    if not run.finished_at_utc.strip():
        raise ReproCapsuleError(f"run {run.run_id!r} missing finished_at_utc")
    metrics = tuple(sorted((metric.name, f"{metric.value:g}{metric.unit}") for metric in run.metrics))
    if not metrics:
        raise ReproCapsuleError(f"run {run.run_id!r} has no output metrics")
    return (("finished_at_utc", run.finished_at_utc), *metrics)


def _asset_revision_index(assets: list[WorkbenchAsset]) -> dict[tuple[str, str], WorkbenchAsset]:
    return {(asset.asset_id, asset.revision): asset for asset in assets}


def _manifest_to_dict(manifest: ReproCapsuleManifest) -> dict[str, Any]:
    payload = {
        "schema_version": manifest.schema_version,
        "capsule_id": manifest.capsule_id,
        "project_id": manifest.project_id,
        "run_id": manifest.run_id,
        "run_kind": manifest.run_kind,
        "run_status": manifest.run_status,
        "actor_agent_type": manifest.actor_agent_type,
        "shard_kind": manifest.shard_kind,
        "started_at_utc": manifest.started_at_utc,
        "finished_at_utc": manifest.finished_at_utc,
        "assets": [
            {
                "asset_id": asset.asset_id,
                "kind": asset.kind,
                "name": asset.name,
                "revision": asset.revision,
                "provenance": list(asset.provenance),
            }
            for asset in manifest.assets
        ],
        "runtime_policy": list(manifest.runtime_policy),
        "traces": [
            {
                "trace_id": trace.trace_id,
                "root_span_id": trace.root_span_id,
                "captured_at_utc": trace.captured_at_utc,
                "spans": list(trace.spans),
            }
            for trace in manifest.traces
        ],
        "evals": [
            {
                "eval_id": eval_ref.eval_id,
                "kind": eval_ref.kind,
                "asset_id": eval_ref.asset_id,
                "asset_revision": eval_ref.asset_revision,
                "captured_at_utc": eval_ref.captured_at_utc,
                "scores": list(eval_ref.scores),
            }
            for eval_ref in manifest.evals
        ],
        "output_evidence": list(manifest.output_evidence),
        "reproduction_command": manifest.reproduction_command,
        "redactions_applied": list(manifest.redactions_applied),
        "metadata": dict(sorted(manifest.metadata.items())),
    }
    return redact_route_payload(payload)


def seal_manifest(manifest: ReproCapsuleManifest) -> str:
    """Return a deterministic SHA-256 hash for a redacted manifest.

    Returns:
        str value produced by seal_manifest().
    """
    payload = _manifest_to_dict(manifest)
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


class ReproCapsuleService:
    """Read-only capsule builder over WorkbenchSpine records."""

    def __init__(self, spine: WorkbenchSpine | None = None) -> None:
        self._spine = spine

    def list_capsules(self, *, project_id: str = "default", limit: int | None = None) -> list[ReproCapsuleExport]:
        """Return sealed capsules for successful runs that have complete proof.

        Returns:
            Collection of capsules values.
        """
        canonical = _canonicalize_project_id(project_id)
        spine = self._get_or_init_spine()
        with _READ_LOCK:
            runs = [run for run in spine.list_runs(status=RunStatus.SUCCEEDED) if run.project_id == canonical]
            capsules: list[ReproCapsuleExport] = []
            for run in sorted(runs, key=lambda row: row.started_at_utc, reverse=True):
                try:
                    capsules.append(self.build_capsule(project_id=canonical, run_id=run.run_id))
                except ReproCapsuleError:
                    logger.warning("Handled recoverable failure before fallback.", exc_info=True)
                    continue
                if limit is not None and len(capsules) >= limit:
                    break
        return capsules

    def build_capsule(self, *, project_id: str = "default", run_id: str) -> ReproCapsuleExport:
        """Build and seal a capsule for one run id.

        Returns:
            Newly constructed capsule value.
        """
        canonical = _canonicalize_project_id(project_id)
        _require_non_empty(run_id, "run_id")
        spine = self._get_or_init_spine()
        with _READ_LOCK:
            run = _find_run(spine, canonical, run_id)
            assets = _assets_for_run(spine, run)
            traces = tuple(_trace_ref(trace) for trace in spine.list_traces_for_run(run.run_id))
            evals = tuple(_eval_ref(eval_result) for eval_result in _evals_for_run(spine, run, assets))
        redaction_probe = {
            "assets": [asset.provenance for asset in assets],
            "traces": [trace.spans for trace in traces],
        }
        redacted_probe = redact_route_payload(redaction_probe)
        manifest = ReproCapsuleManifest(
            schema_version=1,
            capsule_id=f"repro-{run.run_id}",
            project_id=canonical,
            run_id=run.run_id,
            run_kind=run.kind.value,
            run_status=run.status.value,
            actor_agent_type=run.actor_agent_type.value,
            shard_kind=run.shard_kind.value if run.shard_kind is not None else "",
            started_at_utc=run.started_at_utc,
            finished_at_utc=run.finished_at_utc,
            assets=tuple(_asset_ref(asset) for asset in assets),
            runtime_policy=_runtime_policy(run),
            traces=traces,
            evals=evals,
            output_evidence=_output_evidence(run),
            reproduction_command=(
                f"{_REPRODUCTION_COMMAND_PREFIX} --project-id {shell_safe_token(canonical)} "
                f"--run-id {shell_safe_token(run.run_id)}"
            ),
            redactions_applied=_redactions_applied(redaction_probe, redacted_probe),
            metadata={"sealed_by": __name__},
        )
        return ReproCapsuleExport(
            manifest=manifest,
            manifest_hash_sha256=seal_manifest(manifest),
            proof_status=CapsuleProofStatus.SEALED,
        )

    def _get_or_init_spine(self) -> WorkbenchSpine:
        if self._spine is not None:
            return self._spine
        try:
            self._spine = get_workbench_spine()
        except WorkbenchSpineCorrupt as exc:
            raise ReproCapsuleError("workbench spine unavailable; repair the metadata spine") from exc
        return self._spine


def _find_run(spine: WorkbenchSpine, project_id: str, run_id: str) -> WorkbenchRun:
    matches = [run for run in spine.list_runs() if run.run_id == run_id and run.project_id == project_id]
    if not matches:
        raise ReproCapsuleError(f"run {run_id!r} not found")
    return matches[0]


def _assets_for_run(spine: WorkbenchSpine, run: WorkbenchRun) -> list[WorkbenchAsset]:
    by_revision = _asset_revision_index(spine.list_assets())
    assets: list[WorkbenchAsset] = []
    for asset_id, revision in run.asset_revisions:
        asset = by_revision.get((asset_id, revision))
        if asset is None:
            raise ReproCapsuleError(f"run {run.run_id!r} references missing asset {asset_id!r}@{revision!r}")
        if asset.provenance.get("project_id") not in {run.project_id, None}:
            raise ReproCapsuleError(f"asset {asset_id!r} does not belong to project {run.project_id!r}")
        assets.append(asset)
    if not assets:
        raise ReproCapsuleError(f"run {run.run_id!r} has no asset revisions")
    return assets


def _evals_for_run(spine: WorkbenchSpine, run: WorkbenchRun, assets: list[WorkbenchAsset]) -> list[EvalResult]:
    asset_refs = {(asset.asset_id, asset.revision) for asset in assets}
    evals = [
        eval_result
        for eval_result in spine.list_evals(run_id=run.run_id)
        if (eval_result.asset_id, eval_result.asset_revision) in asset_refs
    ]
    if not evals:
        raise ReproCapsuleError(f"run {run.run_id!r} has no eval evidence")
    return evals


__all__ = [
    "CapsuleAssetRef",
    "CapsuleEvalRef",
    "CapsuleProofStatus",
    "CapsuleTraceRef",
    "ReproCapsuleError",
    "ReproCapsuleExport",
    "ReproCapsuleManifest",
    "ReproCapsuleProjectIdRejected",
    "ReproCapsuleService",
    "seal_manifest",
]
