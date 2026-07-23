"""Inference endpoint capability and route-receipt contracts."""

from __future__ import annotations

import hashlib
import json
import os
import threading
from collections.abc import Iterable, Mapping
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import yaml

from vetinari.security.redaction import redact_text, redact_value

REQUIRED_ENDPOINT_CAPABILITIES: tuple[str, ...] = (
    "chat",
    "completion",
    "embeddings",
    "tools",
    "structured_output",
    "reasoning_trace",
    "batching",
    "streaming",
    "lora",
    "health",
)
REQUIRED_ENDPOINT_PRIVACY: Mapping[str, str] = {
    "prompt_storage": "prompt_hash_only",
    "route_receipt_redaction": "required",
    "training_reuse": "disabled_without_explicit_opt_in",
}

_RECEIPT_LOCK = threading.RLock()
_MAX_RECEIPT_LINES = 1_000


class CapabilityContractError(RuntimeError):
    """Raised when inference endpoint capability proof is missing or stale."""


@dataclass(frozen=True, slots=True)
class EndpointCapabilityRecord:
    """One endpoint family capability declaration used by cascade routing."""

    family: str
    provider: str
    tier: str
    model_id: str
    capabilities: tuple[str, ...]
    cost_per_1k_tokens: float
    health_check: str
    support_matrix_version: str
    stale_after: str
    proof_refs: tuple[str, ...] = ()
    privacy: Mapping[str, Any] = field(default_factory=dict)

    def __repr__(self) -> str:
        return (
            "EndpointCapabilityRecord("
            f"model_id={self.model_id!r}, provider={self.provider!r}, tier={self.tier!r}, "
            f"capabilities={len(self.capabilities)}, support_matrix_version={self.support_matrix_version!r})"
        )

    def to_tier(self) -> dict[str, Any]:
        """Return a strict cascade-tier descriptor for AdapterManager."""
        return {
            "model_id": self.model_id,
            "provider": self.provider,
            "tier": self.tier,
            "capabilities": list(self.capabilities),
            "cost_per_1k_tokens": self.cost_per_1k_tokens,
            "support_matrix_version": self.support_matrix_version,
            "stale_after": self.stale_after,
            "privacy": dict(self.privacy),
        }


@dataclass(frozen=True, slots=True)
class RouteReceipt:
    """Redacted, durable receipt for one cascade route decision."""

    receipt_id: str
    created_at_utc: str
    request_model_id: str
    prompt_sha256: str
    tiers_tried: tuple[str, ...]
    accepted_tier: str
    confidence: float
    confidence_source: str
    confidence_calibration_ref: str
    eval_feedback_refs: tuple[str, ...]
    cost_usd: float
    fallback_reason: str | None
    provider_health: Mapping[str, Any]
    support_matrix_version: str
    redaction_status: str
    readback_path: str

    def __repr__(self) -> str:
        return (
            "RouteReceipt("
            f"receipt_id={self.receipt_id!r}, accepted_tier={self.accepted_tier!r}, "
            f"confidence={self.confidence!r}, redaction_status={self.redaction_status!r})"
        )

    def to_json_line(self) -> str:
        """Serialize this receipt to compact JSONL."""
        return json.dumps(asdict(self), sort_keys=True, separators=(",", ":"))


def _parse_utc(value: str) -> datetime:
    text = str(value)
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError as exc:
        raise CapabilityContractError(f"invalid timestamp: {value!r}") from exc
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _assert_not_stale(label: str, stale_after: str, *, now: datetime | None = None) -> None:
    now_utc = now or datetime.now(timezone.utc)
    if _parse_utc(stale_after) < now_utc:
        raise CapabilityContractError(f"{label} stale after {stale_after}")


def _normalise_capabilities(values: Iterable[Any]) -> tuple[str, ...]:
    caps = tuple(sorted({str(value).strip() for value in values if str(value).strip()}))
    missing = sorted(set(REQUIRED_ENDPOINT_CAPABILITIES) - set(caps))
    if missing:
        raise CapabilityContractError(f"endpoint capability record missing required capabilities: {missing}")
    return caps


