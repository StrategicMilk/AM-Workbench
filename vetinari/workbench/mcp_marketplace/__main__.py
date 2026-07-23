"""CLI entry point for the Extension Marketplace install pipeline.

Invoked via::

    python -m vetinari.workbench.mcp_marketplace install <extension_id> [--dry-run]

This module is the thin CLI shell — all business logic lives in
:mod:`vetinari.workbench.mcp_marketplace.install`.
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from dataclasses import asdict

from vetinari.workbench.mcp_marketplace.install import (
    ExtensionInstallError,
    install_extension,
)

logger = logging.getLogger(__name__)


def _build_parser() -> argparse.ArgumentParser:
    """Build the argument parser for the marketplace CLI.

    Returns:
        Configured :class:`argparse.ArgumentParser` with the ``install``
        sub-command wired to :func:`~vetinari.workbench.mcp_marketplace.install.install_extension`.
    """
    parser = argparse.ArgumentParser(
        prog="python -m vetinari.workbench.mcp_marketplace",
        description="Extension Marketplace — catalog lookup, admission check, registration.",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    install_cmd = sub.add_parser(
        "install",
        help="Install an extension from the marketplace catalog.",
    )
    install_cmd.add_argument(
        "extension_id",
        help="Marketplace identifier of the extension to install (e.g. 'my-tool-v1').",
    )
    install_cmd.add_argument(
        "--dry-run",
        action="store_true",
        default=False,
        help=(
            "Run catalog lookup and admission check without registering the extension. "
            "Exits 0 on success, non-zero on failure."
        ),
    )
    install_cmd.add_argument("--oauth-code", help="OAuth authorization code to exchange before install probing.")
    install_cmd.add_argument("--oauth-redirect-uri", help="Redirect URI used for the OAuth authorization request.")
    install_cmd.add_argument("--oauth-client-id", help="OAuth public client identifier.")
    install_cmd.add_argument("--oauth-code-verifier", help="PKCE code verifier used for the authorization request.")
    install_cmd.add_argument("--json", action="store_true", dest="as_json", help="Emit machine-readable JSON.")
    return parser


def main(argv: list[str] | None = None) -> int:
    """Execute the marketplace CLI and return an exit code.

    Args:
        argv: Argument list (defaults to :data:`sys.argv[1:]`).

    Returns:
        Exit code: ``0`` on success, ``1`` on known install errors.
    """
    parser = _build_parser()
    args = parser.parse_args(argv)

    if args.command == "install":
        try:
            result = install_extension(
                args.extension_id,
                dry_run=args.dry_run,
                oauth_code=args.oauth_code,
                oauth_redirect_uri=args.oauth_redirect_uri,
                oauth_client_id=args.oauth_client_id,
                oauth_code_verifier=args.oauth_code_verifier,
            )
        except ExtensionInstallError as exc:
            logger.warning("Extension install failed for %r: %s", args.extension_id, exc)
            if args.as_json:
                sys.stdout.write(
                    json.dumps(
                        {"extension_id": args.extension_id, "status": "error", "error": str(exc)},
                        sort_keys=True,
                    )
                    + "\n"
                )
                return 1
            print(f"error: {exc}", file=sys.stderr)  # noqa: T201 — CLI output
            return 1

        if args.as_json:
            sys.stdout.write(json.dumps(asdict(result), indent=2, sort_keys=True) + "\n")
            return 0

        label = "dry-run" if args.dry_run else "installed"
        print(  # noqa: T201 — CLI output
            f"{label}: {result.extension_id}"
            + (f"  (license: {result.license_id})" if result.license_id else "")
            + ("  (oauth: authorized)" if result.oauth_authorized else "")
        )
        return 0

    # Unreachable — argparse requires a sub-command.
    parser.print_help()
    return 1


if __name__ == "__main__":
    sys.exit(main())
