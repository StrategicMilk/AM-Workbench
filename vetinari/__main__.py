"""Entry points for ``python -m vetinari`` and the installed ``vetinari`` script.

Two top-level "special" pre-empt flags handle one-shot ops modes that exit
without entering the main CLI: ``--scan-models`` (Pack A) and ``--scrape``
(Pack L). When neither is present, control passes to ``vetinari.cli.main``
unchanged. The pre-empt flags are mutually exclusive — combining them is a
usage error reported by argparse with exit code 2.
"""

from __future__ import annotations

import argparse
import dataclasses
import json
import logging
import sys
from collections.abc import Sequence

import vetinari

logger = logging.getLogger(__name__)


_SCRAPE_CACHE_POLICIES = ("default", "bypass", "no_store")


def _register_hardening() -> None:
    """Activate scraping hardening hooks for CLI and agent-toolkit paths."""
    import importlib

    register_module = importlib.import_module("vetinari.scraping._register_hardening")
    register_module.register_extensions()


def _build_special_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m vetinari",
        description="Vetinari special pre-empt entry points (scan models, scrape URL).",
        add_help=False,
    )
    group = parser.add_mutually_exclusive_group()
    group.add_argument(
        "--scan-models",
        action="store_true",
        help="Run the model scan and write outputs/models.jsonl, then exit.",
    )
    group.add_argument(
        "--scrape",
        metavar="URL",
        help="Fetch URL via the in-tree hardened scraper and print JSON, then exit.",
    )
    parser.add_argument(
        "--scrape-cache-policy",
        default="default",
        choices=list(_SCRAPE_CACHE_POLICIES),
        help="Cache policy for --scrape (default: use cache; bypass: read-through; no_store: do not write).",
    )
    parser.add_argument(
        "--special-help",
        action="store_true",
        help="Show help for the special pre-empt flags only and exit.",
    )
    return parser


def _run_scan_models() -> int:
    """Run the lightweight model scan entry point for ``python -m vetinari``."""
    from vetinari.models.scan import ModelScanError, configured_model_paths, scan, write_scan_jsonl

    try:
        records = scan(configured_model_paths())
    except ModelScanError as exc:
        logger.warning("Model scan entry point failed: %s", exc)
        print(str(exc), file=sys.stderr)
        return 2
    write_scan_jsonl(records)
    for record in records:
        print(f"{record.model_id} {record.size_bytes} {record.sha256[:12]} {record.format.value}")
    return 0


def _run_scrape(url: str, *, cache_policy: str) -> int:
    """Run the hardened scraper entry point for ``python -m vetinari``."""
    import importlib

    from vetinari.scraping.contracts import ScrapeFailureReason, ScrapeRequest

    _register_hardening()
    _disp_mod = importlib.import_module("vetinari.scraping.dispatcher")

    req = ScrapeRequest(url=url, cache_policy=cache_policy)
    result = _disp_mod.default_dispatcher().fetch(req)
    print(json.dumps(dataclasses.asdict(result), indent=2, default=str))
    if result.passed:
        return 0
    if result.reason.value == ScrapeFailureReason.URL_BLOCKED.value:
        detail = f": {result.error_detail}" if result.error_detail else ""
        print(f"URL blocked by SSRF policy{detail}", file=sys.stderr)
    else:
        print(f"Scrape failed: {result.reason.value}", file=sys.stderr)
    return 1


def _run_install() -> int:
    """Run the guided install wizard entry point for ``python -m vetinari install``."""
    from vetinari.setup.install_wizard import install_wizard_main

    return install_wizard_main()


def _run_community() -> int:
    """Print community resource URLs for ``python -m vetinari community``."""
    from vetinari.community import print_community_resources

    print_community_resources()
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    """Bootstrap environment and dispatch to the CLI entry point.

    Returns:
        Process exit code for the selected CLI path.
    """
    vetinari.bootstrap_environment()
    _register_hardening()
    args = list(sys.argv[1:] if argv is None else argv)

    # Pre-empt "install" and "community" before the special-flag parser so they
    # work as positional subcommands: ``python -m vetinari install`` and
    # ``python -m vetinari community``.
    if args and args[0] == "install":
        return _run_install()

    if args and args[0] == "community":
        return _run_community()

    parser = _build_special_parser()
    special, remaining = parser.parse_known_args(args)

    if special.special_help:
        parser.print_help()
        return 0

    if special.scan_models:
        return _run_scan_models()

    if special.scrape:
        return _run_scrape(special.scrape, cache_policy=special.scrape_cache_policy)

    if args == ["--help"]:
        from vetinari.cli import _build_parser

        _build_parser().print_help()
        print("  vetinari --scan-models")
        print("  vetinari --scrape https://example.com/")
        return 0

    sys.argv = [sys.argv[0], *remaining] if argv is None else [sys.argv[0], *args]
    from vetinari.cli import main as cli_main

    cli_main()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
