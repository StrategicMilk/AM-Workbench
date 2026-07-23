"""Fail-closed runtime for user-managed AM Workbench agents."""

from __future__ import annotations

import json
import logging
import threading
import uuid
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import yaml

from vetinari.constants import OUTPUTS_DIR, PROJECT_ROOT
from vetinari.workbench.agents.harness import (
    AgentRunRequest,
    NetworkExposure,
    ProcessExposure,
    SandboxProfile,
    admit_agent_run,
)
from vetinari.workbench.agents.templates import (
    AgentTemplateCard,
    AgentTemplateCatalogError,
    load_agent_template_gallery,
)
from vetinari.workbench.managed_agents.contracts import (
    BLOCKER_AGENT_PAUSED,
    BLOCKER_AGENT_RETIRED,
    BLOCKER_COST_CEILING_EXCEEDED,
    BLOCKER_DEPENDENCY_UNAVAILABLE,
    BLOCKER_STATE_UNREADABLE,
    BLOCKER_TEMPLATE_UNAVAILABLE,
    SCHEMA_VERSION,
    ManagedAgentDecision,
    ManagedAgentDecisionStatus,
    ManagedAgentDependencyRefs,
    ManagedAgentInstallRequest,
    ManagedAgentRecord,
    ManagedAgentRunRequest,
    ManagedAgentState,
)
from vetinari.workbench.managed_agents.runtime_support import (
    _fallback_config,
    _intervention_for_state,
    _intervention_summary,
    _iso,
    _memory_blockers,
    _operator_action_for_blockers,
    _project_scope_blocker,
    _replace_record,
    _tool_blockers,
)
from vetinari.workbench.spine_consumers import record_run_completed

logger = logging.getLogger(__name__)


DEFAULT_MANAGED_AGENT_CONFIG_PATH = PROJECT_ROOT / "config" / "workbench" / "managed_agents.yaml"
DEFAULT_MANAGED_AGENT_STATE_PATH = OUTPUTS_DIR / "workbench" / "spine" / "managed_agents" / "state.json"
_CONFIG_SCHEMA_VERSION = 1

TemplateLoader = Callable[[], tuple[AgentTemplateCard, ...]]


