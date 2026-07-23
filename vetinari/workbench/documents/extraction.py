"""Immutable document intelligence records for Workbench extraction evidence."""

from __future__ import annotations

import re
from collections.abc import Iterable
from dataclasses import dataclass
from enum import Enum

_SHA256_RE = re.compile(r"^[a-fA-F0-9]{64}$")


class DocumentExtractionError(ValueError):
    """Raised when document extraction evidence cannot be trusted."""


class DocumentChunkKind(str, Enum):
    """Structured chunk kinds preserved from a document extraction."""

    PARAGRAPH = "paragraph"
    HEADING = "heading"
    TABLE = "table"
    IMAGE = "image"
    OCR_TEXT = "ocr_text"


class DocumentExtractionMethod(str, Enum):
    """Known extraction methods for document evidence."""

    PDF_TEXT_LAYER = "pdf_text_layer"
    OCR = "ocr"
    LAYOUT_ANALYSIS = "layout_analysis"
    TABLE_DETECTION = "table_detection"
    IMAGE_EXTRACTION = "image_extraction"


class DocumentRedactionStatus(str, Enum):
    """Lifecycle state for a redaction reference."""

    PROPOSED = "proposed"
    REVIEWED = "reviewed"
    APPLIED_UPSTREAM = "applied_upstream"


@dataclass(frozen=True, slots=True)
class VisualGrounding:
    """Page-local region grounding for document evidence."""

    page_number: int
    region_ref: str
    bounding_box: tuple[float, float, float, float] | None
    provenance_refs: tuple[str, ...]

    def __post_init__(self) -> None:
        _require_positive_int(self.page_number, "VisualGrounding.page_number")
        _require_non_empty(self.region_ref, "VisualGrounding.region_ref")
        _set_tuple(self, "provenance_refs", self.provenance_refs, field_name="VisualGrounding.provenance_refs")
        if not self.provenance_refs:
            raise DocumentExtractionError("VisualGrounding.provenance_refs must be non-empty")
        if self.bounding_box is not None:
            if len(self.bounding_box) != 4:
                raise DocumentExtractionError("VisualGrounding.bounding_box must contain four numbers")
            left, top, right, bottom = self.bounding_box
            if left < 0 or top < 0 or right <= left or bottom <= top:
                raise DocumentExtractionError("VisualGrounding.bounding_box must be a positive region")

    def __repr__(self) -> str:
        """Return a compact diagnostic representation."""
        return f"VisualGrounding(page_number={self.page_number!r}, region_ref={self.region_ref!r}, bounding_box={self.bounding_box!r})"


@dataclass(frozen=True, slots=True)
class OcrEvidence:
    """OCR text and confidence anchored to a document page."""

    page_number: int
    text: str
    ocr_engine: str
    confidence: float
    provenance_refs: tuple[str, ...]
    visual_grounding: VisualGrounding | None = None

    def __post_init__(self) -> None:
        _require_positive_int(self.page_number, "OcrEvidence.page_number")
        _require_non_empty(self.text, "OcrEvidence.text")
        _require_non_empty(self.ocr_engine, "OcrEvidence.ocr_engine")
        _require_confidence(self.confidence, "OcrEvidence.confidence")
        _set_tuple(self, "provenance_refs", self.provenance_refs, field_name="OcrEvidence.provenance_refs")
        if not self.provenance_refs:
            raise DocumentExtractionError("OcrEvidence.provenance_refs must be non-empty")
        if self.visual_grounding is not None and self.visual_grounding.page_number != self.page_number:
            raise DocumentExtractionError("OcrEvidence.visual_grounding page does not match page_number")

    def __repr__(self) -> str:
        """Return a compact diagnostic representation."""
        return f"OcrEvidence(page_number={self.page_number!r}, text={self.text!r}, ocr_engine={self.ocr_engine!r})"


@dataclass(frozen=True, slots=True)
class DocumentPage:
    """One extracted page with layout provenance."""

    page_number: int
    width: float
    height: float
    text_layer_present: bool
    provenance_refs: tuple[str, ...]

    def __post_init__(self) -> None:
        _require_positive_int(self.page_number, "DocumentPage.page_number")
        if self.width <= 0 or self.height <= 0:
            raise DocumentExtractionError("DocumentPage width and height must be positive")
        _set_tuple(self, "provenance_refs", self.provenance_refs, field_name="DocumentPage.provenance_refs")
        if not self.provenance_refs:
            raise DocumentExtractionError("DocumentPage.provenance_refs must be non-empty")

    def __repr__(self) -> str:
        """Return a compact diagnostic representation."""
        return f"DocumentPage(page_number={self.page_number!r}, width={self.width!r}, height={self.height!r})"


