"""CLI entry-point for the Workbench sub-package.

Invoked with ``python -m vetinari.workbench <command> [options]``.

Available commands:

* ``rank-models`` — benchmark all locally registered models and print a
  ranked recommendation table.
"""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import asdict


def _cmd_rank_models(args: argparse.Namespace) -> int:
    """Run benchmark ranking and print the result table.

    Args:
        args: Parsed command-line namespace.  Uses ``args.suite`` for the
            suite identifier and ``args.all`` to include models not currently
            loaded locally.

    Returns:
        Exit code: 0 on success, 1 if no models are registered.
    """
    from vetinari.workbench.model_benchmark import format_rank_table, rank_models

    results = rank_models(suite_id=args.suite, loaded_only=not args.all)
    if args.as_json:
        print(json.dumps({"results": [asdict(result) for result in results]}, indent=2, sort_keys=True))
        return 0 if results else 1
    if not results:
        print("No registered models found. Run `vetinari models scan` to discover local models.")
        return 1
    print(format_rank_table(results))
    return 0


def _build_parser() -> argparse.ArgumentParser:
    """Construct the top-level argument parser."""
    parser = argparse.ArgumentParser(
        prog="python -m vetinari.workbench",
        description="Vetinari Workbench CLI utilities",
    )
    subparsers = parser.add_subparsers(dest="command", metavar="<command>")
    subparsers.required = True

    # -- rank-models subcommand
    rank_parser = subparsers.add_parser(
        "rank-models",
        help="Benchmark all locally registered models and print a ranked recommendation table.",
    )
    rank_parser.add_argument(
        "--suite",
        default="default",
        metavar="SUITE_ID",
        help="Benchmark suite identifier (default: %(default)s).",
    )
    rank_parser.add_argument(
        "--all",
        action="store_true",
        dest="all",
        default=False,
        help="Include models not currently loaded locally (scored as 0.0 on missing-artifact prompts).",
    )
    rank_parser.add_argument(
        "--json",
        action="store_true",
        dest="as_json",
        help="Emit machine-readable JSON instead of a formatted table.",
    )
    rank_parser.set_defaults(func=_cmd_rank_models)

    return parser


def main(argv: list[str] | None = None) -> int:
    """Parse arguments and dispatch to the requested subcommand.

    Args:
        argv: Argument list, defaults to ``sys.argv[1:]`` when ``None``.

    Returns:
        Exit code suitable for ``sys.exit``.
    """
    parser = _build_parser()
    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