def _validate_privacy_contract(index: int, value: Any) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise CapabilityContractError(f"endpoint_families[{index}] missing privacy contract")
    privacy = {str(key): item for key, item in value.items()}
    for key, expected in REQUIRED_ENDPOINT_PRIVACY.items():
        actual = str(privacy.get(key) or "").strip()
        if actual != expected:
            raise CapabilityContractError(f"endpoint_families[{index}] privacy.{key} must be {expected!r}")
    retention = str(privacy.get("retention") or "").strip()
    if not retention:
        raise CapabilityContractError(f"endpoint_families[{index}] privacy.retention is required")
    return privacy


def load_endpoint_capability_records(
    path: str | Path = "config/model_families.yaml",
    *,
    now: datetime | None = None,
) -> list[EndpointCapabilityRecord]:
    """Load and validate endpoint capability records from ``config/model_families.yaml``.

    Returns:
        Valid endpoint capability records for strict cascade routing.

    Raises:
        CapabilityContractError: If capability config is missing, stale, or malformed.
    """
    config_path = Path(path)
    try:
        payload = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    except OSError as exc:
        raise CapabilityContractError(f"cannot read endpoint capability config: {config_path}") from exc
    if not isinstance(payload, dict):
        raise CapabilityContractError("model_families.yaml must contain a mapping")
    rows = payload.get("endpoint_families")
    if not isinstance(rows, list) or not rows:
        raise CapabilityContractError("model_families.yaml endpoint_families must be a non-empty list")
    support_matrix_version = str(payload.get("support_matrix_version") or "").strip()
    if not support_matrix_version:
        raise CapabilityContractError("model_families.yaml missing support_matrix_version")
    records: list[EndpointCapabilityRecord] = []
    for index, row in enumerate(rows):
        if not isinstance(row, dict):
            raise CapabilityContractError(f"endpoint_families[{index}] must be a mapping")
        for key in (
            "family",
            "provider",
            "tier",
            "model_id",
            "capabilities",
            "cost_per_1k_tokens",
            "health_check",
            "stale_after",
            "privacy",
        ):
            if key not in row:
                raise CapabilityContractError(f"endpoint_families[{index}] missing {key}")
        _assert_not_stale(f"endpoint_families[{index}]", str(row["stale_after"]), now=now)
        proof_refs = tuple(str(value).strip() for value in row.get("proof_refs", ()) if str(value).strip())
        if not proof_refs:
            raise CapabilityContractError(f"endpoint_families[{index}] missing proof_refs")
        records.append(
            EndpointCapabilityRecord(
                family=str(row["family"]),
                provider=str(row["provider"]),
                tier=str(row["tier"]),
                model_id=str(row["model_id"]),
                capabilities=_normalise_capabilities(row["capabilities"]),
                cost_per_1k_tokens=float(row["cost_per_1k_tokens"]),
                health_check=str(row["health_check"]),
                support_matrix_version=support_matrix_version,
                stale_after=str(row["stale_after"]),
                proof_refs=proof_refs,
                privacy=_validate_privacy_contract(index, row["privacy"]),
            )
        )
    return records


def read_support_matrix_version(
    path: str | Path = "config/support_matrix.yaml",
    *,
    now: datetime | None = None,
) -> str:
    """Read and freshness-check the support matrix before routing.

    Returns:
        The support matrix schema/version string.

    Raises:
        CapabilityContractError: If the support matrix is missing, stale, or malformed.
    """
    matrix_path = Path(path)
    try:
        payload = yaml.safe_load(matrix_path.read_text(encoding="utf-8"))
    except OSError as exc:
        raise CapabilityContractError(f"cannot read support matrix: {matrix_path}") from exc
    if not isinstance(payload, dict):
        raise CapabilityContractError("support_matrix.yaml must contain a mapping")
    version = str(payload.get("schema_version") or "").strip()
    last_verified = payload.get("last_verified")
    freshness_days = int(payload.get("freshness_days") or 0)
    if not version or not last_verified or freshness_days <= 0:
        raise CapabilityContractError("support matrix missing schema_version, last_verified, or freshness_days")
    verified = _parse_utc(str(last_verified))
    expires = verified + timedelta(days=freshness_days)
    now_utc = now or datetime.now(timezone.utc)
    if expires < now_utc:
        raise CapabilityContractError(f"support matrix stale after {expires.isoformat()}")
    return version


