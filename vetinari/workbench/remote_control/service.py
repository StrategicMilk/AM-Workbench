"""Read-only service adapter for mobile companion decisions."""

from __future__ import annotations

import logging
from collections.abc import Callable
from typing import TYPE_CHECKING, Any, cast

from vetinari.workbench.agents.watchers.loop_cost import LoopCostWatcherAction, LoopCostWatcherDecision
from vetinari.workbench.policy.risk_context import (
    AffectedAsset,
    RiskContextEntryPoint,
    RiskRollbackStatus,
    ToolAuthoritySummary,
    build_risk_context,
    render_approval_risk_frame,
)
from vetinari.workbench.policy.verdicts import ActionInput, EvidenceLink, RiskDomain, VerdictValue, classify_action
from vetinari.workbench.policy_explainability import (
    BudgetSummary,
    ExposureSummary,
    PolicyExplanation,
    TraceSummary,
)

from .access import evaluate_remote_access
from .contracts import (
    RemoteApproval,
    RemoteControlDecision,
    RemoteControlDecisionValue,
    RemoteControlFailureReason,
    RemoteIntent,
    RemoteIntentKind,
)

logger = logging.getLogger(__name__)


if TYPE_CHECKING:
    from vetinari.workbench.mission_control import MissionControlSnapshot
    from vetinari.workbench.private_ai_appliance import RuntimeCockpitSnapshot

AccessEvaluator = Callable[[RemoteIntent], RemoteControlDecision]
PolicyClassifier = Callable[..., Any]
WatcherProvider = Callable[[RemoteIntent], LoopCostWatcherDecision | None]
SnapshotProvider = Callable[[str], Any | None]
RuntimeSnapshotProvider = Callable[[str], "RuntimeCockpitSnapshot | None"]


