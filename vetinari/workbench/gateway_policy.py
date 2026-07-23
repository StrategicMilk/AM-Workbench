"""Fail-closed workbench gateway-policy authority.

The gateway policy loads virtual-profile, fallback-chain, cache, budget,
timeout, and guardrail rules from ``config/gateway_policy.yaml`` on explicit
construction. It evaluates caller-provided routing context into a typed
``GatewayPolicyDecision`` and emits exactly one ``WorkReceiptKind.POLICY_DECISION``
receipt per evaluation through the existing ``WorkReceiptStore`` corpus.

Side effects: construction reads the policy YAML; every ``evaluate_*`` call
appends a receipt. The receipt actor is ``AgentType.WORKBENCH``, introduced by
the upstream workbench metadata-spine pack.
"""

from __future__ import annotations

import os
import threading
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Any
from uuid import uuid4

import yaml

from vetinari.agents.contracts import OutcomeSignal, Provenance, ToolEvidence
from vetinari.receipts.record import WorkReceipt, WorkReceiptKind
from vetinari.receipts.store import WorkReceiptStore
from vetinari.runtime.workbench_scheduler import Lane
from vetinari.types import AgentType, EvidenceBasis

_DEFAULT_POLICY_DIR = Path("config")
_POLICY_FILENAME = "gateway_policy.yaml"
_SCHEMA_VERSION = 1
_RECEIPT_ACTOR = AgentType.WORKBENCH


class GatewayPolicyError(Exception):
    """Typed fail-closed signal for gateway policy loading or evaluation."""

    def __init__(self, reason: str, *, path: Path | None = None) -> None:
        self.reason = reason
        self.path = path
        super().__init__(str(self))

    def __str__(self) -> str:
        if self.path is None:
            return f"GatewayPolicyError: {self.reason}"
        return f"GatewayPolicyError: {self.reason} (path={self.path})"


class GuardrailAction(str, Enum):
    """Actions available when a gateway guardrail does not simply allow."""

    BLOCK = "block"
    LOG = "log"
    RETRY = "retry"
    FALLBACK = "fallback"
    EVAL_DATASET = "eval_dataset"
    HUMAN_APPROVAL = "human_approval"


class PolicyDecisionKind(str, Enum):
    """Gateway policy decision categories recorded as workbench receipts."""

    ROUTE = "route"
    CACHE = "cache"
    BUDGET = "budget"
    GUARDRAIL_PRE = "guardrail_pre"
    GUARDRAIL_POST = "guardrail_post"


@dataclass(frozen=True, slots=True)
class GatewayPolicyDecision:
    """One gateway policy verdict ready for receipt emission."""

    decision_id: str
    kind: PolicyDecisionKind
    passed: bool
    action: GuardrailAction | None
    profile_id: str | None
    lane: Lane | None
    run_id: str | None
    trace_id: str | None
    lease_id: str | None
    reason: str
    evaluated_at_utc: str
    inputs_summary: str
    outputs_summary: str
    details: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.passed and self.action not in (None, GuardrailAction.LOG):
            raise ValueError("passed=True permits only no action or log action")
        if not self.passed and self.action is None:
            raise ValueError("passed=False requires a guardrail action")
        if len(self.inputs_summary) > 200:
            raise ValueError("inputs_summary exceeds 200 chars")
        if len(self.outputs_summary) > 200:
            raise ValueError("outputs_summary exceeds 200 chars")

    def __repr__(self) -> str:
        return (
            f"GatewayPolicyDecision(decision_id={self.decision_id!r}, kind={self.kind.value!r}, "
            f"passed={self.passed!r}, action={self.action.value if self.action else None!r})"
        )


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _truncate(value: str, limit: int = 200) -> str:
    return value if len(value) <= limit else value[: limit - 3] + "..."


def _coerce_action(raw: Any, *, fallback: GuardrailAction = GuardrailAction.BLOCK) -> GuardrailAction:
    if raw is None or raw == "":
        return fallback
    value = str(raw)
    values = {item.value for item in GuardrailAction}
    return GuardrailAction(value) if value in values else fallback


