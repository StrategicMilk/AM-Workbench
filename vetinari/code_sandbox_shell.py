"""Shell command preparation for :mod:`vetinari.code_sandbox`.

The public ``CodeSandbox.execute_shell()`` method owns subprocess execution.
This module keeps the deterministic allowlist and path-confinement checks
small and testable without widening sandbox behavior.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from vetinari.sandbox_policy import _ALLOWED_COMMANDS
from vetinari.sandbox_types import ExecutionResult

logger = logging.getLogger("vetinari.code_sandbox")


@dataclass(frozen=True, slots=True)
class PreparedShellCommand:
    """Validated shell command argv or a fail-closed rejection result."""

    parts: list[str]
    rejection: ExecutionResult | None = None


def prepare_shell_command(
    command: str,
    working_dir: Path,
    *,
    split_command: Callable[[str], list[str]],
) -> PreparedShellCommand:
    """Validate and split a sandbox shell command.

    Args:
        command: Command string to split into an argv list.
        working_dir: Sandbox root used for argument path confinement.
        split_command: Command splitter, passed in by ``CodeSandbox`` so its
            historical ``shlex`` module import remains observable.

    Returns:
        PreparedShellCommand with argv parts, or an ExecutionResult rejection
        when the command must fail closed before subprocess execution.
    """
    parts = split_command(command)
    if not parts:
        return PreparedShellCommand(
            parts=[],
            rejection=ExecutionResult(success=False, output="", error="Empty command", return_code=-1),
        )

    cmd_name = Path(parts[0]).name
    if cmd_name not in _ALLOWED_COMMANDS:
        logger.warning("Blocked shell command not in allowlist: %s", cmd_name)
        return PreparedShellCommand(
            parts=parts,
            rejection=ExecutionResult(
                success=False,
                output="",
                error=f"Command '{cmd_name}' not in sandbox allowlist",
                execution_time_ms=0,
                return_code=-1,
            ),
        )

    sandbox_root = working_dir.resolve()
    for arg in parts[1:]:
        arg_path = Path(arg)
        if arg_path.is_absolute() or (len(arg_path.parts) > 1 and not arg.startswith("-")):
            try:
                resolved_arg = arg_path.resolve() if arg_path.is_absolute() else (working_dir / arg_path).resolve()
            except OSError:
                resolved_arg = arg_path
            if not resolved_arg.is_relative_to(sandbox_root):
                logger.warning(
                    "Blocked shell command '%s' - argument '%s' resolves outside sandbox root %s",
                    cmd_name,
                    arg,
                    sandbox_root,
                )
                return PreparedShellCommand(
                    parts=parts,
                    rejection=ExecutionResult(
                        success=False,
                        output="",
                        error=f"Argument '{arg}' points outside the sandbox root - access denied",
                        execution_time_ms=0,
                        return_code=-1,
                    ),
                )

    return PreparedShellCommand(parts=parts)