class RemoteControlService:
    """Evaluate mobile intents while desktop Workbench remains execution authority."""

    def __init__(
        self,
        *,
        access_evaluator: AccessEvaluator | None = None,
        policy_classifier: PolicyClassifier | None = None,
        policy_config: dict[str, Any] | None = None,
        watcher_provider: WatcherProvider | None = None,
        snapshot_provider: SnapshotProvider | None = None,
        runtime_snapshot_provider: RuntimeSnapshotProvider | None = None,
        current_policy_version: str | None = None,
    ) -> None:
        self._access_evaluator = access_evaluator
        self._policy_classifier = policy_classifier or classify_action
        self._policy_config = policy_config
        self._watcher_provider = watcher_provider
        self._snapshot_provider = snapshot_provider
        self._runtime_snapshot_provider = runtime_snapshot_provider
        self._current_policy_version = current_policy_version
        self._seen_intent_ids: set[str] = set()
        self._pending_approval_intent_ids: set[str] = set()
        self._used_approval_ids: set[str] = set()

    def evaluate_intent(self, intent: RemoteIntent, *, approval: RemoteApproval | None = None) -> RemoteControlDecision:
        """Return a companion decision; never execute the requested desktop side effect.

        Returns:
            RemoteControlDecision value produced by evaluate_intent().
        """
        if intent.intent_id in self._seen_intent_ids:
            return _block(RemoteControlFailureReason.DUPLICATE_INTENT, "duplicate remote intent rejected", intent)
        if intent.intent_id in self._pending_approval_intent_ids and approval is None:
            return _block(
                RemoteControlFailureReason.DUPLICATE_INTENT, "approval is already pending for remote intent", intent
            )

        access_decision = self._evaluate_access(intent)
        if not access_decision.allowed:
            return access_decision

        verdict = self._classify_policy(intent)
        if verdict is None:
            return _block(RemoteControlFailureReason.MISSING_POLICY_VERDICT, "policy verdict unavailable", intent)
        if verdict.value is not VerdictValue.ALLOW:
            return _block(
                RemoteControlFailureReason.POLICY_BLOCKED,
                f"policy verdict blocked: {verdict.reason_code.value}",
                intent,
            )

        watcher_decision = self._watcher_provider(intent) if self._watcher_provider else None
        watcher_gate = self._watcher_gate(watcher_decision, intent)
        if watcher_gate is not None and not watcher_gate.allowed:
            return watcher_gate

        risk_frame = self._risk_frame(intent, verdict)
        risk_decision = str(risk_frame["risk_frame"]["decision"])
        if risk_decision in {"deny", "degraded"}:
            return RemoteControlDecision(
                RemoteControlDecisionValue.DEGRADED
                if risk_decision == "degraded"
                else RemoteControlDecisionValue.BLOCK,
                "mobile risk context failed closed",
                (RemoteControlFailureReason.RISK_CONTEXT_DEGRADED,),
                evidence_refs=intent.evidence_refs,
                policy_version=intent.policy_version,
                payload={"risk_frame": risk_frame},
            )

        snapshot = self._snapshot_provider(intent.project_id) if self._snapshot_provider else None
        if snapshot is None:
            return _block(
                RemoteControlFailureReason.MISSION_CONTROL_UNAVAILABLE, "Mission Control snapshot unavailable", intent
            )

        approval_gate = self._approval_gate(intent, approval, risk_frame)
        if approval_gate is not None:
            if (
                intent.kind is RemoteIntentKind.APPROVE_ACTION
                and approval_gate.value is RemoteControlDecisionValue.REQUIRE_APPROVAL
            ):
                self._pending_approval_intent_ids.add(intent.intent_id)
            return approval_gate

        self._pending_approval_intent_ids.discard(intent.intent_id)
        self._seen_intent_ids.add(intent.intent_id)
        intent_kind = cast(RemoteIntentKind, intent.kind)
        payload = {
            "intent_id": intent.intent_id,
            "kind": intent_kind.value,
            "desktop_authority": "workbench",
            "advisory_only": intent_kind is RemoteIntentKind.APPROVE_ACTION,
            "watcher_evidence_refs": _watcher_evidence_refs(watcher_decision),
            "risk_frame": risk_frame,
            "mission_control": _mission_payload(snapshot, intent),
        }
        value = (
            RemoteControlDecisionValue.ADVISORY
            if intent_kind is RemoteIntentKind.APPROVE_ACTION
            else RemoteControlDecisionValue.ALLOW
        )
        return RemoteControlDecision(
            value,
            "mobile companion intent accepted for desktop-local Workbench evaluation",
            evidence_refs=tuple(dict.fromkeys((*intent.evidence_refs, *_watcher_evidence_refs(watcher_decision)))),
            policy_version=intent.policy_version,
            payload=payload,
        )

    def _classify_policy(self, intent: RemoteIntent) -> Any:
        intent_kind = cast(RemoteIntentKind, intent.kind)
        action = ActionInput(
            action_id=intent.intent_id,
            action_type=intent_kind.value,
            actor_id=intent.actor.actor_id,
            run_id=intent.run_id,
            risk_domain=RiskDomain.REMOTE_CONTROL,
            summary=f"mobile companion {intent_kind.value}",
            evidence_links=tuple(
                EvidenceLink(f"remote-{index}", "run", ref, "mobile companion intent evidence")
                for index, ref in enumerate(intent.evidence_refs, start=1)
            ),
            authority_refs=("desktop-workbench",),
            details={"mission_id": intent.mission_id, "project_id": intent.project_id},
            metadata={
                "remote_intent_verified": intent.remote_intent_verified,
                "correlation_id": intent.replay_metadata.get("nonce", ""),
            },
        )
        try:
            if self._policy_config is None:
                return self._policy_classifier(action)
            return self._policy_classifier(action, config=self._policy_config)
        except Exception:
            logger.warning("Handled recoverable failure before fallback.", exc_info=True)
            return None

    @staticmethod
    def _watcher_gate(
        decision: LoopCostWatcherDecision | None,
        intent: RemoteIntent,
    ) -> RemoteControlDecision | None:
        if decision is None or not decision.evidence_summary.any_refs():
            return _block(RemoteControlFailureReason.WATCHER_MISSING_EVIDENCE, "watcher evidence is required", intent)
        if decision.recommended_action is LoopCostWatcherAction.STRICT_BLOCK:
            return _block(
                RemoteControlFailureReason.WATCHER_STRICT_BLOCK,
                "watcher strict block overrides mobile approval",
                intent,
            )
        if decision.recommended_action is LoopCostWatcherAction.ASK_USER:
            return RemoteControlDecision(
                RemoteControlDecisionValue.REQUIRE_APPROVAL,
                "watcher requires desktop approval",
                evidence_refs=_watcher_evidence_refs(decision),
                policy_version=intent.policy_version,
                payload={"watcher_action": decision.recommended_action.value},
            )
        if decision.recommended_action is LoopCostWatcherAction.DOWNGRADE:
            return RemoteControlDecision(
                RemoteControlDecisionValue.DEGRADED,
                "watcher downgraded remote companion control",
                (RemoteControlFailureReason.RUNTIME_DEGRADED,),
                evidence_refs=_watcher_evidence_refs(decision),
                policy_version=intent.policy_version,
            )
        return None

    @staticmethod
    def _risk_frame(intent: RemoteIntent, verdict: Any) -> dict[str, Any]:
        context = build_risk_context(
            context_id=f"remote-risk-{intent.intent_id}",
            entry_point=RiskContextEntryPoint.MOBILE,
            verdict=verdict,
            affected_assets=(
                AffectedAsset(
                    asset_id=intent.mission_id,
                    kind="mission",
                    project_scope=intent.project_id,
                    operation="inspect" if intent.kind is RemoteIntentKind.INSPECT_MISSION else "control_request",
                    display_label=f"Mission {intent.mission_id}",
                ),
            ),
            tool_authority=ToolAuthoritySummary(
                tool_surface_id="workbench-remote-control",
                authority_refs=("desktop-workbench", intent.service_binding.service_id),
                capability_pack_refs=("workbench-remote-control-tailnet-access",),
                capability_diff_state="unchanged",
            ),
            rollback_status=RiskRollbackStatus(
                intent.parameters.get("rollback_status", RiskRollbackStatus.AVAILABLE.value)
            ),
            explanation=_policy_explanation(intent),
            recovery_note="desktop Workbench remains execution authority for side effects",
            audit_payload={"replay_ref": intent.replay_metadata["nonce"], "evidence_refs": list(intent.evidence_refs)},
            on_error="degrade",
        )
        return cast(dict[str, Any], render_approval_risk_frame(context))

    def _approval_gate(
        self,
        intent: RemoteIntent,
        approval: RemoteApproval | None,
        risk_frame: dict[str, Any],
    ) -> RemoteControlDecision | None:
        if intent.kind is not RemoteIntentKind.APPROVE_ACTION:
            return None
        if approval is None:
            return RemoteControlDecision(
                RemoteControlDecisionValue.REQUIRE_APPROVAL,
                "approval intent is advisory until approval metadata is supplied",
                evidence_refs=intent.evidence_refs,
                policy_version=intent.policy_version,
                payload={"risk_frame": risk_frame, "advisory_only": True},
            )
        if approval.approval_id in self._used_approval_ids:
            return _block(RemoteControlFailureReason.APPROVAL_REUSED, "approval id already used", intent)
        if approval.intent_id != intent.intent_id:
            return _block(
                RemoteControlFailureReason.DUPLICATE_INTENT, "approval is bound to a different intent", intent
            )
        if approval.device_id != intent.device.device_id:
            return _block(
                RemoteControlFailureReason.WRONG_DEVICE_IDENTITY, "approval device does not match intent device", intent
            )
        if approval.approved_by != intent.actor:
            return _block(
                RemoteControlFailureReason.APPROVAL_ACTOR_MISMATCH, "approval actor does not match intent actor", intent
            )
        if approval.policy_version != intent.policy_version or (
            self._current_policy_version is not None and approval.policy_version != self._current_policy_version
        ):
            return _block(RemoteControlFailureReason.STALE_POLICY_VERSION, "approval policy version is stale", intent)
        if approval.material_fingerprint != str(risk_frame["risk_frame"]["material_fingerprint"]):
            return _block(
                RemoteControlFailureReason.STALE_MATERIAL_FINGERPRINT, "approval material fingerprint is stale", intent
            )
        if approval.mission_snapshot_ref != intent.replay_metadata["mission_snapshot_ref"]:
            return _block(
                RemoteControlFailureReason.STALE_MISSION_SNAPSHOT, "approval mission snapshot is stale", intent
            )
        self._used_approval_ids.add(approval.approval_id)
        return None

    def _evaluate_access(self, intent: RemoteIntent) -> RemoteControlDecision:
        if self._access_evaluator is not None:
            return self._access_evaluator(intent)
        runtime_snapshot = (
            self._runtime_snapshot_provider(intent.project_id) if self._runtime_snapshot_provider else None
        )
        return evaluate_remote_access(
            intent,
            request_context={
                "trusted_proxy_path": intent.service_binding.trusted_proxy,
                "identity_headers": True,
            },
            cockpit_snapshot=runtime_snapshot,
        )