def _sanitize_policy_path(raw: str | os.PathLike[str], *, root: Path = _DEFAULT_POLICY_DIR) -> Path:
    """Reject traversal-style policy path inputs before filesystem reads."""
    if raw is None or str(raw).strip() == "":
        raise GatewayPolicyError("policy path is empty")
    candidate = Path(str(raw))
    if candidate.is_absolute():
        raise GatewayPolicyError("policy path must be relative; absolute path rejected", path=candidate)
    if any(part == ".." for part in candidate.parts):
        raise GatewayPolicyError("policy path contains parent-directory traversal `..`; rejected", path=candidate)
    root_resolved = root.resolve()
    resolved = (root_resolved / candidate).resolve()
    if not resolved.is_relative_to(root_resolved):
        raise GatewayPolicyError("policy path resolves outside the allowed root", path=candidate)
    return resolved


def _resolve_policy_path(path: Path | str | None) -> Path:
    if path is None:
        return (_DEFAULT_POLICY_DIR / _POLICY_FILENAME).resolve()
    candidate = Path(path)
    if candidate.is_absolute():
        if any(part == ".." for part in candidate.parts):
            raise GatewayPolicyError("policy path contains parent-directory traversal `..`; rejected", path=candidate)
        return candidate.resolve()
    return _sanitize_policy_path(candidate)


def load_gateway_policy(path: Path | str | None = None) -> dict[str, Any]:
    """Load and validate the gateway policy document, failing closed.

    Returns:
        Parsed YAML mapping with ``schema_version`` and ``profiles`` validated.

    Raises:
        GatewayPolicyError: If the policy file is missing, unreadable, invalid
            YAML, wrong schema version, or missing required top-level keys.
    """
    policy_path = _resolve_policy_path(path)
    if not policy_path.exists():
        raise GatewayPolicyError("gateway policy file not found", path=policy_path)
    try:
        with policy_path.open("r", encoding="utf-8") as fh:
            doc = yaml.safe_load(fh)
    except PermissionError as exc:
        raise GatewayPolicyError("gateway policy file is unreadable", path=policy_path) from exc
    except OSError as exc:
        raise GatewayPolicyError(f"gateway policy file could not be read: {exc}", path=policy_path) from exc
    except yaml.YAMLError as exc:
        raise GatewayPolicyError(f"invalid YAML in gateway policy: {exc}", path=policy_path) from exc
    if not isinstance(doc, dict):
        raise GatewayPolicyError("gateway policy root must be a YAML mapping", path=policy_path)
    if doc.get("schema_version") != _SCHEMA_VERSION:
        raise GatewayPolicyError(
            f"gateway policy schema version mismatch: expected {_SCHEMA_VERSION}, got {doc.get('schema_version')!r}",
            path=policy_path,
        )
    profiles = doc.get("profiles")
    if not isinstance(profiles, list):
        raise GatewayPolicyError("gateway policy must contain a `profiles` list", path=policy_path)
    for profile in profiles:
        if not isinstance(profile, dict) or not str(profile.get("id", "")).strip():
            raise GatewayPolicyError("every gateway policy profile must be a mapping with an id", path=policy_path)
    return doc


def _decision_payload(decision: GatewayPolicyDecision) -> dict[str, Any]:
    payload = asdict(decision)
    payload["kind"] = decision.kind.value
    payload["action"] = decision.action.value if decision.action else None
    payload["lane"] = decision.lane.value if decision.lane else None
    return payload


