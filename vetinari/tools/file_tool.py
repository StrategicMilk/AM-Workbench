"""File Operations Tool.

====================
Provides safe, sandboxed file operations for Vetinari agents.

All operations are restricted to a configurable project root to prevent
path traversal attacks.  Agents use this tool instead of raw ``os``/``pathlib``
calls so that every I/O operation is auditable and permission-gated.
"""

from __future__ import annotations

import logging
import shutil
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from vetinari.execution_context import ToolPermission, check_permission_unified, get_context_manager
from vetinari.learning.atomic_writers import _write_text_atomic
from vetinari.security.sandbox import RustSandboxAuthorityBridge, enforce_blocked_paths
from vetinari.tool_interface import (
    Tool,
    ToolCategory,
    ToolMetadata,
    ToolParameter,
    ToolResult,
)
from vetinari.types import AgentType
from vetinari.utils.serialization import dataclass_to_dict

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _safe_resolve(path: str, root: Path, permission: str = "read") -> Path:
    """Resolve *path* and ensure it stays inside *root*.

    Raises ``PermissionError`` on traversal attempts.
    """
    return RustSandboxAuthorityBridge(root).require_path(permission, path)


@dataclass(frozen=True, slots=True)
class FileInfo:
    """Metadata about a single file or directory."""

    path: str
    name: str
    is_file: bool
    is_dir: bool
    size_bytes: int = 0
    modified: str = ""
    created: str = ""

    def __repr__(self) -> str:
        """Show key identifying fields for debugging."""
        return f"FileInfo(name={self.name!r}, is_file={self.is_file!r}, size_bytes={self.size_bytes!r})"

    def to_dict(self) -> dict[str, Any]:
        """Serialize to a JSON-compatible dictionary."""
        return dataclass_to_dict(self)


# ---------------------------------------------------------------------------
# Core operations (stateless, testable)
# ---------------------------------------------------------------------------


