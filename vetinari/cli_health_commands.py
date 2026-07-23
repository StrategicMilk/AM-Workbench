"""Health command helpers for the Vetinari CLI."""

from __future__ import annotations

import logging
from collections.abc import Callable

from vetinari.i18n.cli import require_cli_text

logger = logging.getLogger(__name__)


def run_health_check_quiet(health_failure_hint: Callable[[str], str]) -> bool:
    """Run health checks on all providers and print results to stdout.

    Returns:
        Value produced for the caller.
    """
    healthy = True
    try:
        from vetinari.adapters.adapter_cache import get_local_inference_adapter

        adapter = get_local_inference_adapter("cli-health")
        is_healthy = adapter.is_healthy()
        if is_healthy:
            models = adapter.list_loaded_models()
            print(f"  {require_cli_text('health.local_ok')}: OK ({len(models)} models)")
        else:
            print(f"  {require_cli_text('health.local_fail')}: FAIL (unhealthy)")
            print(f"    {require_cli_text('health.hint', hint=health_failure_hint('unhealthy'))}")
            healthy = False
    except Exception as exc:
        print(f"  {require_cli_text('health.local_fail')}: FAIL ({exc})")
        print(f"    {require_cli_text('health.hint', hint=health_failure_hint(str(exc)))}")
        healthy = False

    try:
        from vetinari.adapter_manager import get_adapter_manager

        mgr = get_adapter_manager()
        results = mgr.health_check()
        for name, info in results.items():
            status = "OK" if info.get("healthy") else "FAIL"
            print(f"  {name:20s}: {status}")
            if not info.get("healthy"):
                reason = str(info.get("reason", ""))
                if reason:
                    print(f"    {require_cli_text('health.reason', reason=reason)}")
                print(f"    {require_cli_text('health.hint', hint=health_failure_hint(reason))}")
                healthy = False
    except Exception as exc:
        logger.warning("Adapter manager health check unavailable", exc_info=True)
        reason = str(exc) or require_cli_text("health.unknown")
        print(f"  {require_cli_text('health.adapter_manager_fail')}: FAIL ({reason})")
        print(f"    {require_cli_text('health.hint', hint=health_failure_hint(reason))}")
        healthy = False
    return healthy


__all__ = ["run_health_check_quiet"]
