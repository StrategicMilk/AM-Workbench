"""Command-safety decision service composed from Workbench trust authorities."""

from __future__ import annotations

import hashlib
import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Any
from uuid import uuid4

from vetinari.agents.contracts import OutcomeSignal, Provenance, ToolEvidence
from vetinari.receipts.record import WorkReceipt, WorkReceiptKind
from vetinari.receipts.store import WorkReceiptStore
from vetinari.types import AgentType, EvidenceBasis
from vetinari.utils.bounded_collections import BoundedDict, BoundedList
from vetinari.workbench.approval_chain import ApprovalChainOutcome, ApprovalChainRequest, evaluate_approval_chain
from vetinari.workbench.command_safety.classifier import classify_command, command_fingerprint, normalize_command
from vetinari.workbench.command_safety.contracts import (
    SCHEMA_VERSION,
    CommandClassification,
    CommandSafetyContext,
    CommandSafetyDecision,
    CommandSafetyError,
    CommandSafetyReason,
    CommandSafetyVerdict,
)
from vetinari.workbench.command_safety.profiles import load_command_safety_profiles
from vetinari.workbench.command_safety.state import CommandSafetyStateStore
from vetinari.workbench.policy.verdicts import EvidenceLink, RiskDomain
from vetinari.workbench.tool_trust.contracts import ToolTrustReason
from vetinari.workbench.tool_trust.runtime import assess_tool_surface_pin

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class CommandSafetyDependencies:
    """Runtime contract for CommandSafetyDependencies."""

    profile_path: str | None = None
    state_store: CommandSafetyStateStore | None = None
    receipt_store: WorkReceiptStore | None = None
    issued_decision_limit: int = 1_000

    def __repr__(self) -> str:
        return (
            "CommandSafetyDependencies("
            f"profile_path={self.profile_path!r}, "
            f"state_store={type(self.state_store).__name__ if self.state_store else None!r}, "
            f"receipt_store={type(self.receipt_store).__name__ if self.receipt_store else None!r}, "
            f"issued_decision_limit={self.issued_decision_limit!r})"
        )