class FileOperations:
    """Low-level file operations scoped to a project root."""

    def __init__(self, project_root: str | Path):
        if not project_root:
            raise ValueError(
                "project_root is required — process cwd is not a safe default for sandboxed file operations"
            )
        self.root = Path(project_root).resolve()

    # -- read ---------------------------------------------------------------

    def read_file(self, path: str, encoding: str = "utf-8") -> str:
        """Read and return the text content of *path*.

        Args:
            path: Path to the file, relative to the project root.
            encoding: Text encoding to use when reading the file.

        Returns:
            Full text content of the file.

        Raises:
            FileNotFoundError: If *path* does not point to a regular file.
            PermissionError: If *path* resolves outside the project root.
        """
        target = _safe_resolve(path, self.root, "read")
        if not target.is_file():
            raise FileNotFoundError(f"Not a file: {path}")
        return target.read_text(encoding=encoding)

    def file_exists(self, path: str) -> bool:
        """Check whether *path* exists inside the project root.

        Args:
            path: Path to check, relative to the project root.

        Returns:
            True if the path exists (file or directory), False otherwise.
        """
        target = _safe_resolve(path, self.root, "exists")
        return target.exists()

    def get_file_info(self, path: str) -> FileInfo:
        """Return metadata about the file or directory at *path*.

        Args:
            path: Path to inspect, relative to the project root.

        Returns:
            FileInfo populated with name, type flags, size, and ISO-8601
            modification/creation timestamps.

        Raises:
            FileNotFoundError: If *path* does not exist.
            PermissionError: If *path* resolves outside the project root.
        """
        target = _safe_resolve(path, self.root, "info")
        if not target.exists():
            raise FileNotFoundError(f"Path does not exist: {path}")
        st = target.stat(follow_symlinks=False)
        return FileInfo(
            path=str(target.relative_to(self.root)),
            name=target.name,
            is_file=target.is_file() and not target.is_symlink(),
            is_dir=target.is_dir() and not target.is_symlink(),
            size_bytes=st.st_size,
            modified=datetime.fromtimestamp(st.st_mtime, tz=timezone.utc).isoformat(),
            created=datetime.fromtimestamp(st.st_ctime, tz=timezone.utc).isoformat(),
        )

    def list_directory(self, path: str = ".") -> list[FileInfo]:
        """List the entries in a directory, sorted by name.

        Args:
            path: Directory path relative to the project root. Defaults to
                the project root itself.

        Returns:
            FileInfo for each child entry, sorted alphabetically. Inaccessible
            entries (e.g. broken symlinks) are silently skipped.

        Raises:
            NotADirectoryError: If *path* is not a directory.
            PermissionError: If *path* resolves outside the project root.
        """
        target = _safe_resolve(path, self.root, "list")
        if not target.is_dir():
            raise NotADirectoryError(f"Not a directory: {path}")
        entries: list[FileInfo] = []
        for child in sorted(target.iterdir()):
            try:
                relative_child = child.relative_to(self.root)
                RustSandboxAuthorityBridge(self.root).require_path("info", relative_child)
                st = child.stat(follow_symlinks=False)
                entries.append(
                    FileInfo(
                        path=str(relative_child),
                        name=child.name,
                        is_file=child.is_file() and not child.is_symlink(),
                        is_dir=child.is_dir() and not child.is_symlink(),
                        size_bytes=st.st_size,
                        modified=datetime.fromtimestamp(st.st_mtime, tz=timezone.utc).isoformat(),
                        created=datetime.fromtimestamp(st.st_ctime, tz=timezone.utc).isoformat(),
                    ),
                )
            except (OSError, PermissionError):
                logging.getLogger(__name__).debug("skipping inaccessible directory entry", exc_info=True)
        return entries

    # -- write --------------------------------------------------------------

    def write_file(self, path: str, content: str, encoding: str = "utf-8") -> str:
        """Write *content* to *path*, creating parent dirs as needed.

        Args:
            path: Destination path relative to the project root.
            content: Text to write to the file.
            encoding: Text encoding to use when writing the file.

        Returns:
            The written file's path relative to the project root.

        Raises:
            SandboxPolicyViolation: If the resolved target is on a sandbox
                policy ``blocked_paths`` entry.
        """
        target = _safe_resolve(path, self.root, "write")
        enforce_blocked_paths(target)
        _write_text_atomic(target, content, encoding=encoding)
        return str(target.relative_to(self.root))

    def create_directory(self, path: str) -> str:
        """Create a directory (and any missing parents) inside the project root.

        Args:
            path: Directory path to create, relative to the project root.

        Returns:
            The created directory's path relative to the project root.

        Raises:
            SandboxPolicyViolation: If the resolved target is on a sandbox
                policy ``blocked_paths`` entry.
        """
        target = _safe_resolve(path, self.root, "mkdir")
        enforce_blocked_paths(target)
        target.mkdir(parents=True, exist_ok=True)
        return str(target.relative_to(self.root))

    def move_file(self, src: str, dst: str) -> str:
        """Move or rename a file or directory within the project root.

        Args:
            src: Source path relative to the project root.
            dst: Destination path relative to the project root. Parent
                directories are created automatically.

        Returns:
            The destination path relative to the project root.

        Raises:
            FileNotFoundError: If *src* does not exist.
            PermissionError: If either path resolves outside the project root.
            SandboxPolicyViolation: If either endpoint is on a sandbox policy
                ``blocked_paths`` entry. Moving INTO a blocked path is creation
                there; moving a blocked source is removal of a blocked file —
                both are mutations the policy is meant to prevent.
        """
        src_path = _safe_resolve(src, self.root, "move")
        dst_path = _safe_resolve(dst, self.root, "move")
        if not src_path.exists():
            raise FileNotFoundError(f"Source does not exist: {src}")
        enforce_blocked_paths(dst_path)
        enforce_blocked_paths(src_path)
        dst_path.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(src_path), str(dst_path))
        return str(dst_path.relative_to(self.root))

    def delete_file(self, path: str) -> bool:
        """Delete a file or directory tree at *path*.

        Args:
            path: Path to delete, relative to the project root.

        Returns:
            True if the path existed and was deleted; False if it did not exist.

        Raises:
            SandboxPolicyViolation: If the resolved target is on a sandbox
                policy ``blocked_paths`` entry.
        """
        target = _safe_resolve(path, self.root, "delete")
        if not target.exists():
            return False
        enforce_blocked_paths(target)
        if target.is_dir():
            shutil.rmtree(target)
        else:
            target.unlink()
        return True


# ---------------------------------------------------------------------------
# Tool wrapper (for ToolRegistry integration)
# ---------------------------------------------------------------------------


