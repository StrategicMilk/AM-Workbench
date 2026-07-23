r"""Vetinari RAG Knowledge Base.

Vector-backed knowledge base for Retrieval-Augmented Generation using
SQLite + FTS5 + sqlite-vec for vector search.

Agents query this to get relevant context before execution:
- Project documentation
- Past successful outputs
- Code patterns and templates
- Error resolution guides
- LLM best practices

Architecture
------------
- SQLite + FTS5 for full-text search (always available)
- sqlite-vec for fast KNN vector search (optional, graceful fallback)
- Embeddings via OpenAI-compatible ``/v1/embeddings`` endpoint (shared with UnifiedMemoryStore)
- Automatic document ingestion from project docs/ directory
- Context-window-aware retrieval (returns only what fits)

Decision: sqlite-vec replaces ChromaDB for unified SQLite storage (ADR-0063)

Usage::

    from vetinari.rag.knowledge_base import get_knowledge_base

    kb = get_knowledge_base()
    kb.ingest_directory("docs/")

    results = kb.query(
        "How do I implement exponential backoff?",
        k=5,
        max_chars=3000,
    )
    context = "\n---\n".join(r.content for r in results)
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import logging
import os
import sqlite3
import threading
from dataclasses import replace
from pathlib import Path
from typing import Any

from vetinari.boundary_guards import require_nonempty
from vetinari.constants import DATABASE_BUSY_TIMEOUT_MS
from vetinari.database import get_connection
from vetinari.documents.extraction import extract_document_text
from vetinari.privacy.envelope import PRIVACY_ENVELOPE_KEY, extract_privacy_envelope, privacy_receipt
from vetinari.rag.knowledge_base_helpers import (
    _EMBEDDING_DIMENSIONS,
    _MAX_DOC_CHARS,
    KBDocument,
    load_sqlite_vec,
    pack_embedding,
)
from vetinari.rag.knowledge_base_helpers import (
    kb_embed as _embed,  # aliased as _embed so tests can patch vetinari.rag.knowledge_base._embed
)
from vetinari.rag.knowledge_base_search import KnowledgeBaseSearch
from vetinari.rag.knowledge_base_stats import build_knowledge_base_stats
from vetinari.rag.knowledge_base_url_ingest import (
    fetch_url_bytes as _fetch_url_bytes,
)
from vetinari.rag.knowledge_base_url_ingest import (
    ingest_url_documents,
)
from vetinari.workbench.effective_config import capture_retrieval_config_snapshot

logger = logging.getLogger(__name__)


BOUNDARY_ADR = "ADR-0132"
CANONICAL_BOUNDARY = "knowledge.retrieval"
_MAX_DIRECTORY_FILES = 1000
_MAX_DIRECTORY_BYTES = 10_000_000
_MAX_DIRECTORY_CHUNKS = 5000
_MAX_URL_CHUNKS = 500
_REQUIRE_HTTPS_URL_INGEST = os.environ.get("VETINARI_RAG_REQUIRE_HTTPS", "true").lower() in {"1", "true", "yes"}


# ── Injection guard ──────────────────────────────────────────────────────


class KnowledgeBasePrivacyError(ValueError):
    """Raised when persisted RAG rows lack valid privacy receipts."""


def _document_privacy_receipt(*, source: str, category: str) -> dict[str, Any]:
    return privacy_receipt(
        privacy_class="operational",
        retention_days=30,
        source="knowledge_base.documents",
        erasure_token=f"knowledge_base:{source}:{category}",
    )


def _privacy_receipt_json(envelope: dict[str, Any]) -> str:
    return json.dumps(envelope, sort_keys=True, separators=(",", ":"))


def _validate_document_privacy_receipt(raw: Any, *, doc_id: str) -> dict[str, Any]:
    if not raw:
        raise KnowledgeBasePrivacyError(f"document {doc_id} missing privacy envelope")
    try:
        envelope = json.loads(str(raw)) if isinstance(raw, str) else raw
        if not isinstance(envelope, dict):
            raise ValueError("privacy envelope must be an object")
        return extract_privacy_envelope({PRIVACY_ENVELOPE_KEY: envelope})
    except Exception as exc:
        raise KnowledgeBasePrivacyError(f"document {doc_id} has invalid privacy envelope: {exc}") from exc


class DocumentInjectionError(ValueError):
    """Raised when an ingested document contains prompt-injection markers."""


_INJECTION_MARKERS: tuple[str, ...] = (
    "<system>",
    "</system>",
    "[inst]",
    "[/inst]",
    "<|system|>",
    "<|im_start|>",
    "<|im_end|>",
)


def _check_injection(content: str) -> None:
    """Reject documents whose text contains known prompt-injection markers.

    Args:
        content: Document text about to be ingested into the knowledge base.

    Raises:
        DocumentInjectionError: If the text contains an injection marker that
            would corrupt downstream prompt assembly when retrieved.
    """
    if not isinstance(content, str):
        return
    lowered = content.lower()
    for marker in _INJECTION_MARKERS:
        if marker in lowered:
            raise DocumentInjectionError(f"Document contains prompt-injection marker {marker!r}; refusing to ingest")


# ── Knowledge Base ────────────────────────────────────────────────────────


class KnowledgeBase(KnowledgeBaseSearch):
    """SQLite-backed knowledge base with FTS5 and optional sqlite-vec KNN search.

    In production (``db_path=None``) uses the unified database via
    ``vetinari.database.get_connection()``. When ``db_path`` is provided
    (test isolation), opens a dedicated persistent connection to that file.
    """

    _instance: KnowledgeBase | None = None
    _cls_lock = threading.Lock()

    def __init__(self, db_path: str | None = None):
        self._db_path: str | None = db_path
        self._has_vec = False
        self._conn: sqlite3.Connection | None = None
        self._lock = threading.RLock()
        self._embedding_attempts: int = 0
        self._embedding_fallbacks: int = 0
        self.last_effective_config_snapshot_id: str | None = None
        self._init_db()

    @classmethod
    def get_instance(cls) -> KnowledgeBase:
        """Get or create the singleton instance.

        Returns:
            The shared KnowledgeBase instance.
        """
        with cls._cls_lock:
            if cls._instance is None:
                cls._instance = cls()
        return cls._instance

    # ── Database Setup ────────────────────────────────────────────────

    def _init_db(self) -> None:
        """Create database schema with FTS5 and optional sqlite-vec support."""
        if self._db_path is not None:
            db_dir = Path(self._db_path).parent
            db_dir.mkdir(parents=True, exist_ok=True)
            self._conn = sqlite3.connect(self._db_path, check_same_thread=False)
            self._conn.row_factory = sqlite3.Row
            self._conn.execute("PRAGMA journal_mode=WAL")
            self._conn.execute(f"PRAGMA busy_timeout={DATABASE_BUSY_TIMEOUT_MS}")
        else:
            self._conn = get_connection()

        cursor = self._conn.cursor()

        cursor.execute("""
            CREATE TABLE IF NOT EXISTS documents (
                doc_id TEXT PRIMARY KEY,
                content TEXT NOT NULL,
                source TEXT NOT NULL,
                category TEXT NOT NULL DEFAULT 'general',
                privacy_envelope TEXT NOT NULL,
                created_at TEXT NOT NULL DEFAULT (datetime('now'))
            )
        """)
        self._ensure_privacy_envelope_column(cursor)

        cursor.execute("""
            CREATE TABLE IF NOT EXISTS doc_embeddings (
                doc_id TEXT PRIMARY KEY,
                embedding_blob BLOB NOT NULL,
                FOREIGN KEY (doc_id) REFERENCES documents(doc_id) ON DELETE CASCADE
            )
        """)

        cursor.execute("""
            CREATE VIRTUAL TABLE IF NOT EXISTS doc_fts USING fts5(
                doc_id,
                content,
                source,
                category,
                content=documents,
                content_rowid=rowid
            )
        """)

        self._create_fts_triggers(cursor)
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_doc_category ON documents(category)")
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_doc_source ON documents(source)")
        self._init_vec_table(cursor)

        self._conn.commit()
        logger.info("KnowledgeBase initialized (db=%s, sqlite_vec=%s)", self._db_path, self._has_vec)

    def _ensure_privacy_envelope_column(self, cursor: sqlite3.Cursor) -> None:
        """Add and backfill privacy receipts for databases created before this contract."""
        cursor.execute("PRAGMA table_info(documents)")
        columns = {str(row["name"]) for row in cursor.fetchall()}
        if "privacy_envelope" not in columns:
            cursor.execute("ALTER TABLE documents ADD COLUMN privacy_envelope TEXT")
        cursor.execute(
            "SELECT doc_id, source, category FROM documents WHERE privacy_envelope IS NULL OR privacy_envelope = ''"
        )
        rows = cursor.fetchall()
        for row in rows:
            envelope = _privacy_receipt_json(
                _document_privacy_receipt(source=str(row["source"]), category=str(row["category"]))
            )
            cursor.execute("UPDATE documents SET privacy_envelope = ? WHERE doc_id = ?", (envelope, row["doc_id"]))

    @staticmethod
    def _create_fts_triggers(cursor: sqlite3.Cursor) -> None:
        """Create FTS maintenance triggers for document changes."""
        cursor.execute("""
            CREATE TRIGGER IF NOT EXISTS doc_fts_ai AFTER INSERT ON documents BEGIN
                INSERT INTO doc_fts(rowid, doc_id, content, source, category)
                VALUES (NEW.rowid, NEW.doc_id, NEW.content, NEW.source, NEW.category);
            END
        """)
        cursor.execute("""
            CREATE TRIGGER IF NOT EXISTS doc_fts_ad AFTER DELETE ON documents BEGIN
                INSERT INTO doc_fts(doc_fts, rowid, doc_id, content, source, category)
                VALUES ('delete', OLD.rowid, OLD.doc_id, OLD.content, OLD.source, OLD.category);
            END
        """)
        cursor.execute("""
            CREATE TRIGGER IF NOT EXISTS doc_fts_au AFTER UPDATE ON documents BEGIN
                INSERT INTO doc_fts(doc_fts, rowid, doc_id, content, source, category)
                VALUES ('delete', OLD.rowid, OLD.doc_id, OLD.content, OLD.source, OLD.category);
                INSERT INTO doc_fts(rowid, doc_id, content, source, category)
                VALUES (NEW.rowid, NEW.doc_id, NEW.content, NEW.source, NEW.category);
            END
        """)

    def _init_vec_table(self, cursor: sqlite3.Cursor) -> None:
        """Create optional sqlite-vec table when the extension is available."""
        if load_sqlite_vec(self._conn):
            self._has_vec = True
            try:
                cursor.execute(f"""
                    CREATE VIRTUAL TABLE IF NOT EXISTS doc_vec USING vec0(
                        doc_id TEXT PRIMARY KEY,
                        embedding float[{_EMBEDDING_DIMENSIONS}]
                    )
                """)
            except sqlite3.OperationalError as exc:
                logger.warning("KB sqlite-vec table creation failed: %s", exc)
                self._has_vec = False

    def close(self) -> None:
        """Close the database connection and release resources.

        Only closes the connection when using a dedicated file (test-isolation
        mode). In production the unified connection lifecycle is managed by
        ``vetinari.database``.
        """
        if self._conn is not None and self._db_path is not None:
            self._conn.close()
            self._conn = None

    def __del__(self) -> None:
        """Safety-net cleanup if close() was not called explicitly."""
        with contextlib.suppress(Exception):
            self.close()

    # ── Ingestion ─────────────────────────────────────────────────────

    def add_document(
        self,
        content: str,
        source: str,
        category: str = "general",
        doc_id: str | None = None,
    ) -> str:
        """Add a document chunk to the knowledge base.

        Args:
            content: Document text content.
            source: Source file path or URL.
            category: Document category (docs, code, pattern, error, etc.).
            doc_id: Optional deterministic document ID.

        Returns:
            The document ID assigned to this chunk.
        """
        _check_injection(content)

        if not doc_id:
            digest = hashlib.sha256(f"{source}\0{content}".encode()).hexdigest()
            doc_id = require_nonempty(f"doc_{digest[:16]}", field_name="doc_id")

        truncated = content[:_MAX_DOC_CHARS]
        vec = _embed(truncated)
        privacy_envelope = _privacy_receipt_json(_document_privacy_receipt(source=source, category=category))

        with self._lock:
            cursor = self._conn.cursor()

            cursor.execute(
                "INSERT OR REPLACE INTO documents (doc_id, content, source, category, privacy_envelope) VALUES (?, ?, ?, ?, ?)",
                (doc_id, truncated, source, category, privacy_envelope),
            )

            if vec is not None:
                blob = pack_embedding(vec)
                cursor.execute(
                    "INSERT OR REPLACE INTO doc_embeddings (doc_id, embedding_blob) VALUES (?, ?)",
                    (doc_id, blob),
                )
                if self._has_vec:
                    try:
                        cursor.execute(
                            "INSERT OR REPLACE INTO doc_vec (doc_id, embedding) VALUES (?, ?)",
                            (doc_id, blob),
                        )
                    except sqlite3.Error as exc:
                        logger.warning("KB vec0 upsert failed for %s: %s", doc_id, exc)
            else:
                # Embedding unavailable — delete any stale embedding rows for
                # this doc so that vector search cannot return outdated results
                # for updated content.
                cursor.execute("DELETE FROM doc_embeddings WHERE doc_id = ?", (doc_id,))
                if self._has_vec:
                    try:
                        cursor.execute("DELETE FROM doc_vec WHERE doc_id = ?", (doc_id,))
                    except sqlite3.Error as exc:
                        logger.warning("KB vec0 stale-row delete failed for %s: %s", doc_id, exc)

            self._conn.commit()

        return doc_id

    def ingest_document(
        self,
        content: bytes,
        *,
        source: str,
        content_type: str,
        chunk_size: int = 1000,
        chunk_overlap: int = 100,
        category: str = "general",
    ) -> list[str]:
        """Ingest a single uploaded document (bytes) as one or more chunks.

        The bytes are decoded via :func:`extract_document_text`, which
        dispatches on ``content_type`` (with the filename suffix as a
        fallback signal) to the right extractor — currently PDF and
        UTF-8 text formats.  Extracted text is split with
        ``self._chunk(..., chunk_size, chunk_overlap)`` and each chunk
        is written through :meth:`add_document` so the embedding +
        FTS5 + sqlite-vec paths stay identical to the directory and URL
        ingest entry points.

        Args:
            content: Raw document bytes (the upload payload).
            source: Source identifier (typically the original filename)
                used as the chunk's ``source`` column and as the
                ``filename`` hint passed to the extractor.
            content_type: MIME type provided by the caller (e.g.
                ``application/pdf`` or ``text/markdown``).  Forwarded to
                ``extract_document_text``.
            chunk_size: Maximum characters per chunk.
            chunk_overlap: Overlap between consecutive chunks.
            category: Document category stored on every chunk row.

        Returns:
            List of document IDs written, in chunk order.

        Raises:
            UnsupportedDocumentTypeError: If ``content_type`` and the
                ``source`` suffix together do not match a supported
                extractor.
            UnicodeDecodeError: If a text document is not valid UTF-8.
            DocumentInjectionError: If any chunk contains a known
                prompt-injection marker.
        """
        text = extract_document_text(content, content_type=content_type, filename=source)
        chunks = self._chunk(text, chunk_size, chunk_overlap)
        doc_ids: list[str] = []
        for index, chunk in enumerate(chunks):
            chunk_hash = hashlib.md5(f"{source}_{index}".encode(), usedforsecurity=False).hexdigest()[:8]
            doc_id = f"doc_{chunk_hash}"
            doc_ids.append(self.add_document(content=chunk, source=source, category=category, doc_id=doc_id))
        logger.info("[KnowledgeBase] Ingested %d chunks from upload %s", len(doc_ids), source)
        return doc_ids

    def ingest_directory(
        self,
        directory: str,
        extensions: list[str] | None = None,
        chunk_size: int = 1000,
        chunk_overlap: int = 100,
    ) -> int:
        """Ingest all documents from a directory.

        Args:
            directory: Path to directory to ingest.
            extensions: File extensions to include (default: .md, .txt, .py, .yaml, .json).
            chunk_size: Maximum chunk size in characters.
            chunk_overlap: Overlap between consecutive chunks in characters.

        Returns:
            Number of chunks added.
        """
        extensions = extensions or [".md", ".txt", ".py", ".yaml", ".json"]
        try:
            base = Path(directory).resolve(strict=True)
        except OSError:
            logger.warning("[KnowledgeBase] Directory %s not found", directory)
            return 0
        if not base.is_dir():
            logger.debug("[KnowledgeBase] Directory %s is not a directory", directory)
            return 0

        count = 0
        files_seen = 0
        bytes_seen = 0
        for path in base.rglob("*"):
            if count >= _MAX_DIRECTORY_CHUNKS or files_seen >= _MAX_DIRECTORY_FILES:
                logger.warning("[KnowledgeBase] Directory ingest cap reached for %s", base)
                break
            if path.is_dir() or path.suffix.lower() not in extensions:
                continue
            try:
                relative_path = path.relative_to(base)
            except ValueError as exc:
                logger.warning("[KnowledgeBase] Skipped path outside ingest root %s: %s", path, exc)
                continue
            if path.is_symlink():
                logger.warning("[KnowledgeBase] Skipped symlink during directory ingest: %s", relative_path)
                continue
            if any(
                part.startswith(".") or part in ("__pycache__", "venv", "node_modules") for part in relative_path.parts
            ):
                continue
            try:
                resolved_path = path.resolve(strict=True)
                if not resolved_path.is_relative_to(base):
                    logger.warning("[KnowledgeBase] Skipped path outside ingest root: %s", relative_path)
                    continue
                files_seen += 1
                remaining_bytes = _MAX_DIRECTORY_BYTES - bytes_seen
                if remaining_bytes <= 0:
                    logger.warning("[KnowledgeBase] Directory ingest byte cap reached for %s", base)
                    break
                with resolved_path.open("rb") as handle:
                    raw = handle.read(remaining_bytes + 1)
                if len(raw) > remaining_bytes:
                    logger.warning("[KnowledgeBase] Skipped %s because ingest byte budget is exhausted", relative_path)
                    break
                bytes_seen += len(raw)
                content = raw.decode("utf-8", errors="ignore")
                chunks = self._chunk(content, chunk_size, chunk_overlap)[: _MAX_DIRECTORY_CHUNKS - count]
                source = relative_path.as_posix()
                self._delete_source_chunks(source, path)
                count += self._add_directory_chunks(path, source, chunks)
            except Exception as e:
                logger.warning("[KnowledgeBase] Skipped %s: %s", path, e)

        logger.info("[KnowledgeBase] Ingested %s chunks from %s", count, directory)
        return count

    def _delete_source_chunks(self, source: str, path: Path) -> None:
        """Delete stale chunks and embeddings for one ingested source path."""
        with self._lock:
            cursor = self._conn.cursor()
            cursor.execute("DELETE FROM documents WHERE source = ?", (source,))
            cursor.execute("DELETE FROM doc_embeddings WHERE doc_id NOT IN (SELECT doc_id FROM documents)")
            if self._has_vec:
                try:
                    cursor.execute("DELETE FROM doc_vec WHERE doc_id NOT IN (SELECT doc_id FROM documents)")
                except sqlite3.Error as exc:
                    logger.warning("KB vec0 stale-chunk cleanup failed for %s: %s", path, exc)
            self._conn.commit()

    def _add_directory_chunks(self, path: Path, source: str, chunks: list[str]) -> int:
        """Add chunk records for one file and return the number inserted."""
        count = 0
        for chunk in chunks:
            category = "code" if path.suffix == ".py" else "docs"
            self.add_document(content=chunk, source=source, category=category)
            count += 1
        return count

    def ingest_url(
        self,
        url: str,
        category: str = "docs",
        chunk_size: int = 1000,
        chunk_overlap: int = 100,
    ) -> int:
        """Fetch a URL and ingest its text content into the knowledge base.

        Validates the URL against SSRF attack vectors before making any
        network request.  Only ``http`` and ``https`` schemes are allowed;
        private/loopback/cloud-metadata addresses are rejected with a
        ``ValueError``.

        Args:
            url: The URL to fetch.  Must use http or https and must not
                point to a private or internal address.
            category: Document category to assign to the ingested chunks.
            chunk_size: Maximum size of each text chunk in characters.
            chunk_overlap: Overlap between consecutive chunks in characters.

        Returns:
            Number of chunks ingested from the URL, or 0 if the fetch fails.

        Raises:
            ValueError: If ``url`` fails SSRF validation (private IP,
                metadata endpoint, disallowed scheme, etc.).
        """
        return ingest_url_documents(
            self,
            url,
            category=category,
            chunk_size=chunk_size,
            chunk_overlap=chunk_overlap,
            require_https=_REQUIRE_HTTPS_URL_INGEST,
            max_chunks=_MAX_URL_CHUNKS,
            fetcher=_fetch_url_bytes,
        )

    # ── Querying ──────────────────────────────────────────────────────

    def query(
        self,
        query: str,
        k: int = 5,
        max_chars: int = 3000,
        category: str | None = None,
        reranker: Any | None = None,
    ) -> list[KBDocument]:
        """Retrieve the k most relevant documents for a query.

        Uses sqlite-vec KNN search when available, falls back to manual
        cosine similarity, then to FTS5 keyword search.  When supplied,
        ``reranker`` receives the original query and retrieved documents before
        the character budget is applied.

        Args:
            query: The search query string.
            k: Number of results to return.
            max_chars: Maximum total characters in returned results.
            category: Optional category filter.
            reranker: Optional callable ``(query, docs) -> iterable[KBDocument]``
                used to reorder or filter retrieved candidates.

        Returns:
            List of KBDocument sorted by relevance, truncated to max_chars.

        Raises:
            TypeError: If ``query`` or ``category`` has an invalid type.
            ValueError: If ``k`` or ``max_chars`` is outside its valid range.
        """
        if not isinstance(query, str):
            raise TypeError("query must be a string")
        if not isinstance(k, int) or k < 1:
            raise ValueError(f"k must be a positive integer, got {k!r}")
        if not isinstance(max_chars, int) or max_chars < 0:
            raise ValueError(f"max_chars must be a non-negative integer, got {max_chars!r}")
        if category is not None and not isinstance(category, str):
            raise TypeError("category must be a string or None")

        with self._lock:
            self._embedding_attempts += 1
            query_vec = _embed(query)
            backend = "sqlite_fts5"
            fallback_reason = "embedding-unavailable"
            if query_vec is not None:
                if self._has_vec:
                    backend = "sqlite_vec"
                    fallback_reason = None
                    results = self._query_vec_knn(query_vec, k, category, fallback_query=query)
                else:
                    backend = "manual-cosine"
                    fallback_reason = "sqlite-vec-unavailable"
                    results = self._query_cosine(query_vec, k, category, fallback_query=query)
            else:
                self._embedding_fallbacks += 1
                fallback_rate = self._embedding_fallbacks / self._embedding_attempts
                if self._embedding_fallbacks % 10 == 1:
                    logger.warning(
                        "KB embedding fallback: %d/%d queries (%.0f%%) using FTS5 instead of vectors",
                        self._embedding_fallbacks,
                        self._embedding_attempts,
                        fallback_rate * 100,
                    )
                results = self._query_fts(query, k, category)
            snapshot = capture_retrieval_config_snapshot(
                query,
                k=k,
                max_chars=max_chars,
                category=category,
                backend=backend,
                fallback_reason=fallback_reason,
            )
            self.last_effective_config_snapshot_id = snapshot.snapshot_id

        self._require_privacy_receipts(results)
        if reranker is not None and results:
            results = self._coerce_reranked_documents(reranker(query, tuple(results)))
            self._require_privacy_receipts(results)

        filtered: list[KBDocument] = []
        total_chars = 0
        for doc in results:
            if total_chars + len(doc.content) > max_chars:
                remaining = max_chars - total_chars
                if remaining > 0:
                    # Trim this document to fit within the budget rather than
                    # dropping it entirely.  The old threshold of 100 caused
                    # the first result to be discarded whenever the budget was
                    # small, returning nothing at all.
                    filtered.append(replace(doc, content=doc.content[:remaining]))
                break
            filtered.append(doc)
            total_chars += len(doc.content)

        return self._compress_results(filtered, max_chars)

    # ── Statistics ────────────────────────────────────────────────────

    @staticmethod
    def _coerce_reranked_documents(docs: Any) -> list[KBDocument]:
        if docs is None:
            return []
        reranked = list(docs)
        for index, doc in enumerate(reranked):
            if not isinstance(doc, KBDocument):
                raise TypeError(f"reranker returned non-KBDocument at index {index}")
        return reranked

    def _require_privacy_receipts(self, docs: list[KBDocument]) -> None:
        if not docs:
            return
        doc_ids = [doc.doc_id for doc in docs]
        with self._lock:
            cursor = self._conn.cursor()
            envelopes: dict[str, Any] = {}
            for doc_id in doc_ids:
                row = cursor.execute(
                    "SELECT privacy_envelope FROM documents WHERE doc_id = ?",
                    (doc_id,),
                ).fetchone()
                envelopes[doc_id] = row["privacy_envelope"] if row is not None else None
        for doc_id in doc_ids:
            _validate_document_privacy_receipt(envelopes.get(doc_id), doc_id=doc_id)

    def get_stats(self) -> dict[str, Any]:
        """Get knowledge base statistics.

        Returns:
            Dict with document count, backend type, database path, and counters.
        """
        return build_knowledge_base_stats(self)


# ── Module-level Accessors ────────────────────────────────────────────────


_kb: KnowledgeBase | None = None
_kb_lock = threading.Lock()


def get_knowledge_base() -> KnowledgeBase:
    """Return the global KnowledgeBase singleton.

    Returns:
        The shared KnowledgeBase instance.
    """
    global _kb
    if _kb is None:
        with _kb_lock:
            if _kb is None:
                _kb = KnowledgeBase.get_instance()
    return _kb


def ingest_project_docs() -> int:
    """Ingest all Vetinari project documentation into the knowledge base.

    Returns:
        Number of chunks ingested.
    """
    kb = get_knowledge_base()
    project_root = Path(__file__).parent.parent.parent
    total = 0
    for d in ["docs", "skills", "system_prompts", "prompts"]:
        p = project_root / d
        if p.exists():
            total += kb.ingest_directory(str(p))
    logger.info("[KnowledgeBase] Project docs ingestion complete: %s chunks", total)
    return total
