"""Role-scoped specialist model fleet for Workbench agents."""

from __future__ import annotations

from dataclasses import dataclass, replace
from enum import Enum
from pathlib import Path

import yaml

from vetinari.constants import PROJECT_ROOT
from vetinari.workbench.specialists.cards import SpecialistModelCard, SpecialistTask
from vetinari.workbench.specialists.registry import SpecialistModelRegistry, load_default_specialist_registry

DEFAULT_FLEET_PATH = PROJECT_ROOT / "config" / "workbench" / "specialist_fleet.yaml"

BLOCKER_ROLE_NOT_ALLOWED = "role_not_allowed"
BLOCKER_TASK_NOT_ROUTE_ELIGIBLE = "task_not_route_eligible"
BLOCKER_EVAL_NOT_PASSED = "eval_not_passed"
BLOCKER_APPROVAL_MISSING = "approval_missing"
BLOCKER_ROLLBACK_MISSING = "rollback_missing"


class SpecialistFleetError(ValueError):
    """Raised when a specialist fleet cannot safely route or mutate."""


class AgentRole(str, Enum):
    """Agent roles with specialist variants."""

    FOREMAN = "foreman"
    WORKER = "worker"
    INSPECTOR = "inspector"
    RESEARCH = "research"
    DATA = "data"
    SECURITY = "security"
    RUNTIME = "runtime"


@dataclass(frozen=True, slots=True)
class SpecialistFleetMember:
    """Role-specific specialist variant and governance policy."""

    role: AgentRole
    active_card_id: str
    fallback_chain: tuple[str, ...]
    fleet_eval_suite_ref: str
    promotion_policy_ref: str
    retirement_policy_ref: str
    negative_knowledge_refs: tuple[str, ...]
    route_eligible_tasks: tuple[SpecialistTask, ...]
    approval_ref: str
    rollback_ref: str

    def __post_init__(self) -> None:
        if not isinstance(self.role, AgentRole):
            raise SpecialistFleetError("role must be an AgentRole")
        for field_name in (
            "active_card_id",
            "fleet_eval_suite_ref",
            "promotion_policy_ref",
            "retirement_policy_ref",
            "approval_ref",
            "rollback_ref",
        ):
            _require_text(getattr(self, field_name), field_name)
        _require_string_tuple(self.fallback_chain, "fallback_chain", allow_empty=True)
        _require_string_tuple(self.negative_knowledge_refs, "negative_knowledge_refs")
        if not self.route_eligible_tasks:
            raise SpecialistFleetError("route_eligible_tasks must be non-empty")
        if any(not isinstance(task, SpecialistTask) for task in self.route_eligible_tasks):
            raise SpecialistFleetError("route_eligible_tasks must contain SpecialistTask values")

    def __repr__(self) -> str:
        """Return a compact diagnostic representation."""
        return f"SpecialistFleetMember(role={self.role!r}, active_card_id={self.active_card_id!r}, fallback_chain={self.fallback_chain!r})"


@dataclass(frozen=True, slots=True)
class FleetRouteDecision:
    """Fail-closed role and task routing decision."""

    role: AgentRole
    task: SpecialistTask
    card_id: str
    approved: bool
    blockers: tuple[str, ...]
    fallback_chain: tuple[str, ...]
    negative_knowledge_refs: tuple[str, ...]

    def __post_init__(self) -> None:
        _require_text(self.card_id, "card_id")
        _require_string_tuple(self.blockers, "blockers", allow_empty=True)
        _require_string_tuple(self.fallback_chain, "fallback_chain", allow_empty=True)
        _require_string_tuple(self.negative_knowledge_refs, "negative_knowledge_refs")
        if self.approved and self.blockers:
            raise SpecialistFleetError("approved fleet route cannot include blockers")
        if not self.approved and not self.blockers:
            raise SpecialistFleetError("blocked fleet route requires blockers")

    def __repr__(self) -> str:
        """Return a compact diagnostic representation."""
        return f"FleetRouteDecision(role={self.role!r}, task={self.task!r}, card_id={self.card_id!r})"