class CommandSafetyService:
    """Runtime contract for CommandSafetyService."""

    def __init__(self, dependencies: CommandSafetyDependencies | None = None) -> None:
        deps = dependencies or CommandSafetyDependencies()
        self._profile_path = deps.profile_path
        self._state_store = deps.state_store or CommandSafetyStateStore()
        self._receipt_store = deps.receipt_store or WorkReceiptStore()
        self._issued: BoundedDict[str, CommandSafetyDecision] = BoundedDict(max(1, deps.issued_decision_limit))

    def list_profiles(self) -> list[dict[str, Any]]:
        """Execute the list profiles operation.

        Returns:
            Collection of profiles values.
        """
        profiles = load_command_safety_profiles(self._profile_path)
        return [p.to_dict() for p in sorted(profiles.values(), key=lambda row: row.profile_id)]

    def inspect_state(self, *, project_id: str, run_id: str, session_id: str, surface_id: str) -> Any:
        """Inspect command-safety state via the injected state store.

        Exposed so route handlers can delegate to the per-process injected
        store instead of constructing a fresh ``CommandSafetyStateStore()``
        and accidentally hitting the user's real spine.

        Args:
            project_id: Project scope key.
            run_id: Run scope key.
            session_id: Session scope key.
            surface_id: Surface scope key.

        Returns:
            CwdStatus from the underlying state store.
        """
        return self._state_store.inspect(
            project_id=project_id,
            run_id=run_id,
            session_id=session_id,
            surface_id=surface_id,
        )

    def classify(self, context: CommandSafetyContext) -> CommandClassification:
        """Execute the classify operation.

        Returns:
            CommandClassification value produced by classify().
        """
        profiles = load_command_safety_profiles(self._profile_path)
        profile = profiles.get(context.profile_id)
        if profile is None:
            return CommandClassification(
                normalize_command(context.command),
                (),
                "",
                CommandSafetyVerdict.DEGRADED_MISSING_POLICY,
                (CommandSafetyReason.UNKNOWN_PROFILE,),
                (),
                False,
                True,
            )
        return classify_command(context.command, profile=profile, surface=context.surface)

    def decide(self, context: CommandSafetyContext) -> CommandSafetyDecision:
        """Execute the decide operation.

        Returns:
            CommandSafetyDecision value produced by decide().
        """
        idempotency_key = context.idempotency_key or _idempotency_key(context)
        if idempotency_key in self._issued:
            previous = self._issued[idempotency_key]
            if previous.command_fingerprint != command_fingerprint(context.command):
                return self._block_without_receipt(
                    context,
                    classifier=previous.classifier,
                    reasons=(CommandSafetyReason.DUPLICATE_IDEMPOTENCY_KEY,),
                    cwd_state={"status": "blocked", "reasons": [CommandSafetyReason.DUPLICATE_IDEMPOTENCY_KEY.value]},
                )
            return previous
        try:
            profiles = load_command_safety_profiles(self._profile_path)
            profile = profiles[context.profile_id]
            classifier = classify_command(context.command, profile=profile, surface=context.surface)
        except (CommandSafetyError, KeyError):
            profile = None
            classifier = CommandClassification(
                normalize_command(context.command),
                (),
                "",
                CommandSafetyVerdict.DEGRADED_MISSING_POLICY,
                (CommandSafetyReason.MISSING_POLICY,),
                (),
                False,
                True,
            )
        cwd_status = self._state_store.inspect(
            project_id=context.project_id,
            run_id=context.run_id,
            session_id=context.session_id,
            surface_id=context.surface_id,
        )
        reasons = BoundedList[CommandSafetyReason](32, classifier.reasons)
        if not cwd_status.allows_execution:
            reasons.extend(cwd_status.reasons)
        cwd_allowed = profile is not None and _cwd_in_allowed_roots(context.cwd, profile.allowed_cwd_roots)
        if not cwd_allowed:
            reasons.append(CommandSafetyReason.CWD_OUTSIDE_ALLOWED_ROOT)
        tool_payload, tool_allowed, tool_reasons = self._assess_tool_surface(context)
        reasons.extend(tool_reasons)
        hard_block = (
            classifier.hard_block
            or classifier.verdict is CommandSafetyVerdict.DEGRADED_MISSING_POLICY
            or not cwd_status.allows_execution
            or not cwd_allowed
        )
        if tool_allowed is False and CommandSafetyReason.TOOL_PIN_DRIFT not in tool_reasons:
            hard_block = True
        approval_payload: dict[str, Any] | None = None
        if hard_block:
            verdict = CommandSafetyVerdict.BLOCK
            allowed = False
            human = False
        else:
            approval_payload, approval_reasons = self._evaluate_approval(context, classifier, tool_allowed)
            reasons.extend(approval_reasons)
            outcome = str(approval_payload.get("outcome", "deny"))
            if outcome == ApprovalChainOutcome.ALLOW.value:
                verdict = CommandSafetyVerdict.ALLOW
                allowed = True
                human = False
            elif outcome == ApprovalChainOutcome.REQUIRE_HUMAN_APPROVAL.value:
                verdict = CommandSafetyVerdict.REQUIRE_HUMAN_APPROVAL
                allowed = False
                human = True
            else:
                verdict = CommandSafetyVerdict.BLOCK
                allowed = False
                human = False
        cwd_after = cwd_status
        if cwd_status.allows_execution and cwd_allowed:
            try:
                cwd_after = self._state_store.record_cwd(
                    project_id=context.project_id,
                    run_id=context.run_id,
                    session_id=context.session_id,
                    surface_id=context.surface_id,
                    cwd=context.cwd or cwd_status.cwd,
                    command=context.command,
                    verdict=verdict.value,
                )
            except CommandSafetyError:
                reasons.append(CommandSafetyReason.CORRUPT_CWD_HISTORY)
                verdict = CommandSafetyVerdict.BLOCK
                allowed = False
                human = False
        decision = self._build_decision(
            context=context,
            classifier=classifier,
            verdict=verdict,
            allowed=allowed,
            human_approval_required=human,
            reasons=tuple(dict.fromkeys(reasons)),
            approval_payload=approval_payload,
            tool_payload=tool_payload,
            cwd_state=cwd_after.to_dict(),
            receipt_ref="",
        )
        decision = self._record_receipt_or_block(decision, idempotency_key)
        self._issued[idempotency_key] = decision
        return decision

    @staticmethod
    def _assess_tool_surface(
        context: CommandSafetyContext,
    ) -> tuple[dict[str, Any] | None, bool | None, list[CommandSafetyReason]]:
        if context.observed_surface is None:
            return None, False, [CommandSafetyReason.UNKNOWN_TOOL_SURFACE]
        try:
            trust = assess_tool_surface_pin(
                context.pinned_surfaces,
                context.observed_surface,
                capability_diff=context.capability_diff,
                approval=context.tool_surface_approval,
            )
        except Exception:
            logger.warning("Handled recoverable failure before fallback.", exc_info=True)
            return None, False, [CommandSafetyReason.CORRUPT_TOOL_PIN]
        payload = trust.to_dict()
        if trust.allowed:
            return payload, True, [CommandSafetyReason.TOOL_PIN_ALLOWED]
        mapped = BoundedList[CommandSafetyReason](16)
        for reason in trust.reasons:
            if reason is ToolTrustReason.UNKNOWN_TOOL_SURFACE:
                mapped.append(CommandSafetyReason.UNKNOWN_TOOL_SURFACE)
            elif reason is ToolTrustReason.STALE_PIN:
                mapped.append(CommandSafetyReason.STALE_TOOL_PIN)
            elif reason is ToolTrustReason.CORRUPT_TOOL_SURFACE:
                mapped.append(CommandSafetyReason.CORRUPT_TOOL_PIN)
            elif reason is ToolTrustReason.APPROVAL_REQUIRED:
                mapped.extend([CommandSafetyReason.TOOL_PIN_DRIFT, CommandSafetyReason.APPROVAL_REQUIRED])
            else:
                mapped.append(CommandSafetyReason.TOOL_PIN_DRIFT)
        return payload, False, mapped or [CommandSafetyReason.TOOL_PIN_DRIFT]

    @staticmethod
    def _evaluate_approval(
        context: CommandSafetyContext, classifier: CommandClassification, tool_allowed: bool | None
    ) -> tuple[dict[str, Any], list[CommandSafetyReason]]:
        try:
            request = ApprovalChainRequest(
                project_id=context.project_id,
                session_id=context.session_id,
                channel="desktop",
                action_id=f"command-safety:{command_fingerprint(context.command)[:16]}",
                action_type="command_safety_decision",
                actor_id=context.actor_id,
                run_id=context.run_id,
                risk_domain=RiskDomain.SHELL.value,
                summary=classifier.normalized_command,
                action_fingerprint=command_fingerprint(context.command),
                evidence_links=(
                    EvidenceLink(
                        "command-safety-classifier",
                        "external",
                        "vetinari.workbench.command_safety",
                        "command-safety classifier result",
                    ),
                ),
                authority_refs=("workbench-command-safety-profile",),
                approval_sources=context.approval_sources,
                readiness_signals=context.readiness_signals,
                governance_available=context.governance_available,
                governance_mode=context.governance_mode,
                hard_deny=classifier.hard_block,
                destructive=CommandSafetyReason.DESTRUCTIVE_PATTERN in classifier.reasons,
                dlp_risk=CommandSafetyReason.SECRET_EXPOSURE in classifier.reasons,
                requires_tool_pin=True,
                tool_pin_verified=bool(tool_allowed),
                metadata={
                    "surface_id": context.surface_id,
                    "profile_id": context.profile_id,
                    "classifier_reasons": [r.value for r in classifier.reasons],
                },
            )
            decision = evaluate_approval_chain(request)
            payload = decision.to_dict()
            reasons = BoundedList[CommandSafetyReason](8)
            if decision.outcome is ApprovalChainOutcome.REQUIRE_HUMAN_APPROVAL:
                reasons.append(CommandSafetyReason.APPROVAL_REQUIRED)
            elif decision.outcome is not ApprovalChainOutcome.ALLOW:
                reasons.append(CommandSafetyReason.APPROVAL_CHAIN_DENIED)
            return payload, reasons
        except Exception:
            logger.warning("Handled recoverable failure before fallback.", exc_info=True)
            return {
                "decision_id": "approval-chain-unavailable",
                "outcome": "deny",
                "allowed": False,
                "human_approval_required": False,
                "matched_step": "approval_chain_unavailable",
            }, [CommandSafetyReason.APPROVAL_CHAIN_UNAVAILABLE]

    def _build_decision(
        self,
        *,
        context: CommandSafetyContext,
        classifier: CommandClassification,
        verdict: CommandSafetyVerdict,
        allowed: bool,
        human_approval_required: bool,
        reasons: tuple[CommandSafetyReason, ...],
        approval_payload: dict[str, Any] | None,
        tool_payload: dict[str, Any] | None,
        cwd_state: dict[str, Any],
        receipt_ref: str,
    ) -> CommandSafetyDecision:
        now = _utc_now()
        receipt_payload = {
            "project_id": context.project_id,
            "run_id": context.run_id,
            "session_id": context.session_id,
            "surface_id": context.surface_id,
            "profile_id": context.profile_id,
            "command_fingerprint": command_fingerprint(context.command),
            "classifier_reasons": [r.value for r in classifier.reasons],
            "approval_chain_decision_id": approval_payload.get("decision_id") if approval_payload else "",
            "tool_surface_ref": tool_payload.get("surface_id") if tool_payload else context.surface_id,
            "cwd_before": cwd_state.get("cwd", ""),
            "cwd_after": context.cwd,
            "history_entry_id": (cwd_state.get("history") or [{}])[-1].get("entry_id", "")
            if cwd_state.get("history")
            else "",
            "state_revision": cwd_state.get("revision", 0),
            "final_verdict": verdict.value,
            "decided_at_utc": now,
        }
        return CommandSafetyDecision(
            f"command-safety-{uuid4().hex}",
            SCHEMA_VERSION,
            context.project_id,
            context.run_id,
            context.session_id,
            context.surface_id,
            context.surface.value,
            context.profile_id,
            command_fingerprint(context.command),
            classifier.normalized_command,
            verdict,
            allowed,
            human_approval_required,
            reasons,
            classifier,
            approval_payload,
            tool_payload,
            cwd_state,
            receipt_ref,
            receipt_payload,
            now,
        )

    def _record_receipt_or_block(self, decision: CommandSafetyDecision, idempotency_key: str) -> CommandSafetyDecision:
        # WorkReceiptStore owns its maxsize/retention behavior; this service only emits one receipt per decision.
        try:
            receipt = _receipt_from_decision(decision, idempotency_key)
            self._receipt_store.append(receipt)
            payload = {**decision.receipt_payload, "receipt_id": receipt.receipt_id, "idempotency_key": idempotency_key}
            return _replace_decision(
                decision,
                decision.verdict,
                decision.allowed,
                decision.human_approval_required,
                tuple(dict.fromkeys((*decision.reasons, CommandSafetyReason.RECEIPT_EMITTED))),
                receipt.receipt_id,
                payload,
            )
        except Exception:
            logger.warning("Handled recoverable failure before fallback.", exc_info=True)
            payload = {
                **decision.receipt_payload,
                "receipt_error": "receipt_sink_unavailable",
                "idempotency_key": idempotency_key,
            }
            return _replace_decision(
                decision,
                CommandSafetyVerdict.BLOCK,
                False,
                False,
                tuple(dict.fromkeys((*decision.reasons, CommandSafetyReason.RECEIPT_UNAVAILABLE))),
                "",
                payload,
            )

    def _block_without_receipt(
        self,
        context: CommandSafetyContext,
        *,
        classifier: CommandClassification,
        reasons: tuple[CommandSafetyReason, ...],
        cwd_state: dict[str, Any],
    ) -> CommandSafetyDecision:
        return self._build_decision(
            context=context,
            classifier=classifier,
            verdict=CommandSafetyVerdict.BLOCK,
            allowed=False,
            human_approval_required=False,
            reasons=reasons,
            approval_payload=None,
            tool_payload=None,
            cwd_state=cwd_state,
            receipt_ref="",
        )


