"""CLI entry point for the evaluation subsystem.

Exposes ``python -m vetinari.evaluation <command>`` so operators can trigger
eval runs from the command line without starting the web server.

Supported commands:

* ``run-suite --suite SUITE_ID --model MODEL_ID``  — run a named eval suite
  against a specific model and print the resulting score + run_id.

This is step 2 of the pipeline:
  Operator CLI → **eval runner** → JSONL store → leaderboard view
"""

from __future__ import annotations

import argparse
import json
import sys


def _build_parser() -> argparse.ArgumentParser:
    """Build the argument parser for the evaluation CLI.

    Returns:
        Configured ArgumentParser instance.
    """
    parser = argparse.ArgumentParser(
        prog="python -m vetinari.evaluation",
        description="Vetinari evaluation CLI — run benchmark suites and view results.",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    run_suite = sub.add_parser(
        "run-suite",
        help="Run a named eval suite against a model and persist the result.",
    )
    run_suite.add_argument(
        "--suite",
        default="default",
        metavar="SUITE_ID",
        help="Eval suite to run (default: 'default').",
    )
    run_suite.add_argument(
        "--model",
        required=True,
        metavar="MODEL_ID",
        help="Model identifier to evaluate.",
    )
    run_suite.add_argument(
        "--json",
        action="store_true",
        dest="output_json",
        help="Emit the result record as JSON instead of human-readable text.",
    )

    return parser


def _cmd_run_suite(args: argparse.Namespace) -> int:
    """Execute the run-suite subcommand.

    Args:
        args: Parsed CLI arguments with ``suite``, ``model``, and ``output_json``.

    Returns:
        Exit code (0 = success, 1 = eval failed with recorded error).
    """
    from vetinari.evaluation.runner import run_eval

    record = run_eval(model_id=args.model, suite_id=args.suite)

    if args.output_json:
        from dataclasses import asdict

        print(json.dumps(asdict(record), ensure_ascii=False))  # noqa: T201 - CLI output to stdout is intentional
    else:
        status = "ERROR" if record.error else "OK"
        print(  # noqa: T201 - CLI output to stdout is intentional
            f"[{status}] run_id={record.run_id}  model={record.model_id}"
            f"  suite={record.suite_id}  score={record.score:.4f}"
            + (f"\nerror: {record.error}" if record.error else "")
        )

    return 1 if record.error else 0


def main(argv: list[str] | None = None) -> int:
    """Parse CLI arguments and dispatch to the appropriate sub-command.

    Args:
        argv: Argument list (defaults to ``sys.argv[1:]`` when None).

    Returns:
        Integer exit code suitable for ``sys.exit()``.
    """
    parser = _build_parser()
    args = parser.parse_args(argv)

    if args.command == "run-suite":
        return _cmd_run_suite(args)

    # Unreachable — argparse enforces required subcommand, but be defensive.
    parser.print_help()
    return 1


if __name__ == "__main__":
    sys.exit(main())