def validate_cascade_tiers(
    tiers: Iterable[Mapping[str, Any]],
    *,
    strict_capabilities: bool = False,
    now: datetime | None = None,
) -> list[dict[str, Any]]:
    """Validate cascade tiers and return normalized tier dictionaries.

    Returns:
        Normalized cascade tier dictionaries sorted by declared cost.

    Raises:
        CapabilityContractError: If tiers are missing, stale, malformed, or incomplete.
    """
    normalized: list[dict[str, Any]] = []
    for index, tier in enumerate(tiers):
        model_id = str(tier.get("model_id") or "").strip()
        if not model_id:
            raise CapabilityContractError(f"cascade tier {index} missing model_id")
        cost = float(tier.get("cost_per_1k_tokens", 0.0))
        if cost < 0:
            raise CapabilityContractError(f"cascade tier {model_id} has negative cost")
        if cost > 10000.0:
            raise CapabilityContractError(f"cascade tier {model_id} cost_per_1k_tokens implausibly high: {cost!r}")
        stale_after = str(tier.get("stale_after") or "").strip()
        support_matrix_version = str(tier.get("support_matrix_version") or "").strip()
        if stale_after or support_matrix_version:
            if not stale_after or not support_matrix_version:
                raise CapabilityContractError(f"cascade tier {model_id} missing freshness or support proof")
            _assert_not_stale(f"cascade tier {model_id}", stale_after, now=now)
        if strict_capabilities:
            _normalise_capabilities(tier.get("capabilities", ()))
            if not stale_after or not support_matrix_version:
                raise CapabilityContractError(f"cascade tier {model_id} missing freshness or support proof")
        normalized.append(dict(tier, model_id=model_id, cost_per_1k_tokens=cost))
    if not normalized:
        raise CapabilityContractError("cascade tiers must not be empty")
    return sorted(normalized, key=lambda item: float(item.get("cost_per_1k_tokens", 0.0)))


def redact_prompt_hash(prompt: str) -> str:
    """Return a prompt hash suitable for receipts without storing prompt text."""
    return hashlib.sha256(prompt.encode("utf-8", errors="replace")).hexdigest()


def _redact_receipt_text(value: object | None) -> str | None:
    """Redact receipt text fields while preserving ordinary model identifiers."""
    if value is None:
        return None
    return redact_text(str(value))


def build_route_receipt(
    *,
    request_model_id: str,
    prompt: str,
    tiers_tried: Iterable[str],
    accepted_tier: str,
    confidence: float,
    confidence_source: str,
    confidence_calibration_ref: str,
    eval_feedback_refs: Iterable[str],
    cost_usd: float,
    fallback_reason: str | None,
    provider_health: Mapping[str, Any],
    support_matrix_version: str,
    readback_path: str,
) -> RouteReceipt:
    """Build a redacted route receipt.

    Returns:
        A receipt that stores only a hash of the prompt text.

    Raises:
        CapabilityContractError: If confidence or confidence evidence fields
            are missing or invalid.
    """
    created = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
    prompt_hash = redact_prompt_hash(prompt)
    if not 0.0 <= float(confidence) <= 1.0:
        raise CapabilityContractError(f"route confidence out of bounds: {confidence!r}")
    if float(cost_usd) > 1000.0:
        raise CapabilityContractError(f"route cost_usd implausibly high: {cost_usd!r}")
    confidence_source = str(confidence_source).strip()
    confidence_calibration_ref = str(confidence_calibration_ref).strip()
    eval_feedback_refs = tuple(str(ref).strip() for ref in eval_feedback_refs if str(ref).strip())
    if not confidence_source:
        raise CapabilityContractError("route receipt missing confidence_source")
    if not confidence_calibration_ref:
        raise CapabilityContractError("route receipt missing confidence_calibration_ref")
    if not eval_feedback_refs:
        raise CapabilityContractError("route receipt missing eval_feedback_refs")
    receipt_id = hashlib.sha256(
        "|".join([created, request_model_id, accepted_tier, prompt_hash]).encode("utf-8")
    ).hexdigest()[:24]
    return RouteReceipt(
        receipt_id=receipt_id,
        created_at_utc=created,
        request_model_id=_redact_receipt_text(request_model_id) or "",
        prompt_sha256=prompt_hash,
        tiers_tried=tuple(_redact_receipt_text(tier) or "" for tier in tiers_tried),
        accepted_tier=_redact_receipt_text(accepted_tier) or "",
        confidence=confidence,
        confidence_source=confidence_source,
        confidence_calibration_ref=confidence_calibration_ref,
        eval_feedback_refs=eval_feedback_refs,
        cost_usd=cost_usd,
        fallback_reason=_redact_receipt_text(fallback_reason),
        provider_health=redact_value(dict(provider_health)),
        support_matrix_version=support_matrix_version,
        redaction_status="prompt_hash_only",
        readback_path=_redact_receipt_text(readback_path) or "",
    )