def _receipt_from_decision(decision: CommandSafetyDecision, idempotency_key: str) -> WorkReceipt:
    passed = decision.allowed
    now = _utc_now()
    outcome = OutcomeSignal(
        passed=passed,
        score=1.0 if passed else 0.0,
        basis=EvidenceBasis.TOOL_EVIDENCE,
        tool_evidence=(
            ToolEvidence(
                tool_name="CommandSafetyService",
                command=decision.normalized_command,
                exit_code=0 if passed else 1,
                stdout_snippet=decision.verdict.value,
                passed=passed,
            ),
        ),
        provenance=Provenance(source="workbench_command_safety", timestamp_utc=now, tool_name="CommandSafetyService"),
        issues=() if passed else tuple(r.value for r in decision.reasons),
    )
    return WorkReceipt(
        project_id=decision.project_id,
        agent_id="workbench-command-safety",
        agent_type=AgentType.WORKBENCH,
        kind=WorkReceiptKind.POLICY_DECISION,
        outcome=outcome,
        started_at_utc=decision.decided_at_utc,
        finished_at_utc=now,
        inputs_summary=_truncate(f"{decision.profile_id}|{decision.surface_id}|{decision.command_fingerprint[:16]}"),
        outputs_summary=_truncate(f"{decision.verdict.value}|{','.join(r.value for r in decision.reasons[:3])}"),
        linked_claim_ids=tuple(
            str(v)
            for v in (
                decision.run_id,
                decision.session_id,
                decision.surface_id,
                decision.approval_chain.get("decision_id") if decision.approval_chain else "",
                idempotency_key,
            )
            if v
        ),
    )


