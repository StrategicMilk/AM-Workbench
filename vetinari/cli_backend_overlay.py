"""CLI for backend overlay validation and apply planning."""

from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

from vetinari.setup.backend_overlay import (
    DEFAULT_MANIFEST_PATH,
    DEFAULT_PINS_PATH,
    BackendOverlayError,
    dry_run_overlay,
    find_overlay_manifest,
    load_overlay_manifests,
    plan_overlay_apply,
)

logger = logging.getLogger(__name__)


def _add_common_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--manifest", type=Path, default=DEFAULT_MANIFEST_PATH, help="Overlay manifest YAML path.")
    parser.add_argument("--pins", type=Path, default=DEFAULT_PINS_PATH, help="Backend pins YAML path.")


def _add_backend_arg(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--backend", default="vllm", help="Backend key to inspect.")


def _cmd_check(args: argparse.Namespace) -> int:
    manifests = load_overlay_manifests(args.manifest, pins_path=args.pins)
    sys.stdout.write(f"validated {len(manifests)} backend overlay manifest(s)\n")
    return 0


def _cmd_dry_run(args: argparse.Namespace) -> int:
    manifest = find_overlay_manifest(args.backend, args.manifest, pins_path=args.pins)
    plan = dry_run_overlay(manifest)
    sys.stdout.write(f"status: {plan.status}\n")
    for command in plan.commands:
        sys.stdout.write(f"command: {command}\n")
    return 0


def _cmd_apply_plan(args: argparse.Namespace) -> int:
    manifest = find_overlay_manifest(args.backend, args.manifest, pins_path=args.pins)
    plan = plan_overlay_apply(manifest, explicit_approval=args.approve)
    sys.stdout.write(f"status: {plan.status}\n")
    for diagnostic in plan.diagnostics:
        sys.stdout.write(f"diagnostic: {diagnostic}\n")
    for command in plan.commands:
        sys.stdout.write(f"command: {command}\n")
    sys.stdout.write(f"rollback: {plan.rollback_command}\n")
    return 0 if plan.status in {"ready", "dry-run"} else 2


def _cmd_rollback(args: argparse.Namespace) -> int:
    manifest = find_overlay_manifest(args.backend, args.manifest, pins_path=args.pins)
    sys.stdout.write(f"{manifest.rollback_command}\n")
    return 0


def _cmd_rebase_status(args: argparse.Namespace) -> int:
    manifest = find_overlay_manifest(args.backend, args.manifest, pins_path=args.pins)
    sys.stdout.write(f"{manifest.backend}: {manifest.rebase_status}\n")
    return 0 if manifest.rebase_status == "clean" else 2


def _write_error(message: str) -> None:
    sys.stderr.write(f"{message}\n")


def build_parser() -> argparse.ArgumentParser:
    """Execute the build parser operation.

    Returns:
        Newly constructed parser value.
    """
    parser = argparse.ArgumentParser(description="Validate and plan governed backend overlays.")
    subparsers = parser.add_subparsers(dest="command")

    check = subparsers.add_parser("check", help="Validate overlay manifests without applying patches.")
    _add_common_args(check)
    check.set_defaults(func=_cmd_check)

    dry_run = subparsers.add_parser("dry-run", help="Show patch-check commands without applying patches.")
    _add_common_args(dry_run)
    _add_backend_arg(dry_run)
    dry_run.set_defaults(func=_cmd_dry_run)

    apply_plan = subparsers.add_parser("apply-plan", help="Build an approval-gated apply plan.")
    _add_common_args(apply_plan)
    _add_backend_arg(apply_plan)
    apply_plan.add_argument("--approve", action="store_true", help="Confirm explicit approval for apply planning.")
    apply_plan.set_defaults(func=_cmd_apply_plan)

    rollback = subparsers.add_parser("rollback", help="Print the recorded rollback command.")
    _add_common_args(rollback)
    _add_backend_arg(rollback)
    rollback.set_defaults(func=_cmd_rollback)

    rebase = subparsers.add_parser("rebase-status", help="Print the recorded overlay rebase status.")
    _add_common_args(rebase)
    _add_backend_arg(rebase)
    rebase.set_defaults(func=_cmd_rebase_status)

    parser.set_defaults(func=_cmd_check)
    return parser


def main(argv: list[str] | None = None) -> int:
    """Execute the main operation.

    Returns:
        int value produced by main().
    """
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return int(args.func(args))
    except BackendOverlayError as exc:
        logger.warning("Handled recoverable failure before fallback.", exc_info=True)
        _write_error(f"Backend overlay could not be validated: {exc}")
        _write_error("Run `python -m vetinari.cli_backend_overlay check` after fixing the manifest or pins file.")
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