class FileOperationsTool(Tool):
    """Vetinari Tool wrapper around :class:`FileOperations`."""

    def __init__(self, project_root: str):
        self._ops = FileOperations(project_root)
        metadata = ToolMetadata(
            name="file_operations",
            description="Safe file I/O restricted to the project directory",
            version="1.0.0",
            category=ToolCategory.FILE_OPERATIONS,
            required_permissions=[ToolPermission.FILE_READ],
            parameters=[
                ToolParameter(
                    name="operation",
                    type=str,
                    description="Operation: read, write, list, info, exists, mkdir, move, delete",
                    required=True,
                ),
                ToolParameter(
                    name="path",
                    type=str,
                    description="Target path (relative to project root)",
                    required=True,
                ),
                ToolParameter(name="content", type=str, description="File content (for write)", required=False),
                ToolParameter(name="destination", type=str, description="Destination path (for move)", required=False),
            ],
        )
        super().__init__(metadata)

    @staticmethod
    def _required_permission_for_operation(operation: str) -> ToolPermission | None:
        """Map a file operation to the permission it truly requires."""
        if operation in {"read", "list", "info", "exists"}:
            return ToolPermission.FILE_READ
        if operation in {"write", "mkdir", "move"}:
            return ToolPermission.FILE_WRITE
        if operation == "delete":
            return ToolPermission.FILE_DELETE
        return None

    @staticmethod
    def _policy_action_for_operation(operation: str) -> str | None:
        """Map a file operation to the policy-enforcer action string."""
        return {
            "read": "read",
            "list": "read",
            "info": "read",
            "exists": "read",
            "write": "write",
            "mkdir": "write",
            "move": "write",
            "delete": "delete",
        }.get(operation)

    def run(self, agent_type: AgentType | None = None, **kwargs) -> ToolResult:
        """Enforce per-operation agent permissions before the generic Tool.run path.

        FileOperationsTool metadata must stay broad enough to advertise the tool,
        but the actual authorization boundary depends on the requested operation.

        Returns:
            The delegated ``ToolResult`` when authorization succeeds, or a
            permission-denied result when the caller lacks the required access.
        """
        operation = str(kwargs.get("operation", ""))
        permission = self._required_permission_for_operation(operation)
        action = self._policy_action_for_operation(operation)
        if (
            agent_type is not None
            and permission is not None
            and not check_permission_unified(
                agent_type,
                permission,
                action=action,
                target=str(kwargs.get("path", "")),
            )
        ):
            return ToolResult(
                success=False,
                output=None,
                error=f"Permission {permission.value} denied for agent {agent_type.value}",
            )
        return super().run(agent_type=agent_type, **kwargs)

    def execute(self, **kwargs) -> ToolResult:
        """Dispatch the requested file operation and return the outcome.

        Args:
            **kwargs: Must include 'operation' and 'path'; 'content' is
                required for write, 'destination' for move.

        Returns:
            ToolResult with success=True and operation output on success,
            or success=False with an error message on failure.
        """
        op = kwargs.get("operation", "")
        path = kwargs.get("path", ".")

        # Enforce granular permissions per operation class.
        # Read-only ops (read, list, info, exists) require FILE_READ — they work in
        # PLANNING mode and any mode that grants FILE_READ.
        # Write/mutation ops require FILE_WRITE or FILE_DELETE on top of FILE_READ.
        _ctx = get_context_manager()
        if op in ("read", "list", "info", "exists"):
            _ctx.enforce_permission(ToolPermission.FILE_READ, f"file_{op}")
        elif op in ("write", "mkdir", "move"):
            _ctx.enforce_permission(ToolPermission.FILE_WRITE, f"file_{op}")
        elif op == "delete":
            _ctx.enforce_permission(ToolPermission.FILE_DELETE, "file_delete")

        try:
            if op == "read":
                content = self._ops.read_file(path)
                return ToolResult(success=True, output=content)

            if op == "write":
                content = kwargs.get("content", "")
                rel = self._ops.write_file(path, content)
                return ToolResult(success=True, output=f"Written to {rel}")

            if op == "list":
                entries = self._ops.list_directory(path)
                return ToolResult(success=True, output=[e.to_dict() for e in entries])

            if op == "info":
                info = self._ops.get_file_info(path)
                return ToolResult(success=True, output=info.to_dict())

            if op == "exists":
                return ToolResult(success=True, output=self._ops.file_exists(path))

            if op == "mkdir":
                rel = self._ops.create_directory(path)
                return ToolResult(success=True, output=f"Created {rel}")

            if op == "move":
                dst = kwargs.get("destination", "")
                if not dst:
                    return ToolResult(success=False, output="", error="destination is required for move")
                rel = self._ops.move_file(path, dst)
                return ToolResult(success=True, output=f"Moved to {rel}")

            if op == "delete":
                deleted = self._ops.delete_file(path)
                if not deleted:
                    return ToolResult(
                        success=False,
                        output="",
                        error=f"Path does not exist: {path}",
                    )
                return ToolResult(success=True, output="Deleted")

            return ToolResult(success=False, output="", error=f"Unknown operation: {op}")

        except PermissionError as exc:
            logger.warning("Caught PermissionError in except block: %s", exc)
            return ToolResult(success=False, output="", error=str(exc))
        except FileNotFoundError as exc:
            logger.warning("Caught FileNotFoundError in except block: %s", exc)
            return ToolResult(success=False, output="", error=str(exc))
        except Exception as exc:
            logger.exception("FileOperationsTool error")
            return ToolResult(success=False, output="", error=str(exc))
