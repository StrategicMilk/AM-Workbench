"""CLI entry point for the Workflow Builder package.

Usage
-----
  python -m vetinari.workflows list
  python -m vetinari.workflows show <pipeline_id>
  python -m vetinari.workflows validate <pipeline_id>

Each subcommand exits with code 0 on success and 1 on failure so it can be
used in scripts and CI pipelines.
"""

from __future__ import annotations

import argparse
import logging
import sys

from vetinari.security.fail_closed import FailClosedError
from vetinari.workflows.builder import (
    list_pipelines,
    load_pipeline,
    validate_pipeline,
)

logger = logging.getLogger(__name__)


def _cmd_list(args: argparse.Namespace) -> int:
    """Print all stored pipeline IDs, one per line.

    Args:
        args: Parsed CLI namespace (unused; present for uniform signature).

    Returns:
        Exit code 0.
    """
    ids = list_pipelines()
    if not ids:
        print("No pipelines found.")
    else:
        for pipeline_id in ids:
            print(pipeline_id)
    return 0


def _cmd_show(args: argparse.Namespace) -> int:
    """Print a human-readable summary of a stored pipeline.

    Args:
        args: Parsed CLI namespace; ``args.pipeline_id`` is required.

    Returns:
        Exit code 0 on success, 1 if the pipeline is not found.
    """
    try:
        pipeline = load_pipeline(args.pipeline_id)
    except (FileNotFoundError, FailClosedError, ValueError) as exc:
        logger.warning("Workflow pipeline %s could not be loaded: %s", args.pipeline_id, exc)
        print(f"Error: {exc}", file=sys.stderr)
        return 1

    print(f"Pipeline ID : {pipeline.pipeline_id}")
    print(f"Name        : {pipeline.name}")
    print(f"Created     : {pipeline.created_at}")
    print(f"Updated     : {pipeline.updated_at}")
    print(f"Nodes ({len(pipeline.nodes)}):")
    for node in pipeline.nodes:
        print(f"  [{node.node_type}] {node.node_id}")
    print(f"Edges ({len(pipeline.edges)}):")
    for edge in pipeline.edges:
        cond = f" [if {edge.condition}]" if edge.condition else ""
        print(f"  {edge.from_node} -> {edge.to_node}{cond}")
    return 0


def _cmd_validate(args: argparse.Namespace) -> int:
    """Validate a stored pipeline and report any structural errors.

    Args:
        args: Parsed CLI namespace; ``args.pipeline_id`` is required.

    Returns:
        Exit code 0 if the pipeline is valid, 1 if it has errors or cannot be
        loaded.
    """
    try:
        pipeline = load_pipeline(args.pipeline_id)
    except (FileNotFoundError, FailClosedError, ValueError) as exc:
        logger.warning("Workflow pipeline %s could not be validated because it is missing: %s", args.pipeline_id, exc)
        print(f"Error: {exc}", file=sys.stderr)
        return 1

    errors = validate_pipeline(pipeline)
    if not errors:
        print(f"Pipeline '{args.pipeline_id}' is valid.")
        return 0

    print(f"Pipeline '{args.pipeline_id}' has {len(errors)} error(s):", file=sys.stderr)
    for error in errors:
        print(f"  - {error}", file=sys.stderr)
    return 1


def _build_parser() -> argparse.ArgumentParser:
    """Construct the argument parser for the workflows CLI.

    Returns:
        Configured :class:`argparse.ArgumentParser`.
    """
    parser = argparse.ArgumentParser(
        prog="python -m vetinari.workflows",
        description="Workflow Builder CLI — manage pipeline definitions.",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    subparsers.add_parser("list", help="List all stored pipeline IDs.")

    show_parser = subparsers.add_parser("show", help="Show details of a pipeline.")
    show_parser.add_argument("pipeline_id", help="The pipeline ID to display.")

    validate_parser = subparsers.add_parser("validate", help="Validate a pipeline graph.")
    validate_parser.add_argument("pipeline_id", help="The pipeline ID to validate.")

    return parser


def main(argv: list[str] | None = None) -> int:
    """Parse arguments and dispatch to the appropriate subcommand.

    Args:
        argv: Argument list; defaults to ``sys.argv[1:]`` when ``None``.

    Returns:
        Integer exit code.
    """
    parser = _build_parser()
    args = parser.parse_args(argv)

    dispatch = {
        "list": _cmd_list,
        "show": _cmd_show,
        "validate": _cmd_validate,
    }
    handler = dispatch[args.command]
    return handler(args)


if __name__ == "__main__":
    sys.exit(main())