def _policy_explanation(intent: RemoteIntent) -> PolicyExplanation:
    return PolicyExplanation(
        allowed=True,
        policy_id="remote-control",
        policy_source="workbench_remote_control",
        decision_kind="allow",
        reasons=("remote control intent verified before desktop handoff",),
        denial_reasons=(),
        exposures=ExposureSummary(
            "no credential material",
            "none",
            "tailnet-control-plane-only",
            "no credentials exposed",
            "desktop-local",
        ),
        budget=BudgetSummary(
            scope=intent.run_id,
            policy_name=intent.policy_version,
            limit="not measured by remote-control contract",
            remaining="desktop policy decides",
            failure_behavior="deny-before-use",
        ),
        trace=TraceSummary(
            trace_id=intent.replay_metadata.get("nonce"),
            receipt_kind="remote_control_decision",
            will_record=True,
            retention_note="desktop execution authority records downstream receipt",
        ),
        failure_behavior="deny-before-use; remote service never executes desktop side effects",
        capability_pack_status="trusted",
        degraded=False,
    )


def _watcher_evidence_refs(decision: LoopCostWatcherDecision | None) -> tuple[str, ...]:
    if decision is None:
        return ()
    evidence = decision.evidence_summary
    return tuple(
        dict.fromkeys((
            *evidence.trace_event_refs,
            *evidence.policy_verdict_refs,
            *evidence.resource_counter_refs,
            *evidence.retry_signature_refs,
            *evidence.predicate_refs,
            *evidence.evidence_refs,
        ))
    )


def _mission_payload(snapshot: MissionControlSnapshot, intent: RemoteIntent) -> dict[str, Any]:
    return {
        "project_id": snapshot.project_id,
        "status": snapshot.status,
        "degraded": snapshot.degraded,
        "active_missions": [task.run_id for task in snapshot.agent_tasks],
        "paused": [task.run_id for task in snapshot.agent_tasks if task.paused],
        "evidence_links": [
            link for task in snapshot.agent_tasks if task.run_id == intent.run_id for link in task.evidence_links
        ],
        "summary": snapshot.degraded_reason or f"{len(snapshot.agent_tasks)} task(s) visible",
    }


def _block(reason: RemoteControlFailureReason, summary: str, intent: RemoteIntent) -> RemoteControlDecision:
    return RemoteControlDecision(
        RemoteControlDecisionValue.BLOCK,
        summary,
        (reason,),
        evidence_refs=intent.evidence_refs,
        policy_version=intent.policy_version,
    )


__all__ = ["RemoteControlService"]