def _replace_decision(
    decision: CommandSafetyDecision,
    verdict: CommandSafetyVerdict,
    allowed: bool,
    human: bool,
    reasons: tuple[CommandSafetyReason, ...],
    receipt_ref: str,
    receipt_payload: dict[str, Any],
) -> CommandSafetyDecision:
    return CommandSafetyDecision(
        decision.decision_id,
        decision.schema_version,
        decision.project_id,
        decision.run_id,
        decision.session_id,
        decision.surface_id,
        decision.surface,
        decision.profile_id,
        decision.command_fingerprint,
        decision.normalized_command,
        verdict,
        allowed,
        human,
        reasons,
        decision.classifier,
        decision.approval_chain,
        decision.tool_surface,
        decision.cwd_state,
        receipt_ref,
        receipt_payload,
        decision.decided_at_utc,
    )


def _idempotency_key(context: CommandSafetyContext) -> str:
    return hashlib.sha256(
        "|".join((
            context.project_id,
            context.run_id,
            context.session_id,
            context.surface_id,
            command_fingerprint(context.command),
        )).encode("utf-8")
    ).hexdigest()


def _cwd_in_allowed_roots(cwd: str, roots: tuple[str, ...]) -> bool:
    if not roots:
        return False
    normalized_cwd = _normalize_cwd(cwd)
    return any(
        normalized_cwd == root or normalized_cwd.startswith(f"{root}/")
        for root in (_normalize_cwd(row) for row in roots if str(row).strip())
    )


def _normalize_cwd(value: str) -> str:
    text = str(value).strip()
    # Treat "." as the runtime project root (the current working
    # directory) so command_safety.yaml can ship portable
    # ``allowed_cwd_roots: ["."]`` instead of hardcoded author paths
    # tied to one maintainer checkout. Resolving here also normalizes ``./``
    # and ``./sub`` to absolute paths so the prefix check below stays
    # case-folded and Posix-styled.
    if text == "." or text.startswith(("./", ".\\")):
        try:
            text = (Path.cwd() / text).resolve().as_posix()
        except (OSError, RuntimeError, ValueError):
            text = text.replace("\\", "/")
    else:
        text = text.replace("\\", "/")
        if reparse := _try_resolve_existing_path(text):
            text = reparse
    return PurePosixPath(text).as_posix().rstrip("/").lower()


def _try_resolve_existing_path(value: str) -> str:
    try:
        path = Path(value)
        if path.exists():
            return path.resolve().as_posix()
    except (OSError, RuntimeError, ValueError):
        logger.warning("Handled recoverable failure before fallback.", exc_info=True)
        return ""
    return ""


def _truncate(value: str, limit: int = 200) -> str:
    return value if len(value) <= limit else value[: limit - 3] + "..."


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()
