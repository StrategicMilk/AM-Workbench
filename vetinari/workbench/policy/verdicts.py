"""Policy verdict types, classifier, and receipt bridge for Workbench actions."""

from __future__ import annotations

import logging
import os
import threading
from collections import deque
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Any

import yaml

from vetinari.constants import PROJECT_ROOT
from vetinari.receipts.record import WorkReceipt
from vetinari.receipts.store import WorkReceiptStore
from vetinari.security.path_canonicalizer import canonicalize_project_id
from vetinari.types import AgentType
from vetinari.workbench.gateway_policy import (
    GatewayPolicyDecision,
    record_policy_decision,
)
from vetinari.workbench.policy.verdict_bridges import (
    _decision_kind,
    _first_ref,
    _guardrail_action,
    _make_verdict_impl,
    _matches_blocked_rule_impl,
)
from vetinari.workbench.policy.verdict_bridges import (
    _verdict_from_gateway_policy_decision_impl as _verdict_from_gateway_policy_decision,
)
from vetinari.workbench.policy.verdict_bridges import (
    _verdict_from_watcher_decision_impl as _verdict_from_watcher_decision,
)

logger = logging.getLogger(__name__)


_SCHEMA_VERSION = "1.0"
_DEFAULT_POLICY_DIR = PROJECT_ROOT / "config" / "workbench"
_POLICY_FILENAME = "policy_verdicts.yaml"
_RECEIPT_ACTOR = AgentType.WORKBENCH


class VerdictValue(str, Enum):
    """Stable policy verdict values shared by Workbench consumers."""

    ALLOW = "allow"
    WARN = "warn"
    BLOCK = "block"
    ESCALATE = "escalate"


class PolicyMode(str, Enum):
    """How callers enforce a verdict without changing the verdict value."""

    OBSERVE = "observe"
    WARN = "warn"
    STRICT = "strict"


class RiskDomain(str, Enum):
    """Action risk domains used by policy verdicts and watcher round trips."""

    SHELL = "shell"
    FILE_SYSTEM = "file_system"
    NETWORK = "network"
    TOOL_INVOCATION = "tool_invocation"
    MEMORY_SCOPE = "memory_scope"
    USAGE_BUDGET = "token_budget"
    COST_BUDGET = "cost_budget"
    LOOP_AMPLIFICATION = "loop_amplification"
    SIDE_EFFECT = "side_effect"
    PERMISSION = "permission"
    REMOTE_CONTROL = "remote_control"
    APPROVAL = "approval"
    UNKNOWN = "unknown"


class PolicyReasonCode(str, Enum):
    """Machine-readable reason codes for allow, warn, block, and escalate paths."""

    ALLOWED = "allowed"
    MISSING_EVIDENCE = "missing_evidence"
    MISSING_ACTOR_ID = "missing_actor_id"
    MISSING_RUN_ID = "missing_run_id"
    MISSING_AUTHORITY = "missing_authority"
    UNKNOWN_ACTION = "unknown_action"
    UNKNOWN_RISK_DOMAIN = "unknown_risk_domain"
    UNREADABLE_POLICY = "unreadable_policy"
    POLICY_MODE_STRICT_BLOCKED = "policy_mode_strict_blocked"
    POLICY_MODE_WARN_WARNED = "policy_mode_warn_warned"
    ESCALATED_TO_USER = "escalated_to_user"
    REMOTE_INTENT_UNVERIFIED = "remote_intent_unverified"
    REPLAY_METADATA_MISSING = "replay_metadata_missing"
    SCHEMA_VERSION_MISMATCH = "schema_version_mismatch"


class WorkbenchPolicyVerdictError(Exception):
    """Typed fail-closed signal for policy verdict loading or evaluation."""

    def __init__(self, reason_code: PolicyReasonCode, message: str = "") -> None:
        self.reason_code = reason_code
        self.message = message or reason_code.value
        super().__init__(str(self))

    def __str__(self) -> str:
        return f"WorkbenchPolicyVerdictError[{self.reason_code.value}]: {self.message}"