@dataclass(frozen=True, slots=True)
class DocumentChunk:
    """Structured text chunk with page and visual/layout grounding."""

    chunk_id: str
    kind: DocumentChunkKind
    text: str
    page_number: int
    extraction_method: DocumentExtractionMethod
    confidence: float
    provenance_refs: tuple[str, ...]
    visual_grounding: VisualGrounding | None = None
    no_visual_region_reason: str | None = None

    def __post_init__(self) -> None:
        _require_non_empty(self.chunk_id, "DocumentChunk.chunk_id")
        _require_non_empty(self.text, "DocumentChunk.text")
        _require_positive_int(self.page_number, "DocumentChunk.page_number")
        object.__setattr__(self, "kind", DocumentChunkKind(self.kind))
        object.__setattr__(self, "extraction_method", DocumentExtractionMethod(self.extraction_method))
        _require_confidence(self.confidence, "DocumentChunk.confidence")
        _set_tuple(self, "provenance_refs", self.provenance_refs, field_name="DocumentChunk.provenance_refs")
        if not self.provenance_refs:
            raise DocumentExtractionError("DocumentChunk.provenance_refs must be non-empty")
        if self.visual_grounding is None:
            _require_non_empty(self.no_visual_region_reason, "DocumentChunk.no_visual_region_reason")
        elif self.visual_grounding.page_number != self.page_number:
            raise DocumentExtractionError("DocumentChunk.visual_grounding page does not match page_number")

    def __repr__(self) -> str:
        """Return a compact diagnostic representation."""
        return f"DocumentChunk(chunk_id={self.chunk_id!r}, kind={self.kind!r}, text={self.text!r})"


@dataclass(frozen=True, slots=True)
class DocumentTable:
    """Extracted table structure with source page grounding."""

    table_id: str
    page_number: int
    rows: tuple[tuple[str, ...], ...]
    confidence: float
    visual_grounding: VisualGrounding
    provenance_refs: tuple[str, ...]

    def __post_init__(self) -> None:
        _require_non_empty(self.table_id, "DocumentTable.table_id")
        _require_positive_int(self.page_number, "DocumentTable.page_number")
        _set_nested_tuple(self, "rows", self.rows, field_name="DocumentTable.rows")
        if not self.rows:
            raise DocumentExtractionError("DocumentTable.rows must be non-empty")
        _require_confidence(self.confidence, "DocumentTable.confidence")
        if self.visual_grounding.page_number != self.page_number:
            raise DocumentExtractionError("DocumentTable.visual_grounding page does not match page_number")
        _set_tuple(self, "provenance_refs", self.provenance_refs, field_name="DocumentTable.provenance_refs")
        if not self.provenance_refs:
            raise DocumentExtractionError("DocumentTable.provenance_refs must be non-empty")

    def __repr__(self) -> str:
        """Return a compact diagnostic representation."""
        return f"DocumentTable(table_id={self.table_id!r}, page_number={self.page_number!r}, rows={self.rows!r})"


@dataclass(frozen=True, slots=True)
class DocumentImage:
    """Extracted embedded image with page grounding."""

    image_id: str
    page_number: int
    alt_text: str
    confidence: float
    visual_grounding: VisualGrounding
    provenance_refs: tuple[str, ...]

    def __post_init__(self) -> None:
        _require_non_empty(self.image_id, "DocumentImage.image_id")
        _require_positive_int(self.page_number, "DocumentImage.page_number")
        _require_non_empty(self.alt_text, "DocumentImage.alt_text")
        _require_confidence(self.confidence, "DocumentImage.confidence")
        if self.visual_grounding.page_number != self.page_number:
            raise DocumentExtractionError("DocumentImage.visual_grounding page does not match page_number")
        _set_tuple(self, "provenance_refs", self.provenance_refs, field_name="DocumentImage.provenance_refs")
        if not self.provenance_refs:
            raise DocumentExtractionError("DocumentImage.provenance_refs must be non-empty")

    def __repr__(self) -> str:
        """Return a compact diagnostic representation."""
        return (
            f"DocumentImage(image_id={self.image_id!r}, page_number={self.page_number!r}, alt_text={self.alt_text!r})"
        )