def record_policy_decision(
    decision: GatewayPolicyDecision,
    *,
    project_id: str = "default",
    receipt_store: WorkReceiptStore | None = None,
) -> WorkReceipt:
    """Append one POLICY_DECISION receipt for a gateway policy verdict.

    Returns:
        The immutable receipt that was appended to the store.
    """
    store = receipt_store or WorkReceiptStore()
    now = _utc_now_iso()
    action = decision.action.value if decision.action else "allow"
    outcome = OutcomeSignal(
        passed=decision.passed,
        score=1.0 if decision.passed else 0.0,
        basis=EvidenceBasis.TOOL_EVIDENCE,
        tool_evidence=(
            ToolEvidence(
                tool_name="WorkbenchGatewayPolicy",
                command=f"evaluate:{decision.kind.value}:{decision.profile_id or 'unknown'}",
                exit_code=0 if decision.passed else 1,
                stdout_snippet=_truncate(f"action={action}|reason={decision.reason}|passed={decision.passed}"),
                passed=decision.passed,
            ),
        ),
        provenance=Provenance(
            source="workbench_gateway_policy",
            timestamp_utc=now,
            tool_name="WorkbenchGatewayPolicy",
        ),
        issues=() if decision.passed else (decision.reason,),
    )
    receipt = WorkReceipt(
        project_id=project_id,
        agent_id="workbench-gateway-policy",
        agent_type=_RECEIPT_ACTOR,
        kind=WorkReceiptKind.POLICY_DECISION,
        outcome=outcome,
        started_at_utc=decision.evaluated_at_utc,
        finished_at_utc=now,
        inputs_summary=_truncate(
            f"{decision.kind.value}|profile={decision.profile_id or 'unknown'}|{decision.inputs_summary}"
        ),
        outputs_summary=_truncate(f"action={action}|reason={decision.reason}|passed={decision.passed}"),
        linked_claim_ids=tuple(str(v) for v in (decision.run_id, decision.trace_id, decision.lease_id) if v),
    )
    store.append(receipt)
    return receipt