@dataclass(frozen=True, slots=True)
class SpecialistFleet:
    """Immutable role-specific fleet over a specialist card registry."""

    registry: SpecialistModelRegistry
    members: tuple[SpecialistFleetMember, ...]

    def __post_init__(self) -> None:
        if not isinstance(self.registry, SpecialistModelRegistry):
            raise SpecialistFleetError("registry must be a SpecialistModelRegistry")
        if len({member.role for member in self.members}) != len(self.members):
            raise SpecialistFleetError("fleet roles must be unique")
        roles = {member.role for member in self.members}
        if roles != set(AgentRole):
            missing = sorted(role.value for role in set(AgentRole) - roles)
            raise SpecialistFleetError(f"fleet role coverage mismatch missing={missing}")
        card_ids = {card.card_id for card in self.registry.cards}
        for member in self.members:
            if member.active_card_id not in card_ids:
                raise SpecialistFleetError(f"active card {member.active_card_id!r} is not registered")
            if any(card_id not in card_ids for card_id in member.fallback_chain):
                raise SpecialistFleetError("fallback_chain references unregistered cards")

    def member_for_role(self, role: AgentRole | str) -> SpecialistFleetMember:
        """Return the fleet member for a role.

        Returns:
            SpecialistFleetMember value produced by member_for_role().

        Raises:
            Exception: Propagates validation or runtime failures from the underlying operation.
        """
        selected = AgentRole(role)
        for member in self.members:
            if member.role == selected:
                return member
        raise SpecialistFleetError(f"no fleet member for role {selected.value!r}")

    def route(self, *, role: AgentRole | str, task: SpecialistTask | str) -> FleetRouteDecision:
        """Approve a specialist card only for declared role and task scope.

        Returns:
            FleetRouteDecision value produced by route().
        """
        selected_task = SpecialistTask(task)
        member = self.member_for_role(role)
        blockers: list[str] = []
        if selected_task not in member.route_eligible_tasks:
            blockers.append(BLOCKER_TASK_NOT_ROUTE_ELIGIBLE)
        active_card = self._card_by_id(member.active_card_id)
        if active_card.task != selected_task:
            blockers.append(BLOCKER_ROLE_NOT_ALLOWED)
        return FleetRouteDecision(
            role=member.role,
            task=selected_task,
            card_id=member.active_card_id,
            approved=not blockers,
            blockers=tuple(blockers),
            fallback_chain=member.fallback_chain,
            negative_knowledge_refs=member.negative_knowledge_refs,
        )

    def promote_member(
        self,
        *,
        role: AgentRole | str,
        replacement_card: SpecialistModelCard,
        eval_passed: bool,
        approval_ref: str,
        rollback_ref: str,
    ) -> SpecialistFleet:
        """Replace a role member only after eval proof, approval, and rollback refs exist.

        Returns:
            SpecialistFleet value produced by promote_member().

        Raises:
            Exception: Propagates validation or runtime failures from the underlying operation.
        """
        if not isinstance(replacement_card, SpecialistModelCard):
            raise SpecialistFleetError("replacement_card must be a SpecialistModelCard")
        blockers: list[str] = []
        if not eval_passed:
            blockers.append(BLOCKER_EVAL_NOT_PASSED)
        if not approval_ref.strip():
            blockers.append(BLOCKER_APPROVAL_MISSING)
        if not rollback_ref.strip():
            blockers.append(BLOCKER_ROLLBACK_MISSING)
        if blockers:
            raise SpecialistFleetError(f"specialist fleet promotion blocked: {blockers}")
        member = self.member_for_role(role)
        if replacement_card.task not in member.route_eligible_tasks:
            raise SpecialistFleetError(BLOCKER_TASK_NOT_ROUTE_ELIGIBLE)
        next_registry = SpecialistModelRegistry((*self.registry.cards, replacement_card))
        next_member = replace(
            member,
            active_card_id=replacement_card.card_id,
            fallback_chain=(member.active_card_id, *member.fallback_chain),
            approval_ref=approval_ref,
            rollback_ref=rollback_ref,
        )
        return SpecialistFleet(
            registry=next_registry,
            members=tuple(next_member if item.role == member.role else item for item in self.members),
        )

    def _card_by_id(self, card_id: str) -> SpecialistModelCard:
        for card in self.registry.cards:
            if card.card_id == card_id:
                return card
        raise SpecialistFleetError(f"unknown specialist card {card_id!r}")


