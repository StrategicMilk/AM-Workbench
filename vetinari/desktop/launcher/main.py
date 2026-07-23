"""Command line entry point for the hybrid AM Workbench launcher."""

from __future__ import annotations

import argparse
import json
import logging
import sys
from collections.abc import Sequence

from vetinari.desktop.bootstrap import BootstrapMode, LauncherBootstrap, register_default_probes_and_releasers

logger = logging.getLogger(__name__)

DEVELOPER_WORKFLOW_CONTRACT_ID = "wave3-rcg0014p06-scope-followup-P09"
LAUNCHER_WORKFLOW_GUARDS: tuple[str, ...] = (
    "print-plan mode builds exactly one LauncherBootstrap instance",
    "runtime mode registers default probes before backend startup",
    "unready backend status returns exit code 2 with structured stderr",
    "background-only mode rejects UI opening after readiness succeeds",
)


def developer_workflow_contract() -> dict[str, object]:
    """Return launcher workflow guarantees verified by this follow-up pack."""
    return {
        "pack": DEVELOPER_WORKFLOW_CONTRACT_ID,
        "surface": "vetinari/desktop/launcher/main.py",
        "guards": LAUNCHER_WORKFLOW_GUARDS,
    }


def build_parser() -> argparse.ArgumentParser:
    """Execute the build parser operation.

    Returns:
        Newly constructed parser value.
    """
    parser = argparse.ArgumentParser(description="Start the AM Workbench desktop launcher.")
    parser.add_argument(
        "--print-plan", action="store_true", help="Print the launch plan as JSON without starting services."
    )
    parser.add_argument(
        "--mode",
        choices=[mode.value for mode in BootstrapMode],
        default=BootstrapMode.DESKTOP_DEFAULT.value,
        help="Launcher mode.",
    )
    parser.add_argument("--no-tray", action="store_true", help="Do not enter the tray loop.")
    return parser


def run(argv: Sequence[str] | None = None) -> int:
    """Execute the run operation.

    Returns:
        int value produced by run().
    """
    args = build_parser().parse_args(argv)
    if args.print_plan:
        bootstrap = LauncherBootstrap(mode=BootstrapMode(args.mode))
        plan = bootstrap.plan()
        sys.stdout.write(json.dumps(plan.to_dict(), sort_keys=True) + "\n")
        return 0

    from vetinari.runtime.app_lifecycle import get_app_lifecycle

    controller = get_app_lifecycle()
    register_default_probes_and_releasers(controller)
    bootstrap = LauncherBootstrap(mode=BootstrapMode(args.mode), controller=controller)
    status = bootstrap.start_backend()
    if not status.is_ready:
        status = bootstrap.wait_for_health()
    if not status.is_ready:
        sys.stderr.write(json.dumps(status.to_dict(), sort_keys=True) + "\n")
        return 2
    if status.is_ready and BootstrapMode(args.mode) is BootstrapMode.BROWSER_OPEN:
        bootstrap.open_in_browser()
    elif status.is_ready and BootstrapMode(args.mode) is BootstrapMode.DESKTOP_DEFAULT:
        bootstrap.open_ui()
    elif BootstrapMode(args.mode) is BootstrapMode.BACKGROUND_ONLY:
        logger.info("background-only mode active")
        return 0
    if args.no_tray:
        return 0
    return 0


def main() -> None:
    """Execute the main operation.

    Raises:
        Exception: Propagates validation or runtime failures from the underlying operation.
    """
    raise SystemExit(run())


if __name__ == "__main__":
    main()
