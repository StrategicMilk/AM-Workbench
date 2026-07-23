"""Repository structure indexer — provides codebase context to agents before task execution.

Inspired by Aider's repository mapping technique. Instead of sending entire
codebases to LLMs, generates a concise structural summary:
- Module names, class names, function signatures
- Imports and dependencies
- File relationships

Pipeline role:
    Intake → **RepoMap** (context enrichment) → Planning → Execution → Verify → Learn
    The Foreman calls RepoMap before plan generation so the planner understands
    the existing codebase structure, preventing hallucinated imports and
    duplicate implementations.  Reduces token usage by 60-80% compared to
    sending whole files.

Usage:
    from vetinari.repo_map import get_repo_map

    mapper = get_repo_map()
    summary = mapper.generate(root_path="/path/to/project", max_tokens=2000)
    # summary is a concise string representing the codebase structure
"""

from __future__ import annotations

import ast
import logging
import os
import threading
from dataclasses import dataclass, field
from pathlib import Path

from vetinari.repo_map_indexer import ASTIndexer
from vetinari.repo_map_indexer import FileIndex as FileIndex
from vetinari.repo_map_indexer import SymbolInfo as SymbolInfo
from vetinari.security.fail_closed import confine_to_root, sanitize_untrusted_text

logger = logging.getLogger(__name__)


# Files/dirs to always skip
_SKIP_DIRS = {
    "__pycache__",
    ".git",
    ".agents",
    ".ai-codex",
    ".claude",
    ".codex",
    ".venv",
    "venv",
    "env",
    "node_modules",
    "test-results",
    "dist",
    "build",
    ".pytest_cache",
    ".mypy_cache",
    ".ruff_cache",
    "*.egg-info",
    "model_cache",
    "vetinari_checkpoints",
    "logs",
    "outputs",
    "projects",
    "ui/static",
}
_SKIP_EXTENSIONS = {
    ".pyc",
    ".pyo",
    ".pyd",
    ".so",
    ".dll",
    ".exe",
    ".bin",
    ".png",
    ".jpg",
    ".jpeg",
    ".gif",
    ".svg",
    ".ico",
    ".pdf",
    ".zip",
    ".tar",
    ".gz",
    ".lock",
    ".min.js",
    ".min.css",
}
_SKIP_FILES = {"__pycache__", ".DS_Store", "Thumbs.db"}

# Hard cap on files parsed per scan — prevents multi-second hangs when
# generate_for_task is called with Path.cwd() on large monorepos.
# 300 files is ~10-15s of context at 2K tokens/map; more than enough for
# relevance scoring which only needs the top-20 anyway.
_MAX_SCAN_FILES = 300


def _should_skip_dir(name: str) -> bool:
    """Return True when a directory should be excluded from the repo map scan.

    Handles both the explicit skip-list and common virtualenv naming patterns
    like ``.venv312``, ``.venv3``, ``venv-py311``, etc. that users create
    alongside the canonical ``.venv`` and ``venv`` names.

    Args:
        name: The directory basename (not a full path).

    Returns:
        True if the scanner should skip this directory entirely.
    """
    if name in _SKIP_DIRS:
        return True
    # Match glob patterns in _SKIP_DIRS (e.g. "*.egg-info")
    if any(name.endswith(pat.lstrip("*")) for pat in _SKIP_DIRS if pat.startswith("*")):
        return True
    # Skip any directory that looks like a virtualenv regardless of suffix
    lower = name.lower()
    return lower.startswith((".venv", "venv", ".env"))


@dataclass
class ModuleInfo:
    """Structural information about a Python module."""

    path: str
    name: str
    classes: list[str] = field(default_factory=list)
    functions: list[str] = field(default_factory=list)
    imports: list[str] = field(default_factory=list)
    docstring: str = ""
    line_count: int = 0

    def __repr__(self) -> str:
        """Show key identifying fields for debugging."""
        return f"ModuleInfo(name={self.name!r}, classes={len(self.classes)!r}, functions={len(self.functions)!r})"


