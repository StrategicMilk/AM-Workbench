"""AST indexing support for :mod:`vetinari.repo_map`."""

from __future__ import annotations

import ast
import hashlib
import json
import logging
from dataclasses import dataclass, field
from pathlib import Path

from vetinari.utils.serialization import dataclass_to_dict

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# AST Indexer
# ---------------------------------------------------------------------------


@dataclass
class SymbolInfo:
    """Information about a code symbol (class, function, variable)."""

    name: str
    kind: str  # "class", "function", "method", "variable", "import"
    file_path: str
    line_start: int
    line_end: int
    docstring: str = ""
    parent: str = ""  # parent class/function name
    decorators: list[str] = field(default_factory=list)

    def __repr__(self) -> str:
        """Show key identifying fields for debugging."""
        return f"SymbolInfo(name={self.name!r}, kind={self.kind!r}, file_path={self.file_path!r})"

    def to_dict(self) -> dict:
        """Serialize to a JSON-compatible dictionary."""
        return dataclass_to_dict(self)


@dataclass
class FileIndex:
    """Index of a single Python file."""

    file_path: str
    mtime: float
    size_bytes: int = 0
    content_hash: str = ""
    symbols: list[SymbolInfo] = field(default_factory=list)
    imports: list[str] = field(default_factory=list)
    # All Name identifiers referenced in the file body (function calls, attribute
    # accesses, variable usages).  Used by find_usages() to detect live call sites.
    name_refs: list[str] = field(default_factory=list)

    def __repr__(self) -> str:
        """Show key identifying fields for debugging."""
        return f"FileIndex(file_path={self.file_path!r}, symbols={len(self.symbols)!r})"

    def to_dict(self) -> dict:
        """Serialize this file index to a plain dictionary for JSON cache storage.

        Returns:
            Dictionary containing file path, modification time, symbols, imports,
            and name references.
        """
        return {
            "file_path": self.file_path,
            "mtime": self.mtime,
            "size_bytes": self.size_bytes,
            "content_hash": self.content_hash,
            "symbols": [s.to_dict() for s in self.symbols],
            "imports": self.imports,
            "name_refs": self.name_refs,
        }


