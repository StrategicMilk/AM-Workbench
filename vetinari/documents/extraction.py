"""Text extraction for RAG document ingestion."""

from __future__ import annotations

import io
import logging
from pathlib import Path

from vetinari.security.fail_closed import UntrustedInputError, sanitize_untrusted_text

logger = logging.getLogger(__name__)

_TEXT_CONTENT_TYPES = frozenset({
    "application/json",
    "application/markdown",
    "application/x-yaml",
    "text/csv",
    "text/markdown",
    "text/plain",
    "text/x-python",
    "text/yaml",
})
_PDF_CONTENT_TYPES = frozenset({"application/pdf"})
_TEXT_SUFFIXES = frozenset({".json", ".md", ".py", ".txt", ".yaml", ".yml"})
_MAX_DOCUMENT_BYTES = 5 * 1024 * 1024


class UnsupportedDocumentTypeError(ValueError):
    """Raised when document ingestion receives an unsupported content type."""


def extract_text_from_pdf(path: str | Path) -> str:
    """Extract text from a PDF file.

    Args:
        path: Local PDF path.

    Returns:
        Extracted text joined across pages.

    Raises:
        RuntimeError: If ``pypdf`` is unavailable or extraction fails.
    """
    pdf_path = Path(path)
    try:
        from pypdf import PdfReader
    except ImportError as exc:
        raise RuntimeError("PDF ingestion requires pypdf>=4.0") from exc
    try:
        reader = PdfReader(str(pdf_path))
        return "\n".join(page.extract_text() or "" for page in reader.pages).strip()
    except Exception as exc:
        raise RuntimeError(f"Could not extract text from PDF: {pdf_path}") from exc


def extract_text_from_pdf_bytes(data: bytes) -> str:
    """Extract text from PDF bytes.

    Args:
        data: Raw PDF bytes.

    Returns:
        Extracted text joined across pages.

    Raises:
        RuntimeError: If ``pypdf`` is unavailable or extraction fails.
    """
    try:
        from pypdf import PdfReader
    except ImportError as exc:
        raise RuntimeError("PDF ingestion requires pypdf>=4.0") from exc
    try:
        reader = PdfReader(io.BytesIO(data))
        return "\n".join(page.extract_text() or "" for page in reader.pages).strip()
    except Exception as exc:
        raise RuntimeError("Could not extract text from uploaded PDF bytes") from exc


def extract_document_text(data: bytes, *, content_type: str, filename: str | None = None) -> str:
    """Extract text from uploaded document bytes.

    Args:
        data: Raw document bytes.
        content_type: MIME content type supplied by the caller.
        filename: Optional filename used as a fallback type signal.

    Returns:
        Extracted UTF-8 text.

    Raises:
        UnsupportedDocumentTypeError: If the content type or suffix is not
            supported.
        UnicodeDecodeError: If a text document is not valid UTF-8.
    """
    if not isinstance(data, bytes):
        raise UnsupportedDocumentTypeError("Document data must be bytes")
    if len(data) > _MAX_DOCUMENT_BYTES:
        raise UnsupportedDocumentTypeError("Document exceeds maximum ingestion size")
    normalized_type = content_type.split(";", 1)[0].strip().lower()
    suffix = Path(filename or "").suffix.lower()
    if suffix and suffix not in _TEXT_SUFFIXES | frozenset({".pdf"}):
        raise UnsupportedDocumentTypeError(f"Unsupported document suffix: {suffix}")
    if normalized_type in _PDF_CONTENT_TYPES:
        return extract_text_from_pdf_bytes(data)
    if suffix == ".pdf" and normalized_type not in _TEXT_CONTENT_TYPES:
        raise UnsupportedDocumentTypeError("PDF filename requires application/pdf content type")
    if normalized_type in _TEXT_CONTENT_TYPES:
        try:
            return sanitize_untrusted_text(data.decode("utf-8"), max_length=_MAX_DOCUMENT_BYTES)
        except UntrustedInputError as exc:
            raise UnsupportedDocumentTypeError(f"Unsupported document text: {exc}") from exc
    raise UnsupportedDocumentTypeError(f"Unsupported document type: {content_type or suffix or 'unknown'}")