class RepoMap:
    """Generates compact structural maps of codebases for LLM consumption.

    The output is a text representation showing:
    - Module hierarchy
    - Class names with their methods
    - Top-level function signatures
    - Key imports

    Designed to give LLMs structural awareness in ~500-2000 tokens instead
    of 10,000+ tokens for raw file contents.
    """

    def __init__(self):
        self._cache: dict[str, str] = {}  # path -> cached map

    def generate(
        self,
        root_path: str,
        max_tokens: int = 2000,
        include_private: bool = False,
        focus_paths: list[str] | None = None,
    ) -> str:
        """Generate a repository structure map.

        Args:
            root_path: Root directory to map.
            max_tokens: Approximate token limit (~4 chars/token).
            include_private: Include private (_name) members.
            focus_paths: If provided, only map these specific paths/modules.

        Returns:
            A string representation of the repository structure.
        """
        max_chars = max_tokens * 4
        root = Path(root_path).resolve()

        if not root.exists():
            return f"[RepoMap] Path not found: {root_path}"

        modules = self._scan_directory(root, focus_paths, include_private)

        if not modules:
            return f"[RepoMap] No Python files found in: {root_path}"

        lines = [f"# Repository Structure: {root.name}", ""]
        chars_used = sum(len(line) + 1 for line in lines)

        # When focus_paths is given (e.g. from generate_for_task relevance sort),
        # preserve that caller-supplied order.  Otherwise fall back to alphabetic.
        if focus_paths:
            path_order = {p: i for i, p in enumerate(focus_paths)}
            ordered_modules = sorted(modules, key=lambda m: path_order.get(m.path, len(focus_paths)))
        else:
            ordered_modules = sorted(modules, key=lambda m: m.path)

        for idx, mod in enumerate(ordered_modules):
            mod_lines = self._format_module(mod, include_private)
            mod_str = "\n".join(mod_lines) + "\n"

            if chars_used + len(mod_str) > max_chars:
                remaining = max_chars - chars_used
                if remaining > 100:
                    lines.append(mod_str[:remaining] + "\n  [... truncated]")
                lines.append(f"\n[{len(modules) - idx} more modules not shown — token limit]")
                break

            lines.extend(mod_lines)
            lines.append("")
            chars_used += len(mod_str)

        return "\n".join(lines)

    def generate_for_task(
        self,
        root_path: str,
        task_description: str,
        max_tokens: int = 1500,
    ) -> str:
        """Generate a task-focused repo map that emphasises relevant modules.

        Uses keyword matching to prioritise files likely relevant to the task.

        Args:
            root_path: The root path.
            task_description: The task description.
            max_tokens: The max tokens.

        Returns:
            Compact text representation of the repository structure, with modules
            most relevant to the task listed first. Empty string if the root does
            not exist or contains no Python files.
        """
        root = Path(root_path).resolve()
        task_description = sanitize_untrusted_text(task_description, max_length=4_000)
        if not root.exists():
            return ""

        modules = self._scan_directory(root, None, False)
        if not modules:
            return ""

        # Score modules by relevance to task
        task_keywords = set(task_description.lower().split())
        scored = []
        for mod in modules:
            score = 0
            mod_text = (mod.name + " " + " ".join(mod.classes) + " " + " ".join(mod.functions)).lower()
            for kw in task_keywords:
                if kw in mod_text:
                    score += 1
            scored.append((score, mod))

        # Sort by relevance, then alphabetically
        scored.sort(key=lambda x: (-x[0], x[1].path))
        prioritised = [m for _, m in scored]

        return self.generate(root_path, max_tokens, False, [m.path for m in prioritised[:20]])

    def _scan_directory(
        self,
        root: Path,
        focus_paths: list[str] | None,
        include_private: bool,
        max_files: int = _MAX_SCAN_FILES,
    ) -> list[ModuleInfo]:
        """Scan directory and extract module information.

        Args:
            root: Directory to scan.
            focus_paths: Restrict scan to these paths when provided.
            include_private: Include private (_name) members.
            max_files: Hard cap on files parsed — prevents hanging on repos
                with thousands of Python files.

        Returns:
            List of :class:`ModuleInfo` for each parseable Python file found.
        """
        modules = []
        focus_set: set[str] | None = None
        focus_files: list[Path] | None = None
        if focus_paths:
            focus_set = set()
            focus_files = []
            unresolved_focus = False
            for item in focus_paths:
                safe_item = sanitize_untrusted_text(item, max_length=500)
                focus_path = Path(safe_item)
                if focus_path.is_absolute():
                    confine_to_root(root, safe_item)
                    candidate = focus_path
                else:
                    candidate = root / focus_path
                focus_set.add(safe_item)
                if candidate.is_file() and candidate.suffix == ".py":
                    focus_files.append(candidate)
                else:
                    unresolved_focus = True
            if unresolved_focus:
                focus_files = None
        parsed = 0

        python_files = focus_files if focus_files is not None else self._iter_python_files(root)
        for py_file in python_files:
            if focus_set and str(py_file) not in focus_set and py_file.name not in focus_set:
                # Check if the stem matches
                rel = str(py_file.relative_to(root))
                if rel not in focus_set and py_file.stem not in focus_set:
                    continue

            try:
                mod = self._parse_file(py_file, root)
                if mod:
                    modules.append(mod)
                    parsed += 1
                    if parsed >= max_files:
                        logger.debug(
                            "[RepoMap] Reached max_files=%d limit in %s — stopping scan early",
                            max_files,
                            root,
                        )
                        break
            except Exception as e:
                logger.warning("[RepoMap] Could not parse %s: %s", py_file, e)

        return modules

    def _iter_python_files(self, root: Path):
        """Yield Python files under root, skipping known non-source directories.

        Uses ``os.walk`` with in-place directory pruning so entire skip-listed
        subtrees (e.g. ``.venv312``, ``node_modules``) are never descended
        into.  ``rglob`` cannot prune mid-traversal, so it reads every file
        path under a large venv before the skip check fires.

        Args:
            root: Directory to traverse recursively.

        Yields:
            Path objects for each .py file not in a skip-list directory.
        """
        for dirpath, dirnames, filenames in os.walk(root):
            # Prune skip directories in-place so os.walk never descends into them.
            # Also skip any directory whose name looks like a virtualenv
            # (.venv, .venv312, venv, venv3, etc.) to handle non-standard names.
            rel_parts = Path(dirpath).relative_to(root).parts if Path(dirpath) != root else ()
            dirnames[:] = [
                d
                for d in dirnames
                if not _should_skip_dir(d) and not _should_skip_dir("/".join((*rel_parts, d)).replace("\\", "/"))
            ]
            for filename in filenames:
                if not filename.endswith(".py"):
                    continue
                if filename in _SKIP_FILES:
                    continue
                yield Path(dirpath) / filename

    def _parse_file(self, path: Path, root: Path) -> ModuleInfo | None:
        """Parse a Python file and extract structural information."""
        try:
            source = path.read_text(encoding="utf-8", errors="ignore")
            if len(source) > 100_000:  # Skip huge generated files
                return None

            tree = ast.parse(source, filename=str(path))
        except SyntaxError:
            logger.warning("Skipping %s — syntax error, cannot parse AST for repo map", path)
            return None
        except Exception:
            logger.warning(
                "Failed to parse Python file %s for repo map — file will be excluded from module index",
                path,
            )
            return None

        rel_path = str(path.relative_to(root))
        module_name = rel_path.replace(os.sep, ".").removesuffix(".py") if rel_path.endswith(".py") else rel_path

        mod = ModuleInfo(
            path=rel_path,
            name=module_name,
            line_count=len(source.splitlines()),
        )

        # Extract docstring
        if (
            isinstance(tree.body, list)
            and tree.body
            and isinstance(tree.body[0], ast.Expr)
            and isinstance(tree.body[0].value, ast.Constant)
            and isinstance(tree.body[0].value.value, str)
        ):
            mod.docstring = (tree.body[0].value.value or "")[:80]

        # Extract imports
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names[:2]:
                    mod.imports.append(alias.name.split(".")[0])
            elif isinstance(node, ast.ImportFrom) and node.module:
                mod.imports.append(node.module.split(".")[0])

        mod.imports = list(dict.fromkeys(mod.imports))[:8]

        # Extract classes and functions
        for node in tree.body:
            if isinstance(node, ast.ClassDef):
                mod.classes.append(self._format_class(node))
            elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                sig = self._format_function(node)
                if sig:
                    mod.functions.append(sig)

        return mod

    @staticmethod
    def _format_class(node: ast.ClassDef) -> str:
        """Format a class definition as a concise string."""
        methods = [item.name for item in node.body if isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef))]

        base_names = []
        for base in node.bases[:2]:
            if isinstance(base, ast.Name):
                base_names.append(base.id)
            elif isinstance(base, ast.Attribute):
                base_names.append(base.attr)

        bases = f"({', '.join(base_names)})" if base_names else ""
        method_str = f" [{', '.join(methods[:6])}{'...' if len(methods) > 6 else ''}]" if methods else ""
        return f"{node.name}{bases}{method_str}"

    @staticmethod
    def _format_function(node) -> str | None:
        """Format a function signature as a concise string."""
        args = [arg.arg for arg in node.args.args[:4]]
        if len(node.args.args) > 4:
            args.append("...")
        prefix = "async " if isinstance(node, ast.AsyncFunctionDef) else ""
        return f"{prefix}{node.name}({', '.join(args)})"

    @staticmethod
    def _format_module(mod: ModuleInfo, include_private: bool) -> list[str]:
        """Format module info as a list of lines."""
        lines = [f"## {mod.path}"]
        if mod.docstring:
            lines.append(f"  # {mod.docstring[:60]}")

        if mod.classes:
            visible = [c for c in mod.classes if include_private or not c.startswith("_")]
            if visible:
                lines.append(f"  classes: {', '.join(visible[:5])}")

        if mod.functions:
            visible = [f for f in mod.functions if include_private or not f.startswith("_")]
            if visible:
                lines.append(f"  functions: {', '.join(visible[:8])}")

        return lines


