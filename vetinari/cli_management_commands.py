"""Management command implementations for the Vetinari CLI."""

from __future__ import annotations

import logging
import os
from typing import Any

logger = logging.getLogger(__name__)


def cmd_prompt(args: Any) -> int:
    """Manage agent prompt versions: history and rollback.

    Returns:
        Value produced for the caller.
    """
    agent_name = args.agent
    if any(ch in agent_name for ch in ("/", "\\", "..", "\x00")) or agent_name.startswith("."):
        print(f"Error: invalid agent name {agent_name!r} - must be a plain identifier")
        return 1

    from vetinari.prompts import get_version_manager

    mgr = get_version_manager()

    if args.action == "history":
        try:
            history = mgr.get_history(args.agent.upper(), args.mode)
        except ValueError as exc:
            logger.warning("Could not retrieve prompt history for agent %s - invalid agent name", args.agent)
            print(f"Error: invalid agent name - {exc}")
            return 1
        if not history:
            print(f"No prompt versions found for {args.agent}:{args.mode}")
            return 0
        print(f"Prompt history for {args.agent}:{args.mode}:")
        for version in history:
            score_str = f" (score: {version.quality_score:.3f})" if version.quality_score is not None else ""
            print(
                f"  {version.version}  {version.timestamp[:19]}  {version.checksum[:12]}...{score_str}  {version.notes}"
            )
        return 0

    if args.action == "rollback":
        if not args.version:
            print("Error: --version is required for rollback")
            return 1
        try:
            result = mgr.rollback(args.agent.upper(), args.mode, args.version)
        except ValueError as exc:
            logger.warning("Could not rollback prompt version for agent %s - invalid agent name", args.agent)
            print(f"Error: invalid agent name - {exc}")
            return 1
        if result:
            print(f"Rolled back {args.agent}:{args.mode} to version {args.version} (new version: {result.version})")
            return 0
        print(f"Version {args.version} not found for {args.agent}:{args.mode}")
        return 1

    return 0


def cmd_migrate(args: Any) -> int:
    """Apply database migrations to initialise or upgrade storage schemas.

    Returns:
        Value produced for the caller.
    """
    from vetinari.cli_startup import _setup_logging
    from vetinari.migrations import run_migrations

    db_path = args.db_path or os.environ.get("VETINARI_DB_PATH", ".vetinari/vetinari.db")
    _setup_logging(getattr(args, "verbose", False))
    logger.info("Running migrations on %s", db_path)
    try:
        applied = run_migrations(db_path)
        print(f"Migrations complete - {applied} applied to {db_path}")
        return 0
    except Exception:
        logger.exception("Migration failed")
        return 1


def cmd_capability_packs(args: Any) -> int:
    """Inspect and manage trusted Workbench capability packs.

    Returns:
        Value produced for the caller.

    Raises:
        CapabilityPackError: Propagated when validation, persistence, or execution fails.
    """
    from vetinari.workbench.capability_packs import CapabilityPackError, CapabilityPackService

    def machine_value(value: Any) -> str:
        raw = getattr(value, "value", value)
        return str(raw).strip()

    action = getattr(args, "capability_packs_action", None)
    pack_id = getattr(args, "pack_id", None)
    service = CapabilityPackService()

    try:
        if action == "list":
            packs = service.list_packs()
            for row in packs:
                enablement = row.get("enablement", {})
                print(
                    f"{row['pack_id']} {row['version']} "
                    f"{machine_value(row['capability_kind'])} "
                    f"{machine_value(enablement.get('status', 'unknown'))}"
                )
            return 0
        if action == "status":
            pack = service.get_pack(pack_id)
            decision = service.evaluate_enablement(pack_id)
            print(f"{pack.pack_id}: {machine_value(decision.status)} allowed={decision.allowed}")
            for reason in decision.reasons:
                print(f"- {reason}")
            return 0 if decision.allowed else 1
        if action == "install":
            raise CapabilityPackError(
                "capability-pack install is approval-gated through the Workbench install-on-demand flow; "
                "use 'enable' to verify local trust/enablement only"
            )
        if action == "enable":
            decision = service.enable_pack(pack_id)
        elif action == "disable":
            decision = service.disable_pack(pack_id)
        elif action == "uninstall":
            decision = service.uninstall_pack(pack_id)
        elif action == "smoke-test":
            decision = service.smoke_test_pack(pack_id)
        else:
            print("Error: capability-packs action required")
            return 1
    except (CapabilityPackError, RuntimeError, OSError, ValueError) as exc:
        logger.warning("Capability pack command failed", exc_info=True)
        print(f"[AM Workbench] Capability pack error: {exc}")
        return 1

    print(f"{pack_id}: {machine_value(decision.status)} allowed={decision.allowed}")
    for reason in decision.reasons:
        print(f"- {reason}")
    return 0 if decision.allowed else 1


__all__ = ["cmd_capability_packs", "cmd_migrate", "cmd_prompt"]