@dataclass(frozen=True, slots=True)
class EvidenceLink:
    """Reference to a durable evidence object that justifies a verdict."""

    evidence_id: str
    kind: str
    ref: str
    summary: str

    def __post_init__(self) -> None:
        allowed = {"run", "trace", "lease", "receipt", "proposal", "asset", "external"}
        if not self.evidence_id.strip() or not self.ref.strip():
            raise WorkbenchPolicyVerdictError(PolicyReasonCode.MISSING_EVIDENCE, "evidence_id and ref are required")
        if self.kind not in allowed:
            raise WorkbenchPolicyVerdictError(
                PolicyReasonCode.MISSING_EVIDENCE, f"unsupported evidence kind {self.kind!r}"
            )

    def __repr__(self) -> str:
        """Return a compact diagnostic representation."""
        return f"EvidenceLink(evidence_id={self.evidence_id!r}, kind={self.kind!r}, ref={self.ref!r})"


@dataclass(frozen=True, slots=True)
class ReplayMetadata:
    """Metadata required to replay or audit a verdict later."""

    schema_version: str
    policy_version: str
    mode: str
    captured_at_utc: str
    policy_path_canonical: str = ""
    correlation_id: str = ""

    def __post_init__(self) -> None:
        required = {
            "schema_version": self.schema_version,
            "policy_version": self.policy_version,
            "mode": self.mode,
            "captured_at_utc": self.captured_at_utc,
        }
        missing = [name for name, value in required.items() if not str(value).strip()]
        if missing:
            raise WorkbenchPolicyVerdictError(
                PolicyReasonCode.REPLAY_METADATA_MISSING,
                f"missing replay metadata field(s): {', '.join(missing)}",
            )
        if self.schema_version != _SCHEMA_VERSION:
            raise WorkbenchPolicyVerdictError(
                PolicyReasonCode.SCHEMA_VERSION_MISMATCH,
                f"expected schema_version {_SCHEMA_VERSION}, got {self.schema_version!r}",
            )
        PolicyMode(self.mode)

    def __repr__(self) -> str:
        """Return a compact diagnostic representation."""
        return f"ReplayMetadata(schema_version={self.schema_version!r}, policy_version={self.policy_version!r}, mode={self.mode!r})"


@dataclass(frozen=True, slots=True)
class ActionInput:
    """Caller-provided action facts evaluated by the policy classifier."""

    action_id: str
    action_type: str
    actor_id: str
    run_id: str
    risk_domain: RiskDomain | str
    summary: str
    evidence_links: tuple[EvidenceLink, ...]
    authority_refs: tuple[str, ...] = ()
    details: dict[str, Any] = field(default_factory=dict)
    metadata: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not self.action_id.strip():
            raise WorkbenchPolicyVerdictError(PolicyReasonCode.UNKNOWN_ACTION, "action_id is required")
        object.__setattr__(self, "evidence_links", tuple(self.evidence_links))
        object.__setattr__(self, "authority_refs", tuple(str(ref) for ref in self.authority_refs if str(ref).strip()))

    def __repr__(self) -> str:
        """Return a compact diagnostic representation."""
        return (
            f"ActionInput(action_id={self.action_id!r}, action_type={self.action_type!r}, actor_id={self.actor_id!r})"
        )


@dataclass(frozen=True, slots=True)
class ActionVerdict:
    """Stable policy verdict emitted for a command, tool call, or agent action."""

    verdict_id: str
    value: VerdictValue
    mode: PolicyMode
    risk_domain: RiskDomain
    reason_code: PolicyReasonCode
    action_id: str
    actor_id: str
    run_id: str
    evidence_links: tuple[EvidenceLink, ...]
    replay_metadata: ReplayMetadata
    policy_version: str
    evaluated_at_utc: str
    summary: str
    details: dict[str, Any]
    schema_version: str = _SCHEMA_VERSION

    def __post_init__(self) -> None:
        required = {
            "verdict_id": self.verdict_id,
            "action_id": self.action_id,
            "actor_id": self.actor_id,
            "run_id": self.run_id,
            "policy_version": self.policy_version,
            "evaluated_at_utc": self.evaluated_at_utc,
            "summary": self.summary,
        }
        missing = [name for name, value in required.items() if not str(value).strip()]
        if missing:
            raise WorkbenchPolicyVerdictError(
                PolicyReasonCode.REPLAY_METADATA_MISSING,
                f"missing verdict field(s): {', '.join(missing)}",
            )
        if self.schema_version != _SCHEMA_VERSION:
            raise WorkbenchPolicyVerdictError(PolicyReasonCode.SCHEMA_VERSION_MISMATCH)
        object.__setattr__(self, "evidence_links", tuple(self.evidence_links))

    def to_schema_payload(self) -> dict[str, Any]:
        """Return a JSON-schema-ready representation of this verdict.

        Returns:
            dict[str, Any] value produced by to_schema_payload().
        """
        payload = asdict(self)
        payload["value"] = self.value.value
        payload["mode"] = self.mode.value
        payload["risk_domain"] = self.risk_domain.value
        payload["reason_code"] = self.reason_code.value
        payload["evidence_links"] = list(payload["evidence_links"])
        payload["replay_metadata"]["mode"] = self.replay_metadata.mode
        return payload

    def __repr__(self) -> str:
        """Return a compact diagnostic representation."""
        return f"ActionVerdict(verdict_id={self.verdict_id!r}, value={self.value!r}, mode={self.mode!r})"


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _sanitize_policy_path(raw: str | os.PathLike[str]) -> Path:
    """Reject caller-supplied policy paths that can escape the repository."""
    if raw is None or str(raw).strip() == "":
        raise WorkbenchPolicyVerdictError(PolicyReasonCode.UNREADABLE_POLICY, "policy path is empty")
    candidate = Path(str(raw))
    if candidate.is_absolute():
        raise WorkbenchPolicyVerdictError(PolicyReasonCode.UNREADABLE_POLICY, "absolute policy paths are rejected")
    if any(part == ".." for part in candidate.parts):
        raise WorkbenchPolicyVerdictError(PolicyReasonCode.UNREADABLE_POLICY, "policy path traversal is rejected")
    root = Path.cwd().resolve()
    resolved = (root / candidate).resolve()
    if not resolved.is_relative_to(root):
        raise WorkbenchPolicyVerdictError(PolicyReasonCode.UNREADABLE_POLICY, "policy path escapes repository root")
    return resolved