# ---------------------------------------------------------------------------
# Singleton
# ---------------------------------------------------------------------------

_repo_map: RepoMap | None = None
_repo_map_lock = threading.Lock()


def get_repo_map() -> RepoMap:
    """Return the process-global RepoMap instance, creating it on first call.

    Returns:
        The singleton RepoMap instance.
    """
    global _repo_map
    if _repo_map is None:
        with _repo_map_lock:
            if _repo_map is None:
                _repo_map = RepoMap()
    return _repo_map


# ---------------------------------------------------------------------------
# AST Indexer singleton
# ---------------------------------------------------------------------------


# Per-root cache: resolved root path -> ASTIndexer instance.
# Keyed by resolved absolute path string so different callers spelling the
# same path (relative vs absolute, trailing slash, etc.) share one indexer,
# while distinct project roots never share symbol tables.
_indexer_cache: dict[str, ASTIndexer] = {}
_indexer_lock = threading.Lock()


def get_ast_indexer(root_path: str = ".") -> ASTIndexer:
    """Return an ASTIndexer instance scoped to the given root path.

    Uses a per-root cache so that:
    - The same root always returns the same (warmed) instance.
    - Different roots each get their own isolated instance, preventing
      symbol tables from one project from leaking into another.

    Args:
        root_path: Root directory for the indexer.

    Returns:
        An ASTIndexer scoped to ``root_path``.
    """
    requested = str(Path(root_path).resolve())
    if requested not in _indexer_cache:
        with _indexer_lock:
            # Double-checked locking: re-check inside the lock.
            if requested not in _indexer_cache:
                _indexer_cache[requested] = ASTIndexer(root_path)
    return _indexer_cache[requested]