class WorkbenchGatewayPolicy:
    """Evaluate gateway policy decisions and emit durable receipts."""

    _receipt_store_type = WorkReceiptStore
    _receipt_kind = WorkReceiptKind.POLICY_DECISION

    def __init__(
        self,
        *,
        policy_path: Path | str | None = None,
        project_id: str = "default",
        receipt_store: WorkReceiptStore | None = None,
        spine: Any | None = None,
    ) -> None:
        self._policy_path = _resolve_policy_path(policy_path)
        self._policy = load_gateway_policy(self._policy_path)
        self._project_id = project_id
        self._receipt_store = receipt_store or WorkReceiptStore()
        self._spine = spine
        self._decision_lock = threading.Lock()

    def _profiles(self) -> list[dict[str, Any]]:
        return [p for p in self._policy.get("profiles", []) if isinstance(p, dict)]

    def _find_profile(self, profile_id: str | None) -> dict[str, Any] | None:
        profiles = self._profiles()
        if not profiles:
            return None
        wanted = profile_id or str(profiles[0].get("id", ""))
        for profile in profiles:
            if profile.get("id") == wanted:
                return profile
        return None

    def _make_decision(
        self,
        *,
        kind: PolicyDecisionKind,
        passed: bool,
        action: GuardrailAction | None,
        profile_id: str | None,
        reason: str,
        inputs_summary: str,
        lane: Lane | None = None,
        run_id: str | None = None,
        trace_id: str | None = None,
        lease_id: str | None = None,
        details: dict[str, Any] | None = None,
    ) -> GatewayPolicyDecision:
        return GatewayPolicyDecision(
            decision_id=uuid4().hex,
            kind=kind,
            passed=passed,
            action=action,
            profile_id=profile_id,
            lane=lane,
            run_id=run_id,
            trace_id=trace_id,
            lease_id=lease_id,
            reason=reason,
            evaluated_at_utc=_utc_now_iso(),
            inputs_summary=_truncate(inputs_summary),
            outputs_summary=_truncate(f"{kind.value}:{reason}"),
            details=details or {},
        )

    def _record(self, decision: GatewayPolicyDecision) -> GatewayPolicyDecision:
        # Keep the explicit enum reference here for source-level wiring checks:
        # WorkReceiptKind.POLICY_DECISION is the only receipt kind emitted.
        assert self._receipt_kind is WorkReceiptKind.POLICY_DECISION
        record_policy_decision(decision, project_id=self._project_id, receipt_store=self._receipt_store)
        return decision

    def evaluate_route(
        self,
        *,
        profile_id: str | None,
        model_id: str,
        inputs_summary: str = "",
        lane: Lane | None = None,
        run_id: str | None = None,
        trace_id: str | None = None,
        lease_id: str | None = None,
    ) -> GatewayPolicyDecision:
        """Evaluate whether a model route is allowed for a profile.

        Returns:
            GatewayPolicyDecision value produced by evaluate_route().
        """
        with self._decision_lock:
            profile = self._find_profile(profile_id)
            if profile is None:
                return self._record(
                    self._make_decision(
                        kind=PolicyDecisionKind.ROUTE,
                        passed=False,
                        action=GuardrailAction.BLOCK,
                        profile_id=profile_id,
                        lane=lane,
                        run_id=run_id,
                        trace_id=trace_id,
                        lease_id=lease_id,
                        reason="profile not found",
                        inputs_summary=inputs_summary,
                    )
                )
            fallback_chain = [str(item) for item in profile.get("fallback_chain", [])]
            passed = not fallback_chain or model_id in fallback_chain
            return self._record(
                self._make_decision(
                    kind=PolicyDecisionKind.ROUTE,
                    passed=passed,
                    action=None if passed else GuardrailAction.FALLBACK,
                    profile_id=str(profile.get("id")),
                    lane=lane,
                    run_id=run_id,
                    trace_id=trace_id,
                    lease_id=lease_id,
                    reason="route allowed" if passed else "model outside fallback chain",
                    inputs_summary=inputs_summary or f"model={model_id}",
                    details={"model_id": model_id, "fallback_chain": fallback_chain},
                )
            )

    def evaluate_cache(
        self, *, profile_id: str | None, cache_key: str, inputs_summary: str = ""
    ) -> GatewayPolicyDecision:
        """Evaluate whether cache use is allowed for a profile.

        Returns:
            GatewayPolicyDecision value produced by evaluate_cache().
        """
        with self._decision_lock:
            profile = self._find_profile(profile_id)
            cache = profile.get("cache", {}) if profile else {}
            passed = bool(profile) and bool(cache)
            return self._record(
                self._make_decision(
                    kind=PolicyDecisionKind.CACHE,
                    passed=passed,
                    action=None if passed else GuardrailAction.BLOCK,
                    profile_id=str(profile.get("id")) if profile else profile_id,
                    reason="cache policy present" if passed else "cache policy unavailable",
                    inputs_summary=inputs_summary or f"cache_key={cache_key}",
                    details={"cache_key": cache_key, "cache": cache},
                )
            )

    def evaluate_budget(
        self,
        *,
        profile_id: str | None,
        cost_estimate_usd: float,
        inputs_summary: str = "",
    ) -> GatewayPolicyDecision:
        """Evaluate a cost estimate against the profile budget caps.

        Returns:
            GatewayPolicyDecision value produced by evaluate_budget().
        """
        with self._decision_lock:
            profile = self._find_profile(profile_id)
            caps = profile.get("budget_caps", {}) if profile else {}
            daily_cap = caps.get("daily_usd")
            passed = bool(profile) and (daily_cap is None or float(cost_estimate_usd) <= float(daily_cap))
            return self._record(
                self._make_decision(
                    kind=PolicyDecisionKind.BUDGET,
                    passed=passed,
                    action=None if passed else GuardrailAction.BLOCK,
                    profile_id=str(profile.get("id")) if profile else profile_id,
                    reason="budget allowed" if passed else "budget cap exceeded or unavailable",
                    inputs_summary=inputs_summary or f"cost_estimate_usd={cost_estimate_usd}",
                    details={"cost_estimate_usd": cost_estimate_usd, "budget_caps": caps},
                )
            )

    def evaluate_guardrail_pre(
        self,
        *,
        profile_id: str | None,
        payload_summary: str,
        inputs_summary: str = "",
    ) -> GatewayPolicyDecision:
        return self._evaluate_guardrail(
            kind=PolicyDecisionKind.GUARDRAIL_PRE,
            profile_id=profile_id,
            summary=payload_summary,
            inputs_summary=inputs_summary,
            policy_key="guardrails_pre",
        )

    def evaluate_guardrail_post(
        self,
        *,
        profile_id: str | None,
        output_summary: str,
        inputs_summary: str = "",
    ) -> GatewayPolicyDecision:
        return self._evaluate_guardrail(
            kind=PolicyDecisionKind.GUARDRAIL_POST,
            profile_id=profile_id,
            summary=output_summary,
            inputs_summary=inputs_summary,
            policy_key="guardrails_post",
        )

    def _evaluate_guardrail(
        self,
        *,
        kind: PolicyDecisionKind,
        profile_id: str | None,
        summary: str,
        inputs_summary: str,
        policy_key: str,
    ) -> GatewayPolicyDecision:
        with self._decision_lock:
            profile = self._find_profile(profile_id)
            rules = profile.get(policy_key, []) if profile else []
            if not profile:
                passed = False
                action = GuardrailAction.BLOCK
                reason = "profile not found"
            elif not rules:
                passed = True
                action = None
                reason = "no guardrail failures"
            else:
                first_rule = rules[0] if isinstance(rules[0], dict) else {}
                action = _coerce_action(first_rule.get("action"), fallback=GuardrailAction.BLOCK)
                passed = action is GuardrailAction.LOG
                reason = f"{policy_key} action={action.value}"
            return self._record(
                self._make_decision(
                    kind=kind,
                    passed=passed,
                    action=action,
                    profile_id=str(profile.get("id")) if profile else profile_id,
                    reason=reason,
                    inputs_summary=inputs_summary or summary,
                    details={policy_key: rules},
                )
            )

    def list_recent_decisions(
        self,
        *,
        project_id: str | None = None,
        kind: PolicyDecisionKind | None = None,
        limit: int | None = None,
    ) -> list[WorkReceipt]:
        """Return recent POLICY_DECISION receipts for a project.

        Returns:
            Collection of recent decisions values.
        """
        rows = [
            receipt
            for receipt in self._receipt_store.iter_receipts(project_id or self._project_id)
            if receipt.kind is WorkReceiptKind.POLICY_DECISION
        ]
        if kind is not None:
            rows = [receipt for receipt in rows if receipt.inputs_summary.startswith(kind.value)]
        if limit is not None:
            rows = rows[-limit:]
        return rows

    def list_active_profiles(self) -> list[dict[str, Any]]:
        return [dict(profile) for profile in self._profiles()]


_INSTANCE: WorkbenchGatewayPolicy | None = None
_INSTANCE_LOCK: threading.Lock = threading.Lock()


def get_workbench_gateway_policy() -> WorkbenchGatewayPolicy:
    """Return the lazily constructed gateway policy singleton.

    Returns:
        The process singleton, creating it on first use.
    """
    global _INSTANCE
    if _INSTANCE is None:
        with _INSTANCE_LOCK:
            if _INSTANCE is None:
                override = os.environ.get("VETINARI_GATEWAY_POLICY_PATH") or None
                _INSTANCE = WorkbenchGatewayPolicy(policy_path=override)
    return _INSTANCE


def reset_workbench_gateway_policy_for_test() -> None:
    """Clear the singleton under lock for deterministic tests."""
    global _INSTANCE
    with _INSTANCE_LOCK:
        _INSTANCE = None


__all__ = [
    "GatewayPolicyDecision",
    "GatewayPolicyError",
    "GuardrailAction",
    "PolicyDecisionKind",
    "WorkbenchGatewayPolicy",
    "get_workbench_gateway_policy",
    "load_gateway_policy",
    "record_policy_decision",
    "reset_workbench_gateway_policy_for_test",
]