def _resolve_policy_path(path: Path | str | None = None) -> Path:
    if path is None:
        return (Path.cwd() / _DEFAULT_POLICY_DIR / _POLICY_FILENAME).resolve()
    candidate = Path(path)
    if len(candidate.parts) == 1:
        candidate = Path("config") / "workbench" / candidate
    return _sanitize_policy_path(candidate)


def load_policy_verdicts_config(path: Path | str | None = None) -> dict[str, Any]:
    """Load and validate the policy verdict YAML document, failing closed.

    Returns:
        Resolved policy verdicts config value.

    Raises:
        Exception: Propagates validation or runtime failures from the underlying operation.
    """
    policy_path = _resolve_policy_path(path)
    if not policy_path.exists():
        raise WorkbenchPolicyVerdictError(PolicyReasonCode.UNREADABLE_POLICY, f"policy file not found: {policy_path}")
    try:
        with policy_path.open("r", encoding="utf-8") as handle:
            doc = yaml.safe_load(handle)
    except PermissionError as exc:
        raise WorkbenchPolicyVerdictError(
            PolicyReasonCode.UNREADABLE_POLICY, f"policy file unreadable: {policy_path}"
        ) from exc
    except (OSError, yaml.YAMLError) as exc:
        raise WorkbenchPolicyVerdictError(PolicyReasonCode.UNREADABLE_POLICY, f"policy file invalid: {exc}") from exc
    if not isinstance(doc, dict):
        raise WorkbenchPolicyVerdictError(PolicyReasonCode.UNREADABLE_POLICY, "policy root must be a mapping")
    if doc.get("schema_version") != _SCHEMA_VERSION:
        raise WorkbenchPolicyVerdictError(
            PolicyReasonCode.SCHEMA_VERSION_MISMATCH,
            f"expected schema_version {_SCHEMA_VERSION}, got {doc.get('schema_version')!r}",
        )
    if not str(doc.get("policy_version", "")).strip():
        raise WorkbenchPolicyVerdictError(PolicyReasonCode.UNREADABLE_POLICY, "policy_version is required")
    try:
        PolicyMode(str(doc.get("default_mode", PolicyMode.OBSERVE.value)))
    except ValueError as exc:
        raise WorkbenchPolicyVerdictError(
            PolicyReasonCode.UNREADABLE_POLICY,
            f"default_mode is not one of {[mode.value for mode in PolicyMode]}",
        ) from exc
    domain_rules = doc.get("domain_rules")
    if not isinstance(domain_rules, dict):
        raise WorkbenchPolicyVerdictError(PolicyReasonCode.UNREADABLE_POLICY, "domain_rules mapping is required")
    required = {domain.value for domain in RiskDomain if domain is not RiskDomain.UNKNOWN}
    missing = required.difference(domain_rules)
    if missing:
        raise WorkbenchPolicyVerdictError(
            PolicyReasonCode.UNREADABLE_POLICY,
            f"domain_rules missing required domain(s): {', '.join(sorted(missing))}",
        )
    return doc