class ManagedAgentWorkspaceRuntime:
    """Single service boundary for managed-agent state and policy checks."""

    def __init__(
        self,
        *,
        config_path: str | Path | None = None,
        state_path: str | Path | None = None,
        template_loader: TemplateLoader = load_agent_template_gallery,
        now: Callable[[], datetime] | None = None,
    ) -> None:
        self._config_path = Path(config_path) if config_path is not None else DEFAULT_MANAGED_AGENT_CONFIG_PATH
        self._state_path = Path(state_path) if state_path is not None else DEFAULT_MANAGED_AGENT_STATE_PATH
        self._template_loader = template_loader
        self._now = now or (lambda: datetime.now(UTC))
        self._lock = threading.RLock()
        self._records: dict[str, ManagedAgentRecord] = {}
        self._damaged_reason = ""
        self._config_error = ""
        self._config = self._load_config()
        self._restore()

    def snapshot(self) -> dict[str, Any]:
        """Return a user-facing workspace snapshot without mutating state.

        Returns:
            dict[str, Any] value produced by snapshot().
        """
        with self._lock:
            status = "ready"
            degradation_reasons: list[str] = []
            if self._config_error:
                status = "degraded"
                degradation_reasons.append(self._config_error)
            if self._damaged_reason:
                status = "recovery_needed"
                degradation_reasons.append(self._damaged_reason)
            records = [record.to_dict() for record in sorted(self._records.values(), key=lambda item: item.agent_id)]
            return {
                "schema_version": SCHEMA_VERSION,
                "status": status,
                "degradation_reasons": degradation_reasons,
                "agents": records,
                "dependency_contracts": self._dependency_contracts(),
                "user_intervention": _intervention_summary(records, degradation_reasons),
            }

    def install_agent(self, request: ManagedAgentInstallRequest) -> ManagedAgentDecision:
        """Install or update one managed-agent record after all safety checks pass.

        Returns:
            ManagedAgentDecision value produced by install_agent().
        """
        with self._lock:
            guard = self._write_guard(request.agent_id)
            if guard is not None:
                return guard
            project_block = _project_scope_blocker(request.project_id)
            if project_block:
                return self._blocked(request.agent_id, (project_block,), "choose_safe_project_scope")
            card = self._template_by_id(request.template_id)
            if card is None:
                return self._blocked(
                    request.agent_id, (BLOCKER_TEMPLATE_UNAVAILABLE,), "refresh_agent_template_gallery"
                )
            tool_blockers = _tool_blockers(request.requested_tools, card.allowed_tools)
            memory_blockers = _memory_blockers(
                request.memory_scope,
                card.memory_scope,
                policy_receipt_ref=request.policy_receipt_ref,
            )
            dependency_blockers = self._dependency_blockers()
            blockers = (*tool_blockers, *memory_blockers, *dependency_blockers)
            if blockers:
                return self._blocked(request.agent_id, blockers, _operator_action_for_blockers(blockers))
            now = _iso(self._now())
            record = ManagedAgentRecord(
                schema_version=SCHEMA_VERSION,
                agent_id=request.agent_id,
                project_id=request.project_id,
                template_id=request.template_id,
                display_name=request.display_name,
                purpose=request.purpose,
                kind=request.kind,
                state=ManagedAgentState.ACTIVE,
                requested_tools=request.requested_tools,
                permissions=card.permissions,
                memory_scope=request.memory_scope,
                persona_ref=request.persona_ref or f"persona:{request.agent_id}",
                conversation_branch_ref=request.conversation_branch_ref
                or f"conversation:{request.project_id}:{request.agent_id}",
                policy_receipt_refs=(request.policy_receipt_ref,) if request.policy_receipt_ref else (),
                cost_ceiling_ref=request.cost_ceiling_ref,
                dependencies=self._dependencies_for(card, request),
                intervention=_intervention_for_state(
                    ManagedAgentState.ACTIVE, request.purpose, request.cost_ceiling_ref
                ),
                created_by=request.created_by,
                created_at_utc=now,
                updated_at_utc=now,
            )
            self._records[record.agent_id] = record
            self._persist_locked()
            return ManagedAgentDecision(
                status=ManagedAgentDecisionStatus.ACCEPTED,
                agent_id=record.agent_id,
                accepted=True,
                blockers=(),
                operator_action="inspect_pause_or_retire",
                record=record,
                evidence_refs=("template-gallery", "run-harness", "mailbox", "route-ledger", "watcher-runtime"),
            )

    def start_run(self, request: ManagedAgentRunRequest) -> ManagedAgentDecision:
        """Admit a run through lifecycle, template, harness, and receipt guards.

        Returns:
            ManagedAgentDecision value produced by start_run().
        """
        with self._lock:
            guard = self._write_guard(request.agent_id)
            if guard is not None:
                return guard
            record = self._records.get(request.agent_id)
            if record is None:
                return self._blocked(request.agent_id, (BLOCKER_TEMPLATE_UNAVAILABLE,), "install_agent_before_run")
            if record.state is ManagedAgentState.PAUSED:
                return self._blocked(request.agent_id, (BLOCKER_AGENT_PAUSED,), "resume_or_review_agent")
            if record.state is ManagedAgentState.RETIRED:
                return self._blocked(request.agent_id, (BLOCKER_AGENT_RETIRED,), "create_replacement_agent")
            card = self._template_by_id(record.template_id)
            if card is None:
                return self._blocked(
                    request.agent_id, (BLOCKER_TEMPLATE_UNAVAILABLE,), "refresh_agent_template_gallery"
                )
            tool_blockers = _tool_blockers(request.requested_tools, record.requested_tools)
            if tool_blockers:
                return self._blocked(request.agent_id, tool_blockers, "choose_tools_allowed_by_agent")
            cost_blocker = self._cost_ceiling_blocker(record)
            if cost_blocker:
                return self._blocked(
                    request.agent_id,
                    (cost_blocker,),
                    "raise_cost_ceiling_or_pause_agent",
                    run_id=request.run_id,
                )
            harness_request = AgentRunRequest(
                run_id=request.run_id,
                template_id=record.template_id,
                sandbox=self._sandbox_for(record, card),
                requested_tools=request.requested_tools,
                workspace_path=request.workspace_path,
                input_payload_ref=request.input_payload_ref,
                expected_output_ref=request.expected_output_ref,
                receipt_refs=request.receipt_refs,
            )
            admission = admit_agent_run(harness_request)
            if not admission.admitted:
                return self._blocked(
                    request.agent_id, tuple(admission.blockers), "satisfy_run_harness_admission", run_id=request.run_id
                )
            updated = _replace_record(
                record,
                run_ids=(*record.run_ids, request.run_id) if request.run_id not in record.run_ids else record.run_ids,
                updated_at_utc=_iso(self._now()),
                intervention=_intervention_for_state(record.state, record.purpose, record.cost_ceiling_ref),
            )
            self._records[record.agent_id] = updated
            self._persist_locked()
            return ManagedAgentDecision(
                status=ManagedAgentDecisionStatus.ACCEPTED,
                agent_id=record.agent_id,
                accepted=True,
                blockers=(),
                operator_action="monitor_run_or_pause_agent",
                record=updated,
                run_id=request.run_id,
                evidence_refs=("harness-admission", "watcher-runtime", "resource-lease", "trace-to-eval"),
            )

    def pause_agent(self, agent_id: str, *, reason: str, actor: str) -> ManagedAgentDecision:
        return self._set_state(agent_id, ManagedAgentState.PAUSED, reason=reason, actor=actor)

    def retire_agent(self, agent_id: str, *, reason: str, actor: str) -> ManagedAgentDecision:
        return self._set_state(agent_id, ManagedAgentState.RETIRED, reason=reason, actor=actor)

    def change_memory_scope(
        self,
        agent_id: str,
        *,
        memory_scope: tuple[str, ...],
        policy_receipt_ref: str,
    ) -> ManagedAgentDecision:
        """Change memory scope only with an explicit policy receipt.

        Returns:
            ManagedAgentDecision value produced by change_memory_scope().
        """
        with self._lock:
            guard = self._write_guard(agent_id)
            if guard is not None:
                return guard
            record = self._records.get(agent_id)
            if record is None:
                return self._blocked(agent_id, (BLOCKER_TEMPLATE_UNAVAILABLE,), "install_agent_before_memory_change")
            card = self._template_by_id(record.template_id)
            if card is None:
                return self._blocked(agent_id, (BLOCKER_TEMPLATE_UNAVAILABLE,), "refresh_agent_template_gallery")
            blockers = _memory_blockers(memory_scope, card.memory_scope, policy_receipt_ref=policy_receipt_ref)
            if blockers:
                return self._blocked(agent_id, blockers, _operator_action_for_blockers(blockers))
            updated = _replace_record(
                record,
                memory_scope=memory_scope,
                policy_receipt_refs=tuple(dict.fromkeys((*record.policy_receipt_refs, policy_receipt_ref))),
                updated_at_utc=_iso(self._now()),
            )
            self._records[agent_id] = updated
            self._persist_locked()
            return ManagedAgentDecision(
                status=ManagedAgentDecisionStatus.ACCEPTED,
                agent_id=agent_id,
                accepted=True,
                blockers=(),
                operator_action="review_memory_scope",
                record=updated,
                evidence_refs=("memory-scope-policy", policy_receipt_ref),
            )

    def _set_state(self, agent_id: str, state: ManagedAgentState, *, reason: str, actor: str) -> ManagedAgentDecision:
        with self._lock:
            guard = self._write_guard(agent_id)
            if guard is not None:
                return guard
            record = self._records.get(agent_id)
            if record is None:
                return self._blocked(agent_id, (BLOCKER_TEMPLATE_UNAVAILABLE,), "install_agent_before_lifecycle_change")
            if not reason.strip() or not actor.strip():
                return self._blocked(agent_id, ("lifecycle_reason_and_actor_required",), "provide_lifecycle_reason")
            updated = _replace_record(
                record,
                state=state,
                updated_at_utc=_iso(self._now()),
                intervention=_intervention_for_state(state, reason, record.cost_ceiling_ref),
            )
            self._records[agent_id] = updated
            self._persist_locked()
            return ManagedAgentDecision(
                status=ManagedAgentDecisionStatus.ACCEPTED,
                agent_id=agent_id,
                accepted=True,
                blockers=(),
                operator_action="resume_or_replace_agent"
                if state is ManagedAgentState.PAUSED
                else "create_replacement_agent",
                record=updated,
                evidence_refs=(f"lifecycle:{state.value}", f"actor:{actor}"),
            )

    def _write_guard(self, agent_id: str) -> ManagedAgentDecision | None:
        if self._damaged_reason:
            return ManagedAgentDecision(
                status=ManagedAgentDecisionStatus.RECOVERY_NEEDED,
                agent_id=agent_id,
                accepted=False,
                blockers=(BLOCKER_STATE_UNREADABLE,),
                operator_action="repair_managed_agent_state",
                evidence_refs=(self._damaged_reason,),
            )
        if self._config_error:
            return ManagedAgentDecision(
                status=ManagedAgentDecisionStatus.DEGRADED,
                agent_id=agent_id,
                accepted=False,
                blockers=(BLOCKER_DEPENDENCY_UNAVAILABLE,),
                operator_action="repair_managed_agent_config",
                evidence_refs=(self._config_error,),
            )
        return None

    @staticmethod
    def _blocked(
        agent_id: str,
        blockers: tuple[str, ...],
        operator_action: str,
        *,
        run_id: str = "",
    ) -> ManagedAgentDecision:
        return ManagedAgentDecision(
            status=ManagedAgentDecisionStatus.BLOCKED,
            agent_id=agent_id,
            accepted=False,
            blockers=tuple(dict.fromkeys(blockers)),
            operator_action=operator_action,
            run_id=run_id,
        )

    def _template_by_id(self, template_id: str) -> AgentTemplateCard | None:
        try:
            return {card.template_id: card for card in self._template_loader()}.get(template_id)
        except AgentTemplateCatalogError:
            logger.warning("Handled recoverable failure before fallback.", exc_info=True)
            return None

    def _dependencies_for(
        self, card: AgentTemplateCard, request: ManagedAgentInstallRequest
    ) -> ManagedAgentDependencyRefs:
        deps = self._config["dependency_defaults"]
        return ManagedAgentDependencyRefs(
            template_id=card.template_id,
            mailbox_channel=str(deps["mailbox_channel"]),
            sandbox_profile=card.sandbox_profile,
            route_ledger_ref=f"{deps['route_ledger_ref']}:{request.agent_id}",
            watcher_policy_ref=f"{deps['watcher_policy_ref']}:{request.agent_id}",
            automation_recipe_refs=tuple(str(item) for item in deps["automation_recipe_refs"]),
            conversation_ref=request.conversation_branch_ref
            or f"{deps['conversation_ref']}:{request.project_id}:{request.agent_id}",
            promotion_targets=tuple(str(item) for item in deps["promotion_targets"]),
            memory_policy_ref=f"{deps['memory_policy_ref']}:{','.join(request.memory_scope)}",
            monitoring_signal_ref=f"{deps['monitoring_signal_ref']}:{request.agent_id}",
            resource_lease_ref=request.cost_ceiling_ref,
            trace_eval_ref=f"{deps['trace_eval_ref']}:{request.agent_id}",
        )

    @staticmethod
    def _sandbox_for(record: ManagedAgentRecord, card: AgentTemplateCard) -> SandboxProfile:
        deps = record.dependencies
        return SandboxProfile(
            sandbox_id=f"sandbox:{record.agent_id}",
            workspace_ref=f"workspace:{record.project_id}",
            allowed_workspace_prefix=f"/workspaces/{record.project_id}",
            tool_permissions=card.allowed_tools,
            file_exposure=("project-files",),
            network_exposure=NetworkExposure.ALLOWLISTED if card.trust_badges.networked else NetworkExposure.DENIED,
            process_exposure=ProcessExposure.LIMITED,
            model_policy_ref=str(card.model_policy.get("default_tier", "model-policy:managed-agent")),
            memory_profile_ref=deps.memory_policy_ref,
            input_schema_ref="schema:workbench-managed-agent-run-input",
            output_schema_ref="schema:workbench-managed-agent-run-output",
            receipt_requirements=("receipt:template-policy", "receipt:mailbox-linked"),
            cancellation_behavior="pause_agent_then_cancel_tools",
            replay_boundary_ref=deps.trace_eval_ref,
            authority_ref=deps.watcher_policy_ref,
            provenance_ref=deps.route_ledger_ref,
        )

    def _load_config(self) -> dict[str, Any]:
        try:
            doc = yaml.safe_load(self._config_path.read_text(encoding="utf-8"))
        except (OSError, yaml.YAMLError) as exc:
            logger.warning("Handled recoverable failure before fallback.", exc_info=True)
            self._config_error = f"managed-agent-config-unreadable:{type(exc).__name__}"
            return _fallback_config()
        if not isinstance(doc, dict) or doc.get("schema_version") != _CONFIG_SCHEMA_VERSION:
            self._config_error = "managed-agent-config-schema-mismatch"
            return _fallback_config()
        deps = doc.get("dependency_defaults")
        if not isinstance(deps, dict):
            self._config_error = "managed-agent-config-missing-dependencies"
            return _fallback_config()
        required = set(_fallback_config()["dependency_defaults"])
        missing = tuple(sorted(required - set(deps)))
        if missing:
            self._config_error = f"managed-agent-config-missing:{','.join(missing)}"
            return _fallback_config()
        return doc

    @staticmethod
    def _dependency_contracts() -> list[dict[str, str]]:
        return [
            {"surface": "agent_templates", "ref": "config/workbench_agent_templates.yaml", "status": "linked"},
            {"surface": "mailbox_blackboard", "ref": "vetinari.memory.agent_mailbox.AgentMailbox", "status": "linked"},
            {"surface": "run_harness", "ref": "vetinari.workbench.agents.harness.admit_agent_run", "status": "linked"},
            {"surface": "route_ledger", "ref": "vetinari.workbench.agents.routing", "status": "linked"},
            {"surface": "watcher_runtime", "ref": "vetinari.workbench.agents.watchers", "status": "linked"},
            {"surface": "automation", "ref": "vetinari.workbench.automation", "status": "linked"},
            {"surface": "conversation", "ref": "vetinari.workbench.conversation", "status": "linked"},
            {"surface": "promotion", "ref": "vetinari.workbench.promotions", "status": "linked"},
            {"surface": "memory_scopes", "ref": "vetinari.workbench.memory_scopes", "status": "linked"},
            {"surface": "monitoring", "ref": "vetinari.workbench.monitoring", "status": "linked"},
            {"surface": "resources", "ref": "vetinari.workbench.resources", "status": "linked"},
            {"surface": "trace_to_eval", "ref": "vetinari.workbench.trace_eval_core", "status": "linked"},
        ]

    def _dependency_blockers(self) -> tuple[str, ...]:
        return (BLOCKER_DEPENDENCY_UNAVAILABLE,) if self._config_error else ()

    def _cost_ceiling_blocker(self, record: ManagedAgentRecord) -> str:
        ceilings = self._config.get("cost_ceilings", {})
        if not isinstance(ceilings, dict):
            return BLOCKER_DEPENDENCY_UNAVAILABLE
        policy = ceilings.get(record.cost_ceiling_ref)
        if policy is None:
            return BLOCKER_DEPENDENCY_UNAVAILABLE
        try:
            max_cost = float(policy["max_cost_usd"])
            per_run = float(policy.get("estimated_cost_per_run_usd", 0.0))
        except (KeyError, TypeError, ValueError):
            logger.warning("Managed agent cost ceiling policy is invalid.", exc_info=True)
            return BLOCKER_DEPENDENCY_UNAVAILABLE
        projected = (len(record.run_ids) + 1) * per_run
        return BLOCKER_COST_CEILING_EXCEEDED if projected > max_cost else ""

    def _restore(self) -> None:
        if not self._state_path.exists():
            return
        try:
            payload = json.loads(self._state_path.read_text(encoding="utf-8"))
            if payload.get("schema_version") != SCHEMA_VERSION:
                raise ValueError("schema_version mismatch")
            records = {
                str(item["agent_id"]): ManagedAgentRecord.from_mapping(dict(item)) for item in payload.get("agents", [])
            }
        except Exception as exc:
            logger.warning("Handled recoverable failure before fallback.", exc_info=True)
            self._damaged_reason = f"managed-agent-state-unreadable:{type(exc).__name__}"
            return
        self._records = records

    def _persist_locked(self) -> None:
        payload = {
            "schema_version": SCHEMA_VERSION,
            "agents": [record.to_dict() for record in sorted(self._records.values(), key=lambda item: item.agent_id)],
        }
        self._state_path.parent.mkdir(parents=True, exist_ok=True)
        tmp_path = self._state_path.with_name(f".{self._state_path.name}.{uuid.uuid4().hex}.tmp")
        tmp_path.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")
        tmp_path.replace(self._state_path)
        # spine_consumers invokes get_spine() and absorbs observability failures.
        record_run_completed(
            run_id="managed-agents-state",
            kind="agent_run",
            project_id=str(payload.get("project_id", "default")),
        )


__all__ = [
    "DEFAULT_MANAGED_AGENT_CONFIG_PATH",
    "DEFAULT_MANAGED_AGENT_STATE_PATH",
    "ManagedAgentWorkspaceRuntime",
]