def append_route_receipt(path: str | Path, receipt: RouteReceipt) -> None:
    """Append a route receipt atomically enough for process-local writers.

    Args:
        path: JSONL receipt path.
        receipt: Receipt to append.
    """
    receipt_path = Path(path)
    with _RECEIPT_LOCK:
        receipt_path.parent.mkdir(parents=True, exist_ok=True)
        with receipt_path.open("a", encoding="utf-8") as handle:
            handle.write(receipt.to_json_line() + "\n")
        _prune_jsonl(receipt_path)


def read_route_receipts(path: str | Path) -> list[RouteReceipt]:
    """Read route receipts and fail closed on unreadable or malformed state.

    Returns:
        Parsed route receipts from the JSONL store.

    Raises:
        CapabilityContractError: If the receipt store is unreadable or malformed.
    """
    receipt_path = Path(path)
    try:
        lines = receipt_path.read_text(encoding="utf-8").splitlines()
    except OSError as exc:
        raise CapabilityContractError(f"cannot read route receipts: {receipt_path}") from exc
    receipts: list[RouteReceipt] = []
    for index, line in enumerate(lines, start=1):
        if not line.strip():
            continue
        try:
            payload = json.loads(line)
            if any(key in payload for key in ("prompt", "raw_prompt", "system_prompt")):
                raise ValueError("route receipt contains raw prompt text")
            if str(payload.get("redaction_status")) != "prompt_hash_only":
                raise ValueError("route receipt redaction status is not prompt_hash_only")
            eval_feedback_refs = tuple(str(ref).strip() for ref in payload["eval_feedback_refs"] if str(ref).strip())
            if not str(payload["confidence_source"]).strip():
                raise ValueError("missing confidence_source")
            if not str(payload["confidence_calibration_ref"]).strip():
                raise ValueError("missing confidence_calibration_ref")
            if not eval_feedback_refs:
                raise ValueError("missing eval_feedback_refs")
            receipts.append(
                RouteReceipt(
                    receipt_id=str(payload["receipt_id"]),
                    created_at_utc=str(payload["created_at_utc"]),
                    request_model_id=str(payload["request_model_id"]),
                    prompt_sha256=str(payload["prompt_sha256"]),
                    tiers_tried=tuple(payload["tiers_tried"]),
                    accepted_tier=str(payload["accepted_tier"]),
                    confidence=float(payload["confidence"]),
                    confidence_source=str(payload["confidence_source"]),
                    confidence_calibration_ref=str(payload["confidence_calibration_ref"]),
                    eval_feedback_refs=eval_feedback_refs,
                    cost_usd=float(payload["cost_usd"]),
                    fallback_reason=payload.get("fallback_reason"),
                    provider_health=dict(payload["provider_health"]),
                    support_matrix_version=str(payload["support_matrix_version"]),
                    redaction_status=str(payload["redaction_status"]),
                    readback_path=str(payload["readback_path"]),
                )
            )
        except (KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
            raise CapabilityContractError(f"malformed route receipt at line {index}") from exc
    return receipts


def _prune_jsonl(path: Path, *, max_lines: int = _MAX_RECEIPT_LINES) -> None:
    lines = path.read_text(encoding="utf-8").splitlines()
    if len(lines) <= max_lines:
        return
    _replace_text_atomic(path, "\n".join(lines[-max_lines:]) + "\n")


def _replace_text_atomic(path: Path, text: str) -> None:
    tmp_path = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    try:
        with tmp_path.open("w", encoding="utf-8", newline="\n") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp_path, path)
    finally:
        if tmp_path.exists():
            tmp_path.unlink()