@dataclass(frozen=True, slots=True)
class DocumentRedactionSpan:
    """Document redaction reference without claiming enforcement by this layer."""

    redaction_id: str
    page_number: int
    start_offset: int
    end_offset: int
    redaction_reason: str
    policy_ref: str
    provenance_refs: tuple[str, ...]
    status: DocumentRedactionStatus = DocumentRedactionStatus.PROPOSED

    def __post_init__(self) -> None:
        _require_non_empty(self.redaction_id, "DocumentRedactionSpan.redaction_id")
        _require_positive_int(self.page_number, "DocumentRedactionSpan.page_number")
        if self.start_offset < 0 or self.end_offset <= self.start_offset:
            raise DocumentExtractionError("DocumentRedactionSpan offsets must be a positive range")
        _require_non_empty(self.redaction_reason, "DocumentRedactionSpan.redaction_reason")
        _require_non_empty(self.policy_ref, "DocumentRedactionSpan.policy_ref")
        _set_tuple(self, "provenance_refs", self.provenance_refs, field_name="DocumentRedactionSpan.provenance_refs")
        if not self.provenance_refs:
            raise DocumentExtractionError("DocumentRedactionSpan.provenance_refs must be non-empty")
        object.__setattr__(self, "status", DocumentRedactionStatus(self.status))

    def __repr__(self) -> str:
        """Return a compact diagnostic representation."""
        return f"DocumentRedactionSpan(redaction_id={self.redaction_id!r}, page_number={self.page_number!r}, start_offset={self.start_offset!r})"


@dataclass(frozen=True, slots=True)
class DocumentAnnotationHook:
    """Annotation hook for later review layers."""

    annotation_id: str
    target_ref: str
    label: str
    provenance_refs: tuple[str, ...]

    def __post_init__(self) -> None:
        _require_non_empty(self.annotation_id, "DocumentAnnotationHook.annotation_id")
        _require_non_empty(self.target_ref, "DocumentAnnotationHook.target_ref")
        _require_non_empty(self.label, "DocumentAnnotationHook.label")
        _set_tuple(self, "provenance_refs", self.provenance_refs, field_name="DocumentAnnotationHook.provenance_refs")
        if not self.provenance_refs:
            raise DocumentExtractionError("DocumentAnnotationHook.provenance_refs must be non-empty")

    def __repr__(self) -> str:
        """Return a compact diagnostic representation."""
        return f"DocumentAnnotationHook(annotation_id={self.annotation_id!r}, target_ref={self.target_ref!r}, label={self.label!r})"


@dataclass(frozen=True, slots=True)
class DocumentExtraction:
    """Complete structured document extraction payload."""

    extraction_id: str
    source_card_id: str
    dataset_revision_id: str
    content_sha256: str
    pages: tuple[DocumentPage, ...]
    chunks: tuple[DocumentChunk, ...]
    provenance_refs: tuple[str, ...]
    ocr_evidence: tuple[OcrEvidence, ...] = ()
    tables: tuple[DocumentTable, ...] = ()
    images: tuple[DocumentImage, ...] = ()
    redactions: tuple[DocumentRedactionSpan, ...] = ()
    annotation_hooks: tuple[DocumentAnnotationHook, ...] = ()

    def __post_init__(self) -> None:
        _require_non_empty(self.extraction_id, "DocumentExtraction.extraction_id")
        _require_non_empty(self.source_card_id, "DocumentExtraction.source_card_id")
        _require_non_empty(self.dataset_revision_id, "DocumentExtraction.dataset_revision_id")
        _require_sha256(self.content_sha256, "DocumentExtraction.content_sha256")
        _set_tuple(self, "pages", self.pages, expected=DocumentPage, field_name="DocumentExtraction.pages")
        _set_tuple(self, "chunks", self.chunks, expected=DocumentChunk, field_name="DocumentExtraction.chunks")
        _set_tuple(self, "provenance_refs", self.provenance_refs, field_name="DocumentExtraction.provenance_refs")
        _set_tuple(
            self,
            "ocr_evidence",
            self.ocr_evidence,
            expected=OcrEvidence,
            field_name="DocumentExtraction.ocr_evidence",
        )
        _set_tuple(self, "tables", self.tables, expected=DocumentTable, field_name="DocumentExtraction.tables")
        _set_tuple(self, "images", self.images, expected=DocumentImage, field_name="DocumentExtraction.images")
        _set_tuple(
            self,
            "redactions",
            self.redactions,
            expected=DocumentRedactionSpan,
            field_name="DocumentExtraction.redactions",
        )
        _set_tuple(
            self,
            "annotation_hooks",
            self.annotation_hooks,
            expected=DocumentAnnotationHook,
            field_name="DocumentExtraction.annotation_hooks",
        )
        if not self.pages:
            raise DocumentExtractionError("DocumentExtraction.pages must be non-empty")
        if not self.chunks:
            raise DocumentExtractionError("DocumentExtraction.chunks must be non-empty")
        if not self.provenance_refs:
            raise DocumentExtractionError("DocumentExtraction.provenance_refs must be non-empty")
        page_numbers = {page.page_number for page in self.pages}
        for chunk in self.chunks:
            if chunk.page_number not in page_numbers:
                raise DocumentExtractionError(f"chunk {chunk.chunk_id!r} references an unknown page")
        for ocr in self.ocr_evidence:
            if ocr.page_number not in page_numbers:
                raise DocumentExtractionError("OCR evidence references an unknown page")
        for table in self.tables:
            if table.page_number not in page_numbers:
                raise DocumentExtractionError(f"table {table.table_id!r} references an unknown page")
        for image in self.images:
            if image.page_number not in page_numbers:
                raise DocumentExtractionError(f"image {image.image_id!r} references an unknown page")
        for redaction in self.redactions:
            if redaction.page_number not in page_numbers:
                raise DocumentExtractionError(f"redaction {redaction.redaction_id!r} references an unknown page")
        if not (self.ocr_evidence or self.tables or self.images):
            raise DocumentExtractionError("DocumentExtraction requires OCR, table, or image grounding evidence")

    def __repr__(self) -> str:
        """Return a compact diagnostic representation."""
        return f"DocumentExtraction(extraction_id={self.extraction_id!r}, source_card_id={self.source_card_id!r}, dataset_revision_id={self.dataset_revision_id!r})"