def load_specialist_fleet(
    path: Path | str = DEFAULT_FLEET_PATH,
    *,
    registry: SpecialistModelRegistry | None = None,
) -> SpecialistFleet:
    """Load the role fleet from YAML and validate it against the card registry.

    Returns:
        Resolved specialist fleet value.

    Raises:
        Exception: Propagates validation or runtime failures from the underlying operation.
    """
    fleet_path = Path(path)
    if not fleet_path.exists():
        raise SpecialistFleetError(f"specialist fleet config not found: {fleet_path}")
    raw = yaml.safe_load(fleet_path.read_text(encoding="utf-8"))
    if not isinstance(raw, dict) or not isinstance(raw.get("members"), list):
        raise SpecialistFleetError("specialist fleet config must contain a members list")
    selected_registry = registry or load_default_specialist_registry()
    return SpecialistFleet(
        registry=selected_registry,
        members=tuple(_member_from_mapping(item) for item in raw["members"]),
    )


def _member_from_mapping(raw: object) -> SpecialistFleetMember:
    if not isinstance(raw, dict):
        raise SpecialistFleetError("each fleet member must be a mapping")
    return SpecialistFleetMember(
        role=AgentRole(str(raw.get("role", ""))),
        active_card_id=str(raw.get("active_card_id", "")),
        fallback_chain=tuple(str(item) for item in raw.get("fallback_chain", ())),
        fleet_eval_suite_ref=str(raw.get("fleet_eval_suite_ref", "")),
        promotion_policy_ref=str(raw.get("promotion_policy_ref", "")),
        retirement_policy_ref=str(raw.get("retirement_policy_ref", "")),
        negative_knowledge_refs=tuple(str(item) for item in raw.get("negative_knowledge_refs", ())),
        route_eligible_tasks=tuple(SpecialistTask(str(item)) for item in raw.get("route_eligible_tasks", ())),
        approval_ref=str(raw.get("approval_ref", "")),
        rollback_ref=str(raw.get("rollback_ref", "")),
    )


def _require_text(value: object, field_name: str) -> None:
    if not isinstance(value, str) or not value.strip():
        raise SpecialistFleetError(f"{field_name} must be non-empty")


def _require_string_tuple(values: tuple[str, ...], field_name: str, *, allow_empty: bool = False) -> None:
    if not isinstance(values, tuple) or (not allow_empty and not values):
        raise SpecialistFleetError(f"{field_name} must be a non-empty tuple")
    if any(not isinstance(value, str) or not value.strip() for value in values):
        raise SpecialistFleetError(f"{field_name} must contain non-empty strings")


__all__ = [
    "BLOCKER_APPROVAL_MISSING",
    "BLOCKER_EVAL_NOT_PASSED",
    "BLOCKER_ROLE_NOT_ALLOWED",
    "BLOCKER_ROLLBACK_MISSING",
    "BLOCKER_TASK_NOT_ROUTE_ELIGIBLE",
    "DEFAULT_FLEET_PATH",
    "AgentRole",
    "FleetRouteDecision",
    "SpecialistFleet",
    "SpecialistFleetError",
    "SpecialistFleetMember",
    "load_specialist_fleet",
]