class ASTIndexer:
    """AST-based Python code indexer.

    Parses Python files with the ast module to extract:
    - Classes and their methods
    - Top-level functions
    - Imports (what modules are used)
    - Docstrings

    Caches index to disk, invalidates on file mtime change.
    """

    CACHE_FILE = ".vetinari/ast_index.json"

    def __init__(self, root_path: str = "."):
        self._root = Path(root_path)
        self._index: dict[str, FileIndex] = {}
        self._symbol_table: dict[str, list[SymbolInfo]] = {}  # name -> locations
        self._loaded = False

    def index_project(self, force: bool = False) -> int:
        """Index all Python files in the project. Returns count of indexed files.

        Args:
            force: If True, re-index all files regardless of cached mtime.

        Returns:
            Number of files that were newly parsed and added to the index.
        """
        if not force:
            self._load_cache()
        else:
            self._index.clear()
            self._symbol_table.clear()

        python_files = sorted(self._iter_python_files(), key=lambda path: str(path))
        current_files = {str(path.relative_to(self._root)): path for path in python_files}
        for stale_path in sorted(set(self._index) - set(current_files)):
            del self._index[stale_path]

        indexed_count = 0
        for rel_path, py_file in current_files.items():
            mtime = py_file.stat().st_mtime
            try:
                data = py_file.read_bytes()
            except OSError:
                logger.warning("Could not read %s for symbol extraction - skipping file", py_file)
                continue
            content_hash = hashlib.sha256(data).hexdigest()
            size_bytes = len(data)

            cached = self._index.get(rel_path)
            if (
                not force
                and cached is not None
                and cached.mtime == mtime
                and cached.size_bytes == size_bytes
                and cached.content_hash == content_hash
            ):
                continue

            file_index = self._index_file(
                py_file,
                rel_path,
                mtime,
                source=data.decode("utf-8", errors="ignore"),
                size_bytes=size_bytes,
                content_hash=content_hash,
            )
            if file_index:
                self._index[rel_path] = file_index
                indexed_count += 1

        # Build symbol table
        self._build_symbol_table()
        self._loaded = True

        # Save cache
        self._save_cache()

        return indexed_count

    def _iter_python_files(self):
        """Iterate over Python files, skipping hidden dirs and venvs."""
        skip_dirs = {
            ".git",
            ".venv",
            "venv",
            "__pycache__",
            "node_modules",
            ".tox",
            ".eggs",
            "build",
            "dist",
        }
        for py_file in self._root.rglob("*.py"):
            parts = py_file.relative_to(self._root).parts
            if any(p in skip_dirs or p.startswith(".") for p in parts[:-1]):
                continue
            yield py_file

    def _index_file(
        self,
        file_path: Path,
        rel_path: str,
        mtime: float,
        *,
        source: str | None = None,
        size_bytes: int = 0,
        content_hash: str = "",
    ) -> FileIndex | None:
        """Index one source file into repo-map symbols."""
        try:
            if source is None:
                data = file_path.read_bytes()
                source = data.decode("utf-8", errors="ignore")
                size_bytes = len(data)
                content_hash = hashlib.sha256(data).hexdigest()
            tree = ast.parse(source, filename=str(file_path))
        except (SyntaxError, Exception):
            logger.warning("Could not parse %s for symbol extraction — skipping file", file_path)
            return None
        symbols = []
        imports = []
        for node in ast.walk(tree):
            if isinstance(node, ast.ClassDef):
                symbols.append(
                    SymbolInfo(
                        name=node.name,
                        kind="class",
                        file_path=rel_path,
                        line_start=node.lineno,
                        line_end=node.end_lineno or node.lineno,
                        docstring=ast.get_docstring(node) or "",
                        decorators=[self._decorator_name(d) for d in node.decorator_list],
                    ),
                )
                symbols.extend(
                    SymbolInfo(
                        name=item.name,
                        kind="method",
                        file_path=rel_path,
                        line_start=item.lineno,
                        line_end=item.end_lineno or item.lineno,
                        docstring=ast.get_docstring(item) or "",
                        parent=node.name,
                        decorators=[self._decorator_name(d) for d in item.decorator_list],
                    )
                    for item in node.body
                    if isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef))
                )
            elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                if not any(
                    isinstance(parent, ast.ClassDef)
                    for parent in ast.walk(tree)
                    if isinstance(getattr(parent, "body", None), list) and any(child is node for child in parent.body)
                ):
                    symbols.append(
                        SymbolInfo(
                            name=node.name,
                            kind="function",
                            file_path=rel_path,
                            line_start=node.lineno,
                            line_end=node.end_lineno or node.lineno,
                            docstring=ast.get_docstring(node) or "",
                            decorators=[self._decorator_name(d) for d in node.decorator_list],
                        ),
                    )
            elif isinstance(node, ast.Import):
                imports.extend(alias.name for alias in node.names)
            elif isinstance(node, ast.ImportFrom):
                if node.module:
                    imports.append(node.module)
        name_refs: list[str] = []
        for node in ast.walk(tree):
            if isinstance(node, ast.Name):
                name_refs.append(node.id)
            elif isinstance(node, ast.Attribute):
                name_refs.append(node.attr)
        return FileIndex(
            file_path=rel_path,
            mtime=mtime,
            size_bytes=size_bytes,
            content_hash=content_hash,
            symbols=symbols,
            imports=list(set(imports)),
            name_refs=list(set(name_refs)),
        )

    def _decorator_name(self, node) -> str:
        if isinstance(node, ast.Name):
            return node.id
        if isinstance(node, ast.Attribute):
            return f"{self._decorator_name(node.value)}.{node.attr}"
        if isinstance(node, ast.Call):
            return self._decorator_name(node.func)
        return ""

    def _build_symbol_table(self):
        """Build a reverse lookup: symbol name -> list of SymbolInfo."""
        self._symbol_table.clear()
        for file_index in self._index.values():
            for symbol in file_index.symbols:
                self._symbol_table.setdefault(symbol.name, []).append(symbol)

    def find_symbol(self, name: str) -> list[SymbolInfo]:
        """Find all definitions of a symbol by name.

        Args:
            name: The symbol name to look up (exact match).

        Returns:
            All SymbolInfo entries across the indexed codebase that have this
            name, or an empty list if the symbol was not found.
        """
        if not self._loaded and not self._index:
            self.index_project()
        return self._symbol_table.get(name, [])

    def find_usages(self, name: str) -> list[str]:
        """Find files that import or reference a symbol name.

        Args:
            name: The symbol name to search for across imports and docstrings.

        Returns:
            Sorted list of relative file paths that reference the symbol.
        """
        if not self._loaded and not self._index:
            self.index_project()
        files: set[str] = set()
        for file_path, file_index in self._index.items():
            # Check imports
            for imp in file_index.imports:
                if name in imp:
                    files.add(file_path)
            # Check symbol references (docstring mentions + parent class)
            for sym in file_index.symbols:
                if name in sym.docstring or name == sym.parent:
                    files.add(file_path)
            # Check live call/reference sites (Name and Attribute nodes in code body)
            if name in file_index.name_refs:
                files.add(file_path)
        return sorted(files)

    def get_file_symbols(self, file_path: str) -> list[SymbolInfo]:
        """Get all symbols in a specific file.

        Args:
            file_path: Relative path to the file (as stored in the index).

        Returns:
            All SymbolInfo entries extracted from the file, or an empty list
            if the file has not been indexed.
        """
        if not self._loaded and not self._index:
            self.index_project()
        fi = self._index.get(file_path)
        return fi.symbols if fi else []

    def get_import_graph(self) -> dict[str, list[str]]:
        """Return the intra-project import dependency graph.

        Returns:
            Dictionary mapping each indexed file path to the list of
            vetinari.* modules it imports directly.
        """
        if not self._loaded and not self._index:
            self.index_project()
        graph = {}
        for file_path, file_index in self._index.items():
            graph[file_path] = [imp for imp in file_index.imports if imp.startswith("vetinari")]
        return graph

    def get_stats(self) -> dict[str, int]:
        """Return summary statistics about the indexed codebase.

        Returns:
            Dictionary with counts of files, total symbols, classes, and functions.
        """
        return {
            "files_indexed": len(self._index),
            "total_symbols": sum(len(fi.symbols) for fi in self._index.values()),
            "total_classes": sum(1 for fi in self._index.values() for s in fi.symbols if s.kind == "class"),
            "total_functions": sum(
                1 for fi in self._index.values() for s in fi.symbols if s.kind in ("function", "method")
            ),
        }

    def _load_cache(self):
        cache_file = self._root / self.CACHE_FILE
        if cache_file.exists():
            try:
                data = json.loads(cache_file.read_text(encoding="utf-8"))
                if not isinstance(data, list):
                    raise ValueError("cache root must be a list")
                loaded_index: dict[str, FileIndex] = {}
                for entry in data:
                    if not isinstance(entry, dict):
                        raise ValueError("cache entry must be an object")
                    symbols = [SymbolInfo(**s) for s in entry.get("symbols", [])]
                    fi = FileIndex(
                        file_path=entry["file_path"],
                        mtime=entry["mtime"],
                        size_bytes=int(entry.get("size_bytes", 0) or 0),
                        content_hash=str(entry.get("content_hash", "")),
                        symbols=symbols,
                        imports=entry.get("imports", []),
                        name_refs=entry.get("name_refs", []),
                    )
                    loaded_index[fi.file_path] = fi
                self._index = loaded_index
                self._build_symbol_table()
                self._loaded = True
            except Exception as e:
                logger.warning("AST index cache load error: %s", e)
                self._index.clear()
                self._symbol_table.clear()
                self._loaded = False

    def _save_cache(self):
        try:
            cache_file = self._root / self.CACHE_FILE
            cache_file.parent.mkdir(parents=True, exist_ok=True)
            data = [fi.to_dict() for fi in self._index.values()]
            temp_file = cache_file.with_suffix(cache_file.suffix + ".tmp")
            temp_file.write_text(json.dumps(data), encoding="utf-8")
            temp_file.replace(cache_file)
        except Exception as e:
            logger.warning("AST index cache save error: %s", e)
