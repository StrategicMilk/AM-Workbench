"""Sandbox executor compatibility facade."""

from __future__ import annotations

import contextlib
import logging
import subprocess
from pathlib import Path
from typing import Any

import psutil

from vetinari.security.fail_closed import PathTraversalError, confine_to_root, sanitize_untrusted_text

logger = logging.getLogger(__name__)


class SandboxExecutor:
    """Bounded subprocess executor with best-effort child cleanup."""

    def __init__(self, *, timeout_seconds: float = 30.0, cwd: str | None = None) -> None:
        self.timeout_seconds = timeout_seconds
        self.cwd = str(confine_to_root(Path.cwd(), cwd)) if cwd is not None else None

    def execute(self, command: list[str]) -> dict[str, Any]:
        """Run *command* without a shell and clean up on timeout or abnormal exit.

        Args:
            command: Command argv.

        Returns:
            Execution result mapping.
        """
        try:
            sanitized_command = [sanitize_untrusted_text(part, max_length=4096) for part in command]
        except (TypeError, PathTraversalError, ValueError) as exc:
            logger.warning("Rejected unsafe sandbox command argv: %s", exc)
            if "empty" in str(exc).lower():
                return {"command": command, "returncode": 2, "stdout": "", "stderr": "command argv must be non-empty"}
            return {"command": command, "returncode": 2, "stdout": "", "stderr": "command argv must be safe text"}

        if not sanitized_command or not all(sanitized_command):
            return {"command": command, "returncode": 2, "stdout": "", "stderr": "command argv must be non-empty"}

        process: psutil.Popen | None = None
        creationflags = subprocess.CREATE_NEW_PROCESS_GROUP if hasattr(subprocess, "CREATE_NEW_PROCESS_GROUP") else 0
        try:
            process = psutil.Popen(
                sanitized_command,
                cwd=self.cwd,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                shell=False,
                creationflags=creationflags,
            )
            stdout, stderr = process.communicate(timeout=self.timeout_seconds)
            return {"command": command, "returncode": process.returncode, "stdout": stdout, "stderr": stderr}
        except (psutil.TimeoutExpired, subprocess.TimeoutExpired):
            if process is not None:
                _terminate_process(process)
                stdout, stderr = process.communicate()
                return {
                    "command": command,
                    "returncode": process.returncode if process.returncode is not None else -9,
                    "stdout": stdout,
                    "stderr": stderr,
                    "timed_out": True,
                }
            logger.warning("Sandbox subprocess timed out before process handle was available for %s", command)
            return {
                "command": command,
                "returncode": -9,
                "stdout": "",
                "stderr": "process timed out",
                "timed_out": True,
            }
        except OSError as exc:
            logger.warning("Sandbox subprocess launch failed for %s", command, exc_info=True)
            return {"command": sanitized_command, "returncode": 127, "stdout": "", "stderr": str(exc)}
        finally:
            if process is not None and process.poll() is None:
                _terminate_process(process)


def _terminate_process(process: psutil.Popen) -> None:
    """Terminate a subprocess and any currently attached descendants."""
    process_tree: list[psutil.Process] = []
    with contextlib.suppress(psutil.Error):
        process_tree.extend(process.children(recursive=True))
    process_tree.append(process)

    for proc in reversed(process_tree):
        with contextlib.suppress(psutil.Error):
            proc.terminate()
    _, alive = psutil.wait_procs(process_tree, timeout=5)
    for proc in alive:
        with contextlib.suppress(psutil.Error):
            proc.kill()
    if alive:
        psutil.wait_procs(alive, timeout=5)


__all__ = ["SandboxExecutor"]
