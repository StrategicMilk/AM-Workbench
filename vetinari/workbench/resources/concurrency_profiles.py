"""Active-user concurrency profiles for Workbench prosumer hardware."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Any

import yaml

from vetinari.constants import PROJECT_ROOT
from vetinari.workbench.resources.governor import ResourceBudget

DEFAULT_CONCURRENCY_PROFILES_PATH = PROJECT_ROOT / "config" / "workbench" / "concurrency_profiles.yaml"


class ConcurrencyProfileError(ValueError):
    """Raised when concurrency profile state cannot be trusted."""

    def __init__(self, reason: str, message: str | None = None) -> None:
        super().__init__(f"{reason}: {message}" if message else reason)
        self.reason = reason


class ConcurrencyLane(str, Enum):
    """Concurrency lanes separated by real resource pressure, not agent count."""

    LARGE_GPU_DECODE = "large_gpu_decode"
    MEDIUM_GPU_MODEL = "medium_gpu_model"
    GPU_TRAINING = "gpu_training"
    GPU_EMBEDDING_RERANK = "gpu_embedding_rerank"
    CPU_SPECIALIST = "cpu_specialist"
    AGENT_ORCHESTRATION = "agent_orchestration"
    RAG_INDEXING = "rag_indexing"
    EVAL_SWEEP = "eval_sweep"
    DOWNLOAD_MATERIALIZE = "download_materialize"


class ConcurrencyAction(str, Enum):
    """Admission action for one concurrency request."""

    ADMIT = "admit"
    QUEUE = "queue"
    DOWNGRADE = "downgrade"
    BATCH = "batch"
    APPROVAL_REQUIRED = "approval_required"
    DENY = "deny"


@dataclass(frozen=True, slots=True)
class ConcurrencyCap:
    """Hard cap and over-cap policy for one concurrency lane."""

    lane: ConcurrencyLane
    max_active: int
    label: str
    over_cap_action: ConcurrencyAction
    burst_active: int = 0
    batch_after: int | None = None
    requires_approval: bool = False

    def __post_init__(self) -> None:
        if self.max_active < 0:
            raise ConcurrencyProfileError("cap-negative", f"{self.lane.value} max_active must be non-negative")
        if self.burst_active < 0:
            raise ConcurrencyProfileError("burst-negative", f"{self.lane.value} burst_active must be non-negative")
        if self.batch_after is not None and self.batch_after < 0:
            raise ConcurrencyProfileError("batch-after-negative", f"{self.lane.value} batch_after must be non-negative")
        _require_text(self.label, f"{self.lane.value}.label")

    def __repr__(self) -> str:
        """Return a compact diagnostic representation."""
        return f"ConcurrencyCap(lane={self.lane!r}, max_active={self.max_active!r}, label={self.label!r})"


@dataclass(frozen=True, slots=True)
class ActiveUserConcurrencyProfile:
    """One named active-user concurrency policy."""

    profile_id: str
    description: str
    caps: tuple[ConcurrencyCap, ...]
    max_vram_gb: float
    max_ram_gb: float
    max_cpu_threads: int
    max_storage_gb: float
    max_queue_depth: int
    max_context_tokens: int
    max_kv_cache_gb: float
    interactive_vram_reserve_gb: float = 2.0
    interactive_ram_reserve_gb: float = 4.0

    def __post_init__(self) -> None:
        _require_text(self.profile_id, "profile_id")
        _require_text(self.description, "description")
        lanes = [cap.lane for cap in self.caps]
        if set(lanes) != set(ConcurrencyLane):
            missing = sorted(lane.value for lane in set(ConcurrencyLane) - set(lanes))
            extra_count = len(lanes) - len(set(lanes))
            raise ConcurrencyProfileError(
                "lane-coverage-invalid",
                f"profile {self.profile_id!r} missing={missing} duplicate_count={extra_count}",
            )
        for field_name in (
            "max_vram_gb",
            "max_ram_gb",
            "max_storage_gb",
            "max_kv_cache_gb",
            "interactive_vram_reserve_gb",
            "interactive_ram_reserve_gb",
        ):
            _require_non_negative_number(getattr(self, field_name), field_name)
        for field_name in ("max_cpu_threads", "max_queue_depth", "max_context_tokens"):
            _require_non_negative_int(getattr(self, field_name), field_name)

    def cap_for(self, lane: ConcurrencyLane | str) -> ConcurrencyCap:
        """Return the cap for a lane, failing closed on unknown lanes.

        Returns:
            ConcurrencyCap value produced by cap_for().

        Raises:
            Exception: Propagates validation or runtime failures from the underlying operation.
        """
        selected = _coerce_lane(lane)
        for cap in self.caps:
            if cap.lane is selected:
                return cap
        raise ConcurrencyProfileError("lane-missing", selected.value)

    def to_resource_budget(self) -> ResourceBudget:
        """Convert profile caps to the existing resource-governor budget envelope."""
        return ResourceBudget(
            max_vram_gb=self.max_vram_gb,
            max_ram_gb=self.max_ram_gb,
            max_cpu_threads=self.max_cpu_threads,
            max_storage_gb=self.max_storage_gb,
            max_queue_depth=self.max_queue_depth,
            max_context_tokens=self.max_context_tokens,
            max_kv_cache_gb=self.max_kv_cache_gb,
            max_agent_slots=self.cap_for(ConcurrencyLane.AGENT_ORCHESTRATION).max_active,
            max_rag_jobs=self.cap_for(ConcurrencyLane.RAG_INDEXING).max_active,
            max_downloads=self.cap_for(ConcurrencyLane.DOWNLOAD_MATERIALIZE).max_active,
            max_eval_jobs=self.cap_for(ConcurrencyLane.EVAL_SWEEP).max_active,
            max_training_jobs=self.cap_for(ConcurrencyLane.GPU_TRAINING).max_active,
            interactive_vram_reserve_gb=self.interactive_vram_reserve_gb,
            interactive_ram_reserve_gb=self.interactive_ram_reserve_gb,
        )

    def __repr__(self) -> str:
        """Return a compact diagnostic representation."""
        return f"ActiveUserConcurrencyProfile(profile_id={self.profile_id!r}, description={self.description!r}, caps={self.caps!r})"


@dataclass(frozen=True, slots=True)
class ConcurrencyProfileSet:
    """Validated collection of concurrency profiles."""

    profiles: tuple[ActiveUserConcurrencyProfile, ...]
    default_profile_id: str

    def __post_init__(self) -> None:
        if not self.profiles:
            raise ConcurrencyProfileError("profiles-empty", "at least one profile is required")
        ids = [profile.profile_id for profile in self.profiles]
        if len(ids) != len(set(ids)):
            raise ConcurrencyProfileError("profile-id-duplicate", "profile ids must be unique")
        if self.default_profile_id not in set(ids):
            raise ConcurrencyProfileError("default-profile-missing", self.default_profile_id)

    def profile_for(self, profile_id: str | None) -> ActiveUserConcurrencyProfile:
        """Return a profile by id, failing closed when the id is absent.

        Returns:
            ActiveUserConcurrencyProfile value produced by profile_for().

        Raises:
            Exception: Propagates validation or runtime failures from the underlying operation.
        """
        if not profile_id:
            raise ConcurrencyProfileError("profile-id-missing", "profile_id is required")
        for profile in self.profiles:
            if profile.profile_id == profile_id:
                return profile
        raise ConcurrencyProfileError("profile-not-found", profile_id)


@dataclass(frozen=True, slots=True)
class ConcurrencyRequest:
    """One admission request against an active-user concurrency profile."""

    profile_id: str
    lane: ConcurrencyLane | str
    workload_id: str
    active_counts: dict[ConcurrencyLane | str, int]
    queued_counts: dict[ConcurrencyLane | str, int] | None = None
    user_active: bool = True
    gpu_required: bool = False
    can_downgrade: bool = False
    can_batch: bool = False
    agent_role: str | None = None
    specialist_task: str | None = None

    def __post_init__(self) -> None:
        _require_text(self.workload_id, "workload_id")
        if not isinstance(self.active_counts, dict):
            raise ConcurrencyProfileError("active-counts-invalid", "active_counts must be a mapping")
        if self.queued_counts is not None and not isinstance(self.queued_counts, dict):
            raise ConcurrencyProfileError("queued-counts-invalid", "queued_counts must be a mapping")

    def __repr__(self) -> str:
        """Return a compact diagnostic representation."""
        return (
            f"ConcurrencyRequest(profile_id={self.profile_id!r}, lane={self.lane!r}, workload_id={self.workload_id!r})"
        )


@dataclass(frozen=True, slots=True)
class ConcurrencyDecision:
    """Admission decision with user-visible reasons."""

    profile_id: str
    lane: ConcurrencyLane
    workload_id: str
    action: ConcurrencyAction
    reasons: tuple[str, ...]
    cap: ConcurrencyCap
    active_count: int

    @property
    def admitted(self) -> bool:
        """Whether the request may start immediately."""
        return self.action is ConcurrencyAction.ADMIT

    def __repr__(self) -> str:
        """Return a compact diagnostic representation."""
        return (
            f"ConcurrencyDecision(profile_id={self.profile_id!r}, lane={self.lane!r}, workload_id={self.workload_id!r})"
        )


def load_concurrency_profiles(path: Path | str = DEFAULT_CONCURRENCY_PROFILES_PATH) -> ConcurrencyProfileSet:
    """Load and validate active-user concurrency profiles from static YAML.

    Returns:
        Resolved concurrency profiles value.

    Raises:
        Exception: Propagates validation or runtime failures from the underlying operation.
    """
    profile_path = Path(path)
    if not profile_path.exists():
        raise ConcurrencyProfileError("profile-config-not-found", str(profile_path))
    try:
        raw = yaml.safe_load(profile_path.read_text(encoding="utf-8"))
    except OSError as exc:
        raise ConcurrencyProfileError("profile-config-unreadable", str(profile_path)) from exc
    except yaml.YAMLError as exc:
        raise ConcurrencyProfileError("profile-config-malformed", str(profile_path)) from exc
    if not isinstance(raw, dict) or not isinstance(raw.get("concurrency_profiles"), dict):
        raise ConcurrencyProfileError("profile-config-invalid", "missing concurrency_profiles mapping")
    payload = raw["concurrency_profiles"]
    default_profile_id = str(payload.get("default_profile_id", ""))
    raw_profiles = payload.get("profiles")
    if not isinstance(raw_profiles, list):
        raise ConcurrencyProfileError("profiles-invalid", "profiles must be a list")
    return ConcurrencyProfileSet(
        profiles=tuple(_profile_from_mapping(item) for item in raw_profiles),
        default_profile_id=default_profile_id,
    )


def decide_concurrency(
    request: ConcurrencyRequest,
    *,
    profiles: ConcurrencyProfileSet | None = None,
) -> ConcurrencyDecision:
    """Return a fail-closed concurrency decision for a workload request.

    Returns:
        ConcurrencyDecision value produced by decide_concurrency().
    """
    selected_profiles = profiles or load_concurrency_profiles()
    profile = selected_profiles.profile_for(request.profile_id)
    lane = _coerce_lane(request.lane)
    active_counts = _coerce_active_counts(request.active_counts)
    queued_counts = _coerce_active_counts(request.queued_counts or {})
    active_count = active_counts.get(lane, 0)
    queued_count = queued_counts.get(lane, 0)
    cap = profile.cap_for(lane)

    pre_cap_decision = _pre_cap_decision(request, profile, lane, cap, active_count)
    if pre_cap_decision is not None:
        return pre_cap_decision

    if active_count < cap.max_active:
        if cap.batch_after is not None and request.can_batch and active_count >= cap.batch_after:
            return _decision(
                request,
                lane,
                cap,
                active_count,
                ConcurrencyAction.BATCH,
                f"{cap.label} can batch with existing cpu specialist work",
            )
        return _decision(request, lane, cap, active_count, ConcurrencyAction.ADMIT, f"{cap.label} admitted")

    if cap.burst_active and active_count < cap.max_active + cap.burst_active:
        return _decision(
            request,
            lane,
            cap,
            active_count,
            ConcurrencyAction.ADMIT,
            f"{cap.label} admitted on burst capacity active={active_count} burst={cap.burst_active}",
        )

    return _over_cap_decision(request, profile, lane, cap, active_count, queued_count)


def _pre_cap_decision(
    request: ConcurrencyRequest,
    profile: ActiveUserConcurrencyProfile,
    lane: ConcurrencyLane,
    cap: ConcurrencyCap,
    active_count: int,
) -> ConcurrencyDecision | None:
    if lane is ConcurrencyLane.CPU_SPECIALIST and (
        not _present(request.agent_role) or not _present(request.specialist_task)
    ):
        return _decision(
            request,
            lane,
            cap,
            active_count,
            ConcurrencyAction.APPROVAL_REQUIRED,
            "cpu specialist work needs declared agent role and specialist task",
        )
    if cap.requires_approval:
        return _decision(
            request,
            lane,
            cap,
            active_count,
            ConcurrencyAction.APPROVAL_REQUIRED,
            f"{cap.label} requires operator approval in profile {profile.profile_id}",
        )
    return None


def _over_cap_decision(
    request: ConcurrencyRequest,
    profile: ActiveUserConcurrencyProfile,
    lane: ConcurrencyLane,
    cap: ConcurrencyCap,
    active_count: int,
    queued_count: int,
) -> ConcurrencyDecision:
    if request.can_downgrade and cap.over_cap_action is ConcurrencyAction.DOWNGRADE:
        return _decision(
            request,
            lane,
            cap,
            active_count,
            ConcurrencyAction.DOWNGRADE,
            f"{cap.label} over cap; downgrade to a cheaper lane",
        )
    if request.can_batch and cap.over_cap_action is ConcurrencyAction.BATCH:
        return _decision(request, lane, cap, active_count, ConcurrencyAction.BATCH, f"{cap.label} over cap; batch work")
    if cap.over_cap_action is ConcurrencyAction.QUEUE and queued_count >= profile.max_queue_depth:
        return _decision(
            request,
            lane,
            cap,
            active_count,
            ConcurrencyAction.DENY,
            f"{cap.label} queue full queued={queued_count} max_queue_depth={profile.max_queue_depth}",
        )
    return _decision(
        request,
        lane,
        cap,
        active_count,
        cap.over_cap_action,
        f"{cap.label} active={active_count} cap={cap.max_active}; {cap.over_cap_action.value}",
    )


def _profile_from_mapping(raw: object) -> ActiveUserConcurrencyProfile:
    if not isinstance(raw, dict):
        raise ConcurrencyProfileError("profile-invalid", "profile entries must be mappings")
    budget = raw.get("resource_budget")
    if not isinstance(budget, dict):
        raise ConcurrencyProfileError("profile-budget-invalid", str(raw.get("profile_id", "")))
    caps = raw.get("caps")
    if not isinstance(caps, list):
        raise ConcurrencyProfileError("profile-caps-invalid", str(raw.get("profile_id", "")))
    return ActiveUserConcurrencyProfile(
        profile_id=str(raw.get("profile_id", "")),
        description=str(raw.get("description", "")),
        caps=tuple(_cap_from_mapping(item) for item in caps),
        max_vram_gb=float(budget.get("max_vram_gb", -1)),
        max_ram_gb=float(budget.get("max_ram_gb", -1)),
        max_cpu_threads=int(budget.get("max_cpu_threads", -1)),
        max_storage_gb=float(budget.get("max_storage_gb", -1)),
        max_queue_depth=int(budget.get("max_queue_depth", -1)),
        max_context_tokens=int(budget.get("max_context_tokens", -1)),
        max_kv_cache_gb=float(budget.get("max_kv_cache_gb", -1)),
        interactive_vram_reserve_gb=float(budget.get("interactive_vram_reserve_gb", 2.0)),
        interactive_ram_reserve_gb=float(budget.get("interactive_ram_reserve_gb", 4.0)),
    )


def _cap_from_mapping(raw: object) -> ConcurrencyCap:
    if not isinstance(raw, dict):
        raise ConcurrencyProfileError("cap-invalid", "cap entries must be mappings")
    return ConcurrencyCap(
        lane=_coerce_lane(raw.get("lane")),
        max_active=int(raw.get("max_active", -1)),
        label=str(raw.get("label", "")),
        over_cap_action=_coerce_action(raw.get("over_cap_action")),
        burst_active=int(raw.get("burst_active", 0)),
        batch_after=int(raw["batch_after"]) if "batch_after" in raw else None,
        requires_approval=bool(raw.get("requires_approval", False)),
    )


def _coerce_active_counts(active_counts: dict[ConcurrencyLane | str, int]) -> dict[ConcurrencyLane, int]:
    coerced: dict[ConcurrencyLane, int] = {}
    for raw_lane, raw_count in active_counts.items():
        lane = _coerce_lane(raw_lane)
        if not isinstance(raw_count, int) or raw_count < 0:
            raise ConcurrencyProfileError("active-count-negative", lane.value)
        coerced[lane] = raw_count
    return coerced


def _decision(
    request: ConcurrencyRequest,
    lane: ConcurrencyLane,
    cap: ConcurrencyCap,
    active_count: int,
    action: ConcurrencyAction,
    reason: str,
) -> ConcurrencyDecision:
    return ConcurrencyDecision(
        profile_id=request.profile_id,
        lane=lane,
        workload_id=request.workload_id,
        action=action,
        reasons=(reason,),
        cap=cap,
        active_count=active_count,
    )


def _coerce_lane(value: object) -> ConcurrencyLane:
    if isinstance(value, ConcurrencyLane):
        return value
    raw_value = value.value if isinstance(value, Enum) else value
    try:
        return ConcurrencyLane(raw_value)
    except ValueError as exc:
        raise ConcurrencyProfileError("lane-unknown", str(value)) from exc


def _coerce_action(value: object) -> ConcurrencyAction:
    if isinstance(value, ConcurrencyAction):
        return value
    raw_value = value.value if isinstance(value, Enum) else value
    try:
        return ConcurrencyAction(raw_value)
    except ValueError as exc:
        raise ConcurrencyProfileError("action-unknown", str(value)) from exc


def _present(value: str | None) -> bool:
    return isinstance(value, str) and bool(value.strip())


def _require_text(value: object, field_name: str) -> None:
    if not isinstance(value, str) or not value.strip():
        raise ConcurrencyProfileError(f"{field_name}-missing", f"{field_name} must be non-empty")


def _require_non_negative_int(value: int, field_name: str) -> None:
    if not isinstance(value, int) or value < 0:
        raise ConcurrencyProfileError(f"{field_name}-invalid", f"{field_name} must be non-negative")


def _require_non_negative_number(value: Any, field_name: str) -> None:
    if not isinstance(value, (int, float)) or value < 0:
        raise ConcurrencyProfileError(f"{field_name}-invalid", f"{field_name} must be non-negative")


__all__ = [
    "DEFAULT_CONCURRENCY_PROFILES_PATH",
    "ActiveUserConcurrencyProfile",
    "ConcurrencyAction",
    "ConcurrencyCap",
    "ConcurrencyDecision",
    "ConcurrencyLane",
    "ConcurrencyProfileError",
    "ConcurrencyProfileSet",
    "ConcurrencyRequest",
    "decide_concurrency",
    "load_concurrency_profiles",
]
