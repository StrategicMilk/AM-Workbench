"""Deterministic habit cadence and user-rhythm calculations."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from vetinari.workbench.habit_health.contracts import (
    FatigueRisk,
    HabitCheckIn,
    HabitRhythmSnapshot,
    HabitRoutine,
)


def compute_rhythm_snapshot(
    check_ins: tuple[HabitCheckIn, ...] | list[HabitCheckIn],
    *,
    routines: tuple[HabitRoutine, ...] | list[HabitRoutine] = (),
    now_utc: str | datetime | None = None,
    user_id: str = "unknown",
) -> HabitRhythmSnapshot:
    """Compute missed cadence, streak, agent rhythm, and fatigue labels.

    Returns:
        Computed rhythm snapshot result.
    """
    now = _parse_time(now_utc) if now_utc is not None else datetime.now(UTC)
    normalized = tuple(sorted(check_ins, key=lambda item: _parse_time(item.checked_at_utc)))
    routine_map = {routine.routine_id: routine for routine in routines}
    stale: list[str] = []
    for routine in routines:
        last = next((item for item in reversed(normalized) if item.routine_id == routine.routine_id), None)
        if last is None:
            continue
        checked_at = _parse_time(last.checked_at_utc)
        cadence = timedelta(hours=routine.cadence.interval_hours + routine.cadence.grace_hours)
        if now - checked_at > cadence:
            stale.append(routine.routine_id)

    missed_count = len(stale)
    streak_count = _streak_count(normalized, routine_map)
    fatigue_risk, reasons = _fatigue_risk(normalized)
    agent_refs = tuple(dict.fromkeys(ref for item in normalized for ref in item.agent_run_refs if ref))
    quiet_hints = tuple(
        f"{routine.routine_id}:{routine.cadence.quiet_window_start}-{routine.cadence.quiet_window_end}"
        for routine in routines
        if routine.cadence.quiet_window_start and routine.cadence.quiet_window_end
    )
    return HabitRhythmSnapshot(
        user_id=user_id,
        generated_at_utc=_to_iso(now),
        streak_count=streak_count,
        missed_count=missed_count,
        stale_routine_ids=tuple(stale),
        fatigue_risk=fatigue_risk,
        quiet_window_hints=quiet_hints,
        agent_run_refs=agent_refs,
        reasons=reasons,
    )


def _streak_count(check_ins: tuple[HabitCheckIn, ...], routine_map: dict[str, HabitRoutine]) -> int:
    if not check_ins:
        return 0
    routine_id = check_ins[-1].routine_id
    routine = routine_map.get(routine_id)
    if routine is None:
        return 1
    cadence = timedelta(hours=routine.cadence.interval_hours + routine.cadence.grace_hours)
    count = 1
    previous = _parse_time(check_ins[-1].checked_at_utc)
    for item in reversed(check_ins[:-1]):
        if item.routine_id != routine_id:
            continue
        current = _parse_time(item.checked_at_utc)
        if previous - current <= cadence:
            count += 1
            previous = current
            continue
        break
    return count


def _fatigue_risk(check_ins: tuple[HabitCheckIn, ...]) -> tuple[FatigueRisk, tuple[str, ...]]:
    if not check_ins:
        return FatigueRisk.UNKNOWN, ("insufficient-evidence",)
    recent = check_ins[-3:]
    if any(not item.source_context.strip() for item in recent):
        return FatigueRisk.UNKNOWN, ("source-context-missing",)
    low_energy = sum(1 for item in recent if item.energy is not None and item.energy <= 2)
    low_focus = sum(1 for item in recent if item.focus is not None and item.focus <= 2)
    if low_energy >= 2 or low_focus >= 2:
        return FatigueRisk.NEEDS_REVIEW, ("user-reported-low-energy-or-focus", "informational-not-medical")
    if any(item.agent_run_refs for item in recent) and len(recent) >= 3:
        return FatigueRisk.HIGH_FRICTION, ("dense-agent-run-rhythm", "informational-not-medical")
    return FatigueRisk.STEADY, ("user-reported-steady", "informational-not-medical")


def _parse_time(value: str | datetime) -> datetime:
    if isinstance(value, datetime):
        parsed = value
    else:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def _to_iso(value: datetime) -> str:
    return value.astimezone(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")


__all__ = ["compute_rhythm_snapshot"]