def build_document_extraction(
    *,
    extraction_id: str,
    source_card_id: str,
    dataset_revision_id: str,
    content_sha256: str,
    pages: Iterable[DocumentPage],
    chunks: Iterable[DocumentChunk],
    provenance_refs: Iterable[str],
    ocr_evidence: Iterable[OcrEvidence] = (),
    tables: Iterable[DocumentTable] = (),
    images: Iterable[DocumentImage] = (),
    redactions: Iterable[DocumentRedactionSpan] = (),
    annotation_hooks: Iterable[DocumentAnnotationHook] = (),
) -> DocumentExtraction:
    """Normalize iterables into an immutable document extraction and validate relationships."""
    return DocumentExtraction(
        extraction_id=extraction_id,
        source_card_id=source_card_id,
        dataset_revision_id=dataset_revision_id,
        content_sha256=content_sha256,
        pages=tuple(pages),
        chunks=tuple(chunks),
        provenance_refs=tuple(provenance_refs),
        ocr_evidence=tuple(ocr_evidence),
        tables=tuple(tables),
        images=tuple(images),
        redactions=tuple(redactions),
        annotation_hooks=tuple(annotation_hooks),
    )


def _require_non_empty(value: str | None, field_name: str) -> None:
    if value is None or not str(value).strip():
        raise DocumentExtractionError(f"{field_name} must be non-empty")


def _require_positive_int(value: int, field_name: str) -> None:
    if not isinstance(value, int) or value < 1:
        raise DocumentExtractionError(f"{field_name} must be a positive integer")


def _require_confidence(value: float, field_name: str) -> None:
    if value < 0.0 or value > 1.0:
        raise DocumentExtractionError(f"{field_name} must be between 0.0 and 1.0")


def _require_sha256(value: str, field_name: str) -> None:
    if not _SHA256_RE.fullmatch(value):
        raise DocumentExtractionError(f"{field_name} must be a 64-character sha256 hex digest")


def _set_tuple(
    instance: object,
    attr: str,
    values: Iterable[object],
    *,
    expected: type[object] | None = None,
    field_name: str,
) -> None:
    normalized = tuple(values)
    if expected is not None and any(not isinstance(item, expected) for item in normalized):
        raise DocumentExtractionError(f"{field_name} must contain {expected.__name__} instances")
    for item in normalized:
        if isinstance(item, str):
            _require_non_empty(item, f"{field_name}[]")
    object.__setattr__(instance, attr, normalized)


def _set_nested_tuple(
    instance: object,
    attr: str,
    rows: Iterable[Iterable[str]],
    *,
    field_name: str,
) -> None:
    normalized = tuple(tuple(str(cell) for cell in row) for row in rows)
    if any(not row for row in normalized):
        raise DocumentExtractionError(f"{field_name} must not contain empty rows")
    object.__setattr__(instance, attr, normalized)


__all__ = [
    "DocumentAnnotationHook",
    "DocumentChunk",
    "DocumentChunkKind",
    "DocumentExtraction",
    "DocumentExtractionError",
    "DocumentExtractionMethod",
    "DocumentImage",
    "DocumentPage",
    "DocumentRedactionSpan",
    "DocumentRedactionStatus",
    "DocumentTable",
    "OcrEvidence",
    "VisualGrounding",
    "build_document_extraction",
]
