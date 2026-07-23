"""Startup wiring for the autonomy and notification subsystems.

Called during application startup to initialize the governor, approval queue,
notification channels, and daily digest schedule. This is the single entry
point that wires all Session 4A components into the running application.
"""

from __future__ import annotations

import logging

from vetinari.boundary_guards import assert_dependency_success

logger = logging.getLogger(__name__)
_STARTUP_STATUS: dict[str, dict[str, str | bool]] = {}


def get_autonomy_startup_status() -> dict[str, dict[str, str | bool]]:
    """Return startup wiring status for optional autonomy subsystems."""
    return {key: dict(value) for key, value in _STARTUP_STATUS.items()}


def _record_startup_status(name: str, *, ok: bool, detail: str = "") -> None:
    _STARTUP_STATUS[name] = {"ok": ok, "detail": detail}


def _raise_startup_failure(name: str, exc: BaseException) -> None:
    _record_startup_status(name, ok=False, detail=str(exc))
    try:
        assert_dependency_success(False, dependency_id=name)
    except RuntimeError:
        raise RuntimeError(f"{name} startup failed: {exc}") from exc


def _wire_autonomy_governor() -> None:
    """Initialize the autonomy governor and log degraded startup."""
    try:
        from vetinari.autonomy.governor import get_governor

        governor = get_governor()
        trust_status = governor.get_trust_status()
        logger.info(
            "Autonomy governor initialized — %d action types tracked",
            len(trust_status),
        )
    except Exception as exc:
        _raise_startup_failure("autonomy_governor", exc)
        logger.warning("Failed to initialize autonomy governor — autonomous actions will be gated by default")


def _wire_promotion_checker() -> None:
    """Verify the promotion checker is available for scheduled invocation."""
    try:
        from vetinari.autonomy.governor import get_governor

        governor = get_governor()
        if not callable(getattr(governor, "check_pending_promotions", None)):
            raise AttributeError("governor.check_pending_promotions is not callable")
        logger.info("Promotion checker available (call governor.check_pending_promotions() on schedule)")
    except Exception as exc:
        _raise_startup_failure("promotion_checker", exc)
        logger.warning("Promotion checker initialization failed — scheduled promotions unavailable")


def _wire_notification_manager() -> None:
    """Initialize the notification manager singleton."""
    try:
        from vetinari.notifications.manager import get_notification_manager

        get_notification_manager()
        logger.info("Notification manager initialized")
    except Exception as exc:
        _raise_startup_failure("notification_manager", exc)
        logger.warning("Failed to initialize notification manager — notifications disabled")


def _wire_desktop_notifications() -> None:
    """Register the optional desktop notification channel when available."""
    try:
        from vetinari.notifications.desktop import create_desktop_channel

        channel = create_desktop_channel()
        if channel:
            logger.info("Desktop notification channel registered")
    except Exception as exc:
        _raise_startup_failure("desktop_notifications", exc)
        logger.warning("Desktop notifications unavailable — notification channel not registered")


def _wire_webhook_notifications() -> None:
    """Register the optional webhook notification channel when configured."""
    try:
        from vetinari.notifications.webhook import create_webhook_channel

        channel = create_webhook_channel()
        if channel:
            logger.info("Webhook notification channel registered")
    except Exception as exc:
        _raise_startup_failure("webhook_notifications", exc)
        logger.warning("Webhook notifications unavailable — webhook channel not registered")


def _wire_digest_generator() -> None:
    """Verify the manual digest generator can be constructed."""
    try:
        from vetinari.notifications.digest import DigestGenerator

        generator = DigestGenerator()
        if not callable(getattr(generator, "send_digest", None)):
            raise RuntimeError("DigestGenerator missing send_digest")
        assert_dependency_success(True, dependency_id="digest_generator")
        logger.info("Daily digest generator available (manual trigger only — no scheduler wired)")
    except Exception as exc:
        _raise_startup_failure("digest_generator", exc)
        logger.warning("Daily digest initialization failed — digest reports unavailable")


def _wire_confidence_gate() -> None:
    """Verify the confidence gate can be constructed for inference routing."""
    try:
        from vetinari.agents.confidence_gate import ConfidenceGate

        _gate = ConfidenceGate()
        # The gate is used by the orchestration layer after agent inference:
        #   decision = gate.route_by_confidence(logprobs, task_type)
        # The TwoLayerOrchestrator can call this after _infer() returns.
        if not callable(getattr(_gate, "route_by_confidence", None)):
            raise RuntimeError("ConfidenceGate missing route_by_confidence")
        assert_dependency_success(True, dependency_id="confidence_gate")
        logger.info(
            "Confidence gate initialized (thresholds: high=%.1f, med=%.1f, low=%.1f)",
            _gate._threshold_high,
            _gate._threshold_medium,
            _gate._threshold_low,
        )
        _record_startup_status("confidence_gate", ok=True, detail="initialized")
    except Exception as exc:
        _record_startup_status("confidence_gate", ok=False, detail=str(exc))
        logger.error("Confidence gate initialization failed; confidence-based routing is unavailable", exc_info=True)


def _wire_episodic_recall() -> None:
    """Verify episodic recall is importable for plan generation."""
    try:
        from vetinari.learning.episodic_recall import recall_similar_episodes

        if not callable(recall_similar_episodes):
            raise RuntimeError("recall_similar_episodes is not callable")
        assert_dependency_success(True, dependency_id="episodic_recall")
        logger.info("Episodic recall (recall_similar_episodes) wired")
    except Exception as exc:
        _raise_startup_failure("episodic_recall", exc)
        logger.warning("Episodic recall initialization failed — planning will proceed without episode history")


def _wire_multi_perspective_review() -> None:
    """Verify multi-perspective review is importable for Inspector."""
    try:
        from vetinari.agents.consolidated.quality_agent import run_multi_perspective_review

        if not callable(run_multi_perspective_review):
            raise RuntimeError("run_multi_perspective_review is not callable")
        assert_dependency_success(True, dependency_id="multi_perspective_review")
        logger.info("Multi-perspective review wired")
    except Exception as exc:
        _raise_startup_failure("multi_perspective_review", exc)
        logger.warning("Multi-perspective review initialization failed — Inspector will use single-perspective review")


def wire_autonomy_and_notifications() -> None:
    """Initialize autonomy governor, notification channels, and digest schedule.

    Call this during application startup (after event bus is ready).
    Failures in optional channels (desktop, webhook) are logged but don't
    block startup.

    Raises:
        No exceptions are intentionally raised; subsystem startup failures are
        logged and degraded in place.
    """
    for initializer in (
        _wire_autonomy_governor,
        _wire_promotion_checker,
        _wire_notification_manager,
        _wire_desktop_notifications,
        _wire_webhook_notifications,
        _wire_digest_generator,
        _wire_confidence_gate,
        _wire_episodic_recall,
        _wire_multi_perspective_review,
    ):
        initializer()