def classify_action(
    action: ActionInput,
    *,
    config: dict[str, Any] | None = None,
    mode: PolicyMode | str | None = None,
) -> ActionVerdict:
    """Classify action for Vetinari callers.

    Args:
        action: Action value consumed by classify_action().
        config: Config value consumed by classify_action().
        mode: Mode value consumed by classify_action().

    Returns:
        Value produced for the caller.

    Raises:
        WorkbenchPolicyVerdictError: Propagated when validation, persistence, or execution fails.
    """
    policy = config if config is not None else load_policy_verdicts_config()
    policy_version = str(policy["policy_version"])
    effective_mode = _coerce_policy_mode(mode or policy.get("default_mode", PolicyMode.OBSERVE.value))
    policy_path = str(policy.get("_policy_path", ""))
    replay = ReplayMetadata(
        schema_version=_SCHEMA_VERSION,
        policy_version=policy_version,
        mode=effective_mode.value,
        captured_at_utc=_utc_now_iso(),
        policy_path_canonical=policy_path,
        correlation_id=str(action.metadata.get("correlation_id", "")),
    )
    if not action.actor_id.strip():
        raise WorkbenchPolicyVerdictError(PolicyReasonCode.MISSING_ACTOR_ID, "actor_id is required")
    if not action.run_id.strip():
        raise WorkbenchPolicyVerdictError(PolicyReasonCode.MISSING_RUN_ID, "run_id is required")
    if not action.evidence_links:
        raise WorkbenchPolicyVerdictError(PolicyReasonCode.MISSING_EVIDENCE, "at least one evidence link is required")
    risk_domain = _coerce_risk_domain(action.risk_domain)
    if risk_domain is RiskDomain.UNKNOWN:
        return _make_verdict(
            action, risk_domain, VerdictValue.ESCALATE, PolicyReasonCode.UNKNOWN_RISK_DOMAIN, replay, policy
        )
    if risk_domain is RiskDomain.REMOTE_CONTROL and not bool(action.metadata.get("remote_intent_verified")):
        return _make_verdict(
            action,
            risk_domain,
            VerdictValue.BLOCK,
            PolicyReasonCode.REMOTE_INTENT_UNVERIFIED,
            replay,
            policy,
        )
    if risk_domain in {RiskDomain.PERMISSION, RiskDomain.APPROVAL} and not action.authority_refs:
        return _make_verdict(
            action, risk_domain, VerdictValue.ESCALATE, PolicyReasonCode.MISSING_AUTHORITY, replay, policy
        )
    if _matches_blocked_rule(action, policy, risk_domain):
        return _make_verdict(
            action,
            risk_domain,
            VerdictValue.BLOCK,
            PolicyReasonCode.POLICY_MODE_STRICT_BLOCKED,
            replay,
            policy,
        )
    return _make_verdict(action, risk_domain, VerdictValue.ALLOW, PolicyReasonCode.ALLOWED, replay, policy)


def record_action_verdict(
    verdict: ActionVerdict,
    *,
    project_id: str = "default",
    receipt_store: WorkReceiptStore | None = None,
) -> WorkReceipt:
    """Emit exactly one existing POLICY_DECISION receipt for an action verdict.

    Returns:
        Outcome produced by record_action_verdict().
    """
    canonical_project_id = canonicalize_project_id(project_id)
    decision = GatewayPolicyDecision(
        decision_id=verdict.verdict_id,
        kind=_decision_kind(verdict.risk_domain),
        passed=verdict.value is VerdictValue.ALLOW,
        action=_guardrail_action(verdict.value),
        profile_id="workbench-policy-verdicts",
        lane=None,
        run_id=verdict.run_id,
        trace_id=_first_ref(verdict.evidence_links, "trace"),
        lease_id=_first_ref(verdict.evidence_links, "lease"),
        reason=verdict.reason_code.value,
        evaluated_at_utc=verdict.evaluated_at_utc,
        inputs_summary=f"{verdict.risk_domain.value}|{verdict.action_id}|{verdict.actor_id}",
        outputs_summary=f"{verdict.value.value}|{verdict.reason_code.value}",
        details=verdict.to_schema_payload(),
    )
    return record_policy_decision(decision, project_id=canonical_project_id, receipt_store=receipt_store)


class WorkbenchPolicyVerdicts:
    """Facade that classifies actions and records durable verdict receipts."""

    def __init__(
        self,
        *,
        policy_path: Path | str | None = None,
        project_id: str = "default",
        receipt_store: WorkReceiptStore | None = None,
    ) -> None:
        self._policy_path = _resolve_policy_path(policy_path)
        self._policy = load_policy_verdicts_config(self._policy_path.relative_to(Path.cwd()))
        self._policy["_policy_path"] = str(self._policy_path)
        self._project_id = canonicalize_project_id(project_id)
        self._receipt_store = receipt_store or WorkReceiptStore()
        self._decision_lock = threading.Lock()
        self._recent_verdicts: deque[ActionVerdict] = deque(maxlen=1024)

    def classify(self, action: ActionInput, *, mode: PolicyMode | str | None = None) -> ActionVerdict:
        """Execute the classify operation.

        Returns:
            ActionVerdict value produced by classify().
        """
        verdict = classify_action(action, config=self._policy, mode=mode)
        self._recent_verdicts.append(verdict)
        return verdict

    def record(self, verdict: ActionVerdict) -> WorkReceipt:
        return record_action_verdict(verdict, project_id=self._project_id, receipt_store=self._receipt_store)

    def classify_and_record(self, action: ActionInput, *, mode: PolicyMode | str | None = None) -> WorkReceipt:
        """Execute the classify and record operation.

        Returns:
            WorkReceipt value produced by classify_and_record().
        """
        with self._decision_lock:
            verdict = self.classify(action, mode=mode)
            return self.record(verdict)

    def list_recent_verdicts(self) -> tuple[ActionVerdict, ...]:
        return tuple(self._recent_verdicts)


_INSTANCE: WorkbenchPolicyVerdicts | None = None
_INSTANCE_LOCK = threading.Lock()


def get_workbench_policy_verdicts() -> WorkbenchPolicyVerdicts:
    """Return the process singleton using double-checked locking.

    Returns:
        Resolved workbench policy verdicts value.
    """
    global _INSTANCE
    if _INSTANCE is None:
        with _INSTANCE_LOCK:
            if _INSTANCE is None:
                _INSTANCE = WorkbenchPolicyVerdicts()
    return _INSTANCE


def reset_workbench_policy_verdicts_for_test() -> None:
    """Reset the singleton for deterministic tests."""
    global _INSTANCE
    with _INSTANCE_LOCK:
        _INSTANCE = None


def verdict_from_watcher_decision(
    decision: Any,
    *,
    policy_version: str,
    evidence_links: tuple[EvidenceLink, ...],
) -> ActionVerdict:
    """Convert a watcher runtime decision into the shared verdict object.

    Returns:
        ActionVerdict value produced by verdict_from_watcher_decision().
    """
    return _verdict_from_watcher_decision(
        decision,
        policy_version=policy_version,
        evidence_links=evidence_links,
    )


def verdict_from_gateway_policy_decision(
    decision: GatewayPolicyDecision,
    *,
    risk_domain: RiskDomain,
    policy_version: str,
    evidence_links: tuple[EvidenceLink, ...],
) -> ActionVerdict:
    """Convert an existing gateway policy decision into the shared verdict object.

    Returns:
        ActionVerdict value produced by verdict_from_gateway_policy_decision().
    """
    return _verdict_from_gateway_policy_decision(
        decision,
        risk_domain=risk_domain,
        policy_version=policy_version,
        evidence_links=evidence_links,
    )


def _coerce_risk_domain(value: RiskDomain | str | None) -> RiskDomain:
    if isinstance(value, RiskDomain):
        return value
    raw_value = value.value if isinstance(value, Enum) else value
    try:
        return RiskDomain(raw_value)
    except ValueError:
        logger.warning("Handled recoverable failure before fallback.", exc_info=True)
        return RiskDomain.UNKNOWN


def _coerce_policy_mode(value: PolicyMode | str) -> PolicyMode:
    if isinstance(value, PolicyMode):
        return value
    raw_value = value.value if isinstance(value, Enum) else value
    return PolicyMode(raw_value)


def _make_verdict(
    action: ActionInput,
    risk_domain: RiskDomain,
    value: VerdictValue,
    reason: PolicyReasonCode,
    replay: ReplayMetadata,
    policy: dict[str, Any],
) -> ActionVerdict:
    return _make_verdict_impl(action, risk_domain, value, reason, replay, policy)


def _matches_blocked_rule(action: ActionInput, policy: dict[str, Any], risk_domain: RiskDomain) -> bool:
    return _matches_blocked_rule_impl(action, policy, risk_domain)
