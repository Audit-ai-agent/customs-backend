"""Small Backend-owned models for local development and functional checks."""

from __future__ import annotations

import hashlib
import json
import unicodedata
from datetime import UTC, date, datetime
from enum import Enum
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


def utc_now() -> datetime:
    return datetime.now(UTC)


def canonical_json_bytes(value: Any) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    ).encode("utf-8")


def canonical_digest(value: Any) -> str:
    return hashlib.sha256(canonical_json_bytes(value)).hexdigest()


MAX_TEXT_DOCUMENT_CHARACTERS = 200_000
MAX_TEXT_DOCUMENT_UTF8_BYTES = MAX_TEXT_DOCUMENT_CHARACTERS * 4
# Keep the public upload boundary no larger than the Agent's independently
# enforced default.  Accepting a larger asset here would persist a binary that
# the isolated preview service must reject later, which is neither useful nor
# a safe preview workflow.
MAX_DOCUMENT_ASSET_BYTES = 16 * 1024 * 1024
MAX_DOCUMENT_ASSET_PAGES = 20
MAX_DOCUMENT_ASSET_PIXELS = 40_000_000
# A reviewer page is encoded from an already-admitted PDF page at the
# approval-pinned DPI.  It is never persisted or returned in a JSON payload,
# but its transport still needs a hard in-memory ceiling independent of the
# source upload limit.
MAX_RASTER_VIEWER_PAGE_BYTES = 32 * 1024 * 1024
_PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"
_PNG_IHDR_LENGTH = 13
_PNG_IHDR_TOTAL_BYTES = len(_PNG_SIGNATURE) + 4 + 4 + _PNG_IHDR_LENGTH + 4


def _png_ihdr_dimensions(content: bytes) -> tuple[int, int]:
    """Read only the fixed PNG signature and IHDR prefix needed for dimensions.

    This is deliberately not an image decode: the page bytes have already been
    bounded at the transport boundary, and the Backend only needs a stable,
    constant-size check that the Agent's claimed dimensions describe the PNG
    the reviewer will receive.
    """

    if (
        len(content) < _PNG_IHDR_TOTAL_BYTES
        or content[: len(_PNG_SIGNATURE)] != _PNG_SIGNATURE
    ):
        raise ValueError("raster viewer page is not a complete PNG IHDR prefix")
    if int.from_bytes(content[8:12], byteorder="big") != _PNG_IHDR_LENGTH:
        raise ValueError("raster viewer page has an invalid PNG IHDR length")
    if content[12:16] != b"IHDR":
        raise ValueError("raster viewer page has an invalid PNG IHDR type")

    width = int.from_bytes(content[16:20], byteorder="big")
    height = int.from_bytes(content[20:24], byteorder="big")
    if width < 1 or height < 1:
        raise ValueError("raster viewer page has invalid PNG dimensions")
    return width, height


# Keep the compact legacy codes while adding the text documents needed for a
# normal export workbench.  They are only labels for text input; this does not
# imply PDF/image/OCR support.
DocumentType = Literal[
    "CI",
    "PL",
    "BL",
    "CO",
    "AWB",
    "EXPORT_DECLARATION",
    "SUPPORTING_EVIDENCE",
    "UNKNOWN",
]
CaseTradeType = Literal["import", "export"]
KnowledgeTradeType = Literal["import", "export", "both"]
GuidanceCoverage = Literal["covered", "no_coverage", "partial", "unavailable"]
GuidanceRegistryVersion = Literal["1.0", "2.0"]
GuidanceRequestedDirection = Literal["import", "export", "both"]
GuidanceApplicableDirection = Literal["import", "export"]
CaseInputNotice = Literal["unregistered_document_text"]
# ``development_fixture`` is deliberately separate from the official snapshot
# registry.  Functional-UAT can render every official registry category, but
# must never present the deterministic development fixture as an official
# source.
GuidanceSourceKind = Literal[
    "official_law",
    "official_law_catalog",
    "official_agency_guidance",
    "official_requirement_reference",
    "official_public_guidance",
    "development_fixture",
]


class Role(str, Enum):
    reviewer = "reviewer"
    approver = "approver"


class Principal(BaseModel):
    subject: str = Field(min_length=1, max_length=256, pattern=r"^[A-Za-z0-9][A-Za-z0-9._:@/-]*$")
    org_id: str = Field(min_length=1, max_length=128, pattern=r"^[A-Za-z0-9][A-Za-z0-9._:-]*$")
    roles: frozenset[Role]


class CaseStatus(str, Enum):
    open = "open"
    in_review = "in_review"
    closed = "closed"


class JobStatus(str, Enum):
    queued = "queued"
    running = "running"
    succeeded = "succeeded"
    failed = "failed"


class ExecutionStatus(str, Enum):
    succeeded = "succeeded"
    partial = "partial"
    failed = "failed"


class Assessment(str, Enum):
    no_issue_detected = "no_issue_detected"
    issues_detected = "issues_detected"
    indeterminate = "indeterminate"


class ReviewDisposition(str, Enum):
    # Existing Browser values are retained only as inert compatibility metadata.
    auto_review_complete = "auto_review_complete"
    human_review_required = "human_review_required"
    blocked = "blocked"


class AssuranceLevel(str, Enum):
    # Existing Browser values are retained only as inert compatibility metadata.
    diagnostic = "diagnostic"
    production_candidate = "production_candidate"
    production = "production"


class CaseCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    title: str = Field(min_length=1, max_length=200)
    external_reference: str | None = Field(default=None, max_length=200)
    metadata: dict[str, Any] = Field(default_factory=dict)


class CaseClosureRequest(BaseModel):
    """A reviewer-owned closure decision; it never sends anything anywhere."""

    model_config = ConfigDict(extra="forbid")

    disposition: Literal["cleared", "returned"]
    reason: str = Field(min_length=1, max_length=4000)

    @field_validator("reason")
    @classmethod
    def non_blank_reason(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("a closure reason is required")
        return value


class CaseClosureRecord(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    disposition: Literal["cleared", "returned"]
    reason: str = Field(min_length=1, max_length=4000)
    review_run_id: str = Field(min_length=1, max_length=128)
    closed_by: str = Field(min_length=1, max_length=256)
    closed_at: datetime


class CaseHsAgentRankingRecord(BaseModel):
    """The last Agent-ranked HSK candidate list for a case.

    Kept so that reopening the case shows the ranking again without spending
    the Agent's two model rounds.  It is bound to the document set (ids and
    content digests) it was read from: once a document is added, replaced or
    re-confirmed the ranking is hidden until the reviewer asks again.
    """

    model_config = ConfigDict(extra="forbid")

    document_digest: str = Field(pattern=r"^[a-f0-9]{64}$")
    ranked_at: datetime
    candidates: list[dict[str, Any]] = Field(default_factory=list, max_length=10)


class CaseRecord(BaseModel):
    case_id: str
    org_id: str
    title: str
    external_reference: str | None = None
    status: CaseStatus = CaseStatus.open
    revision: int = Field(default=0, ge=0)
    document_manifest_digest: str = Field(pattern=r"^[a-f0-9]{64}$")
    metadata: dict[str, Any] = Field(default_factory=dict)
    # The reviewer's final internal disposition. Historic cases stay readable
    # with no closure; a closed case never carries an external transmission.
    closure: CaseClosureRecord | None = None
    # A reviewer may confirm the HSK code before the first AI review so the
    # very first run can already retrieve law evidence.  Reviewer-owned;
    # never derived from or written by an Agent result.
    hs_selection: HsSelectionRecord | None = None
    # Advisory only: the Agent's last ranking, never a source for review
    # facts or the reviewer's selection.
    hs_agent_ranking: CaseHsAgentRankingRecord | None = None
    created_by: str
    created_at: datetime
    updated_at: datetime


class TextDocumentCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    document_type: DocumentType
    filename: str = Field(min_length=1, max_length=255)
    content_text: str = Field(min_length=1, max_length=MAX_TEXT_DOCUMENT_CHARACTERS)
    media_type: Literal["text/plain"] = "text/plain"
    replaces_document_id: str | None = Field(
        default=None,
        min_length=1,
        max_length=128,
        pattern=r"^[A-Za-z0-9][A-Za-z0-9._:-]*$",
    )

    @field_validator("filename")
    @classmethod
    def text_only_filename(cls, value: str) -> str:
        if value.casefold().endswith((".pdf", ".png", ".jpg", ".jpeg", ".tif", ".tiff")):
            raise ValueError("visual document input is not supported; submit extracted text")
        return value

    @field_validator("content_text")
    @classmethod
    def bounded_utf8_payload(cls, value: str) -> str:
        if len(value.encode("utf-8")) > MAX_TEXT_DOCUMENT_UTF8_BYTES:
            raise ValueError("text document exceeds the UTF-8 byte safety limit")
        return value


DocumentExtractionSource = Literal["text_layer", "vlm_ocr", "raster_ocr"]


class DocumentRecord(BaseModel):
    document_id: str
    case_id: str
    org_id: str
    document_type: DocumentType
    filename: str
    media_type: str
    content_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    object_key: str
    byte_size: int = Field(ge=1)
    character_count: int = Field(ge=1, le=MAX_TEXT_DOCUMENT_CHARACTERS)
    created_by: str
    created_at: datetime
    # A reviewer-confirmed text document may have come from an unverified
    # extraction preview.  The review contract still receives text only; this
    # link is operational provenance and never authorises OCR-based findings.
    source_asset_id: str | None = None
    # How the confirmed text was obtained (born-digital text layer, VLM OCR,
    # or the raster OCR + layout engine) and a count-only layout summary.
    # Provenance for the Agent's confidence signal; never review input itself.
    extraction_source: DocumentExtractionSource | None = None
    layout_summary: dict[str, Any] | None = None
    # A document is immutable.  A correction creates a new record and marks
    # the earlier record as superseded, so historic review input snapshots
    # remain reproducible.
    replaces_document_id: str | None = None
    superseded_by_document_id: str | None = None
    superseded_at: datetime | None = None


DocumentAssetMediaType = Literal["application/pdf", "image/jpeg", "image/png"]
DocumentExtractionStatus = Literal[
    "queued", "preview_ready", "needs_review", "unavailable", "failed"
]
RasterDocumentExtractionStatus = Literal[
    "queued", "running", "preview_ready", "needs_review", "unavailable", "failed"
]
# Keep the text-only path independently named even though its queue lifecycle
# matches the structured raster preview.  A text-only activation must never be
# mistaken for an approval of table/cell/geometry reconstruction.
RasterTextDocumentExtractionStatus = Literal[
    "queued", "running", "preview_ready", "needs_review", "unavailable", "failed"
]
RasterDocumentType = Literal[
    "bill_of_lading", "commercial_invoice", "packing_list", "unknown"
]


class DocumentAssetRecord(BaseModel):
    """Immutable binary source kept outside the text-review input boundary."""

    model_config = ConfigDict(extra="forbid")

    asset_id: str = Field(min_length=1, max_length=128, pattern=r"^[A-Za-z0-9][A-Za-z0-9._:-]*$")
    case_id: str = Field(min_length=1, max_length=128)
    org_id: str = Field(min_length=1, max_length=128)
    document_type: DocumentType
    filename: str = Field(min_length=1, max_length=255)
    media_type: DocumentAssetMediaType
    source_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    object_key: str = Field(min_length=1, max_length=512)
    byte_size: int = Field(ge=1, le=MAX_DOCUMENT_ASSET_BYTES)
    page_count: int = Field(ge=1, le=MAX_DOCUMENT_ASSET_PAGES)
    pixel_count: int = Field(ge=0, le=MAX_DOCUMENT_ASSET_PIXELS)
    created_by: str = Field(min_length=1, max_length=256)
    created_at: datetime

    @field_validator("filename")
    @classmethod
    def safe_binary_filename(cls, value: str) -> str:
        """Keep stored Browser-visible filenames out of path/control syntax."""

        if not value.strip() or any(character in value for character in ("/", "\\", "\r", "\n", "\x00")):
            raise ValueError("document asset filename is invalid")
        return value


class ExtractionBoundingBox(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    x: float = Field(ge=0, le=1)
    y: float = Field(ge=0, le=1)
    width: float = Field(gt=0, le=1)
    height: float = Field(gt=0, le=1)

    @model_validator(mode="after")
    def fits_inside_normalized_page(self) -> "ExtractionBoundingBox":
        if self.x + self.width > 1 or self.y + self.height > 1:
            raise ValueError("extraction bounding box exceeds its page")
        return self


class ExtractionSpan(BaseModel):
    """A human-visible OCR/text-layer candidate with optional page geometry."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    span_id: str = Field(
        min_length=1, max_length=128, pattern=r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$"
    )
    ordinal: int = Field(ge=1, le=100_000)
    text: str = Field(min_length=1, max_length=8_000)
    bbox: ExtractionBoundingBox | None = None
    reading_order: int = Field(ge=1, le=100_000)


class ExtractionPage(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    page_number: int = Field(ge=1, le=MAX_DOCUMENT_ASSET_PAGES)
    width: int = Field(ge=1, le=100_000)
    height: int = Field(ge=1, le=100_000)
    coordinate_unit: Literal["pixels", "pdf_points"]
    spans: list[ExtractionSpan] = Field(default_factory=list, max_length=20_000)

    @model_validator(mode="after")
    def span_identity_and_order_are_consistent(self) -> "ExtractionPage":
        span_ids = [span.span_id for span in self.spans]
        ordinals = [span.ordinal for span in self.spans]
        reading_order = [span.reading_order for span in self.spans]
        if (
            len(span_ids) != len(set(span_ids))
            or len(ordinals) != len(set(ordinals))
            or len(reading_order) != len(set(reading_order))
            or ordinals != sorted(ordinals)
            or reading_order != sorted(reading_order)
        ):
            raise ValueError("extraction spans are not uniquely ordered")
        return self


class ExtractionWarning(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    code: str = Field(min_length=1, max_length=128, pattern=r"^[a-z][a-z0-9_]{1,127}$")


def canonical_extraction_text_sha256(pages: list[ExtractionPage]) -> str:
    """Bind the visible preview spans to the Agent-provided text receipt."""

    text = "\n".join(
        unicodedata.normalize("NFC", span.text).replace("\r\n", "\n").replace("\r", "\n")
        for page in sorted(pages, key=lambda item: item.page_number)
        for span in sorted(page.spans, key=lambda item: (item.reading_order, item.ordinal))
    )
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


class DocumentExtractionResult(BaseModel):
    """Safe preview projection returned by the Agent; never contains source bytes."""

    model_config = ConfigDict(extra="forbid")

    schema_version: Literal["1.0"] = "1.0"
    document_asset_id: str = Field(
        min_length=1, max_length=128, pattern=r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$"
    )
    extraction_request_id: str = Field(min_length=1, max_length=128)
    status: Literal["preview_ready", "needs_review", "unavailable"]
    media_type: DocumentAssetMediaType
    byte_size: int = Field(ge=1, le=MAX_DOCUMENT_ASSET_BYTES)
    source_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    canonical_text_sha256: str | None = Field(default=None, pattern=r"^[a-f0-9]{64}$")
    extractor_revision: str | None = Field(default=None, min_length=1, max_length=256)
    pages: list[ExtractionPage] = Field(default_factory=list, max_length=MAX_DOCUMENT_ASSET_PAGES)
    warnings: list[ExtractionWarning] = Field(default_factory=list, max_length=32)

    @model_validator(mode="after")
    def preview_result_is_coherent(self) -> "DocumentExtractionResult":
        page_numbers = [page.page_number for page in self.pages]
        if page_numbers != list(range(1, len(page_numbers) + 1)):
            raise ValueError("extraction page numbers must be contiguous")
        if self.status == "unavailable":
            if self.canonical_text_sha256 is not None or self.extractor_revision or self.pages:
                raise ValueError("an unavailable extraction must not expose preview content")
            return self
        if self.canonical_text_sha256 is None or not self.extractor_revision or not self.pages:
            raise ValueError("an available extraction requires text provenance and pages")
        if self.canonical_text_sha256 != canonical_extraction_text_sha256(self.pages):
            raise ValueError("extraction text receipt does not match preview spans")
        return self


class AgentDocumentExtractionRequest(BaseModel):
    """Backend-owned metadata used to form the raw Agent extraction request.

    The object is intentionally not a JSON wire body.  ``HttpAgentClient``
    places these fields in the fixed request headers and sends only the
    already-validated source bytes as the body.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    document_asset_id: str = Field(
        min_length=1, max_length=128, pattern=r"^[A-Za-z0-9][A-Za-z0-9._:-]*$"
    )
    extraction_request_id: str = Field(
        min_length=1, max_length=128, pattern=r"^[A-Za-z0-9][A-Za-z0-9._:-]*$"
    )
    media_type: DocumentAssetMediaType
    source_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    byte_size: int = Field(ge=1, le=MAX_DOCUMENT_ASSET_BYTES)

    def to_agent_wire(self) -> dict[str, str]:
        return {
            "document_asset_id": self.document_asset_id,
            "extraction_request_id": self.extraction_request_id,
            "source_sha256": self.source_sha256,
            "media_type": self.media_type,
        }


class ExtractionJobRecord(BaseModel):
    model_config = ConfigDict(extra="forbid")

    extraction_job_id: str = Field(
        min_length=1, max_length=128, pattern=r"^[A-Za-z0-9][A-Za-z0-9._:-]*$"
    )
    asset_id: str = Field(min_length=1, max_length=128)
    case_id: str = Field(min_length=1, max_length=128)
    org_id: str = Field(min_length=1, max_length=128)
    status: DocumentExtractionStatus
    result: DocumentExtractionResult | None = None
    error_code: str | None = Field(default=None, min_length=1, max_length=128)
    created_by: str = Field(min_length=1, max_length=256)
    created_at: datetime
    updated_at: datetime

    @model_validator(mode="after")
    def job_result_matches_status(self) -> "ExtractionJobRecord":
        if self.status in {"preview_ready", "needs_review", "unavailable"} and self.result is None:
            raise ValueError("a completed extraction job requires a result")
        if self.status in {"queued", "failed"} and self.result is not None:
            raise ValueError("a queued or failed extraction job must not contain a result")
        return self


class RasterExtractionPoint(BaseModel):
    """One normalized point in the source page coordinate system."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    x: float = Field(ge=0, le=1)
    y: float = Field(ge=0, le=1)


class RasterExtractionPolygon(BaseModel):
    """Optional provider geometry retained alongside the stable bounding box."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    points: list[RasterExtractionPoint] = Field(min_length=4, max_length=32)


class RasterExtractionSpan(BaseModel):
    """A raster OCR candidate; it never authorises an automatic decision."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    span_id: str = Field(
        min_length=1, max_length=128, pattern=r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$"
    )
    ordinal: int = Field(ge=1, le=100_000)
    text: str = Field(min_length=1, max_length=8_000)
    bbox: ExtractionBoundingBox | None = None
    polygon: RasterExtractionPolygon | None = None
    confidence: float = Field(ge=0, le=1)
    reading_order: int = Field(ge=1, le=100_000)


class RasterExtractionPage(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    page_number: int = Field(ge=1, le=MAX_DOCUMENT_ASSET_PAGES)
    width: int = Field(ge=1, le=100_000)
    height: int = Field(ge=1, le=100_000)
    coordinate_unit: Literal["pixels"]
    spans: list[RasterExtractionSpan] = Field(default_factory=list, max_length=20_000)

    @model_validator(mode="after")
    def span_identity_and_order_are_consistent(self) -> "RasterExtractionPage":
        span_ids = [span.span_id for span in self.spans]
        ordinals = [span.ordinal for span in self.spans]
        reading_order = [span.reading_order for span in self.spans]
        if (
            len(span_ids) != len(set(span_ids))
            or len(ordinals) != len(set(ordinals))
            or len(reading_order) != len(set(reading_order))
            or ordinals != sorted(ordinals)
            or reading_order != sorted(reading_order)
        ):
            raise ValueError("raster extraction spans are not uniquely ordered")
        return self


class RasterFieldCandidate(BaseModel):
    """One evidence-linked field candidate for the reviewer workbench."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    field_id: str = Field(
        min_length=1, max_length=128, pattern=r"^[A-Za-z][A-Za-z0-9._:-]{0,127}$"
    )
    value: str = Field(min_length=1, max_length=8_000)
    confidence: float = Field(ge=0, le=1)
    evidence_span_ids: list[str] = Field(min_length=1, max_length=256)
    bbox: ExtractionBoundingBox | None = None


class RasterTableCell(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    cell_id: str = Field(
        min_length=1, max_length=128, pattern=r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$"
    )
    row_index: int = Field(ge=0, le=100_000)
    column_index: int = Field(ge=0, le=100_000)
    text: str = Field(min_length=1, max_length=8_000)
    bbox: ExtractionBoundingBox | None = None
    confidence: float = Field(ge=0, le=1)
    evidence_span_ids: list[str] = Field(min_length=1, max_length=256)


class RasterTableRow(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    row_index: int = Field(ge=0, le=100_000)
    cells: list[RasterTableCell] = Field(min_length=1, max_length=1_000)

    @model_validator(mode="after")
    def cell_coordinates_match_row(self) -> "RasterTableRow":
        columns = [cell.column_index for cell in self.cells]
        if any(cell.row_index != self.row_index for cell in self.cells) or len(columns) != len(
            set(columns)
        ):
            raise ValueError("raster table row cells are inconsistent")
        return self


class RasterTable(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    table_id: str = Field(
        min_length=1, max_length=128, pattern=r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$"
    )
    page_number: int = Field(ge=1, le=MAX_DOCUMENT_ASSET_PAGES)
    bbox: ExtractionBoundingBox | None = None
    rows: list[RasterTableRow] = Field(min_length=1, max_length=10_000)

    @model_validator(mode="after")
    def rows_are_unique_and_ordered(self) -> "RasterTable":
        row_indexes = [row.row_index for row in self.rows]
        if len(row_indexes) != len(set(row_indexes)) or row_indexes != sorted(row_indexes):
            raise ValueError("raster table rows are not uniquely ordered")
        return self


class RasterExtractionWarning(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    code: str = Field(min_length=2, max_length=128, pattern=r"^[a-z][a-z0-9_]{1,127}$")


def canonical_raster_extraction_text_sha256(pages: list[RasterExtractionPage]) -> str:
    """Bind the visible raster spans to the Agent text provenance receipt."""

    text = "\n".join(
        unicodedata.normalize("NFC", span.text).replace("\r\n", "\n").replace("\r", "\n")
        for page in sorted(pages, key=lambda item: item.page_number)
        for span in sorted(page.spans, key=lambda item: (item.reading_order, item.ordinal))
    )
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


class RasterDocumentExtractionResult(BaseModel):
    """Versioned human-review-only OCR/layout projection for raster inputs."""

    model_config = ConfigDict(extra="forbid")

    schema_version: Literal["2.0"] = "2.0"
    document_asset_id: str = Field(
        min_length=1, max_length=128, pattern=r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$"
    )
    extraction_request_id: str = Field(min_length=1, max_length=128)
    media_type: DocumentAssetMediaType
    byte_size: int = Field(ge=1, le=MAX_DOCUMENT_ASSET_BYTES)
    source_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    raster_profile_revision: str = Field(
        min_length=1, max_length=128, pattern=r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$"
    )
    status: Literal["preview_ready", "needs_review", "unavailable"]
    canonical_text_sha256: str | None = Field(default=None, pattern=r"^[a-f0-9]{64}$")
    extractor_revision: str | None = Field(default=None, min_length=1, max_length=256)
    document_type: RasterDocumentType
    classification_confidence: float | None = Field(default=None, ge=0, le=1)
    pages: list[RasterExtractionPage] = Field(default_factory=list, max_length=MAX_DOCUMENT_ASSET_PAGES)
    field_candidates: list[RasterFieldCandidate] = Field(default_factory=list, max_length=256)
    tables: list[RasterTable] = Field(default_factory=list, max_length=100)
    warnings: list[RasterExtractionWarning] = Field(default_factory=list, max_length=32)
    needs_review_reasons: list[RasterExtractionWarning] = Field(default_factory=list, max_length=32)

    @model_validator(mode="after")
    def preview_result_is_coherent(self) -> "RasterDocumentExtractionResult":
        page_numbers = [page.page_number for page in self.pages]
        if page_numbers != list(range(1, len(page_numbers) + 1)):
            raise ValueError("raster extraction page numbers must be contiguous")
        span_ids = {span.span_id for page in self.pages for span in page.spans}
        if self.status == "unavailable":
            if any(
                (
                    self.canonical_text_sha256 is not None,
                    self.extractor_revision is not None,
                    self.classification_confidence is not None,
                    self.pages,
                    self.field_candidates,
                    self.tables,
                )
            ):
                raise ValueError("an unavailable raster extraction must not expose preview content")
            return self
        has_content = bool(self.pages or self.field_candidates or self.tables)
        has_text_provenance = (
            self.canonical_text_sha256 is not None or self.extractor_revision is not None
        )
        # A pre-OCR safety stop (for example a JPEG with EXIF rotation) is a
        # genuine reviewer-held ``needs_review`` outcome, not an unavailable
        # engine or a failed job.  It deliberately contains no extracted text
        # or geometry, so it must not be forced through the contentful-preview
        # provenance invariant below.
        if not has_content:
            if has_text_provenance:
                raise ValueError("an empty raster extraction must not claim text provenance")
            if self.status == "preview_ready":
                raise ValueError("a preview-ready raster extraction requires positioned spans")
            if self.status == "needs_review" and not self.needs_review_reasons:
                raise ValueError("an empty needs-review raster extraction requires a reason")
            return self
        if self.canonical_text_sha256 is None or not self.extractor_revision or not self.pages:
            raise ValueError("a contentful raster extraction requires text provenance and pages")
        if self.canonical_text_sha256 != canonical_raster_extraction_text_sha256(self.pages):
            raise ValueError("raster extraction text receipt does not match preview spans")
        if any(table.page_number not in page_numbers for table in self.tables):
            raise ValueError("raster extraction table page is outside the source")
        evidence = [
            evidence_span_id
            for candidate in self.field_candidates
            for evidence_span_id in candidate.evidence_span_ids
        ] + [
            evidence_span_id
            for table in self.tables
            for row in table.rows
            for cell in row.cells
            for evidence_span_id in cell.evidence_span_ids
        ]
        if any(evidence_span_id not in span_ids for evidence_span_id in evidence):
            raise ValueError("raster extraction evidence does not reference a visible span")
        if self.status == "preview_ready":
            if (
                self.document_type == "unknown"
                or self.classification_confidence is None
                or self.needs_review_reasons
                or any(span.bbox is None for page in self.pages for span in page.spans)
                or any(candidate.bbox is None for candidate in self.field_candidates)
                or any(
                    table.bbox is None or any(cell.bbox is None for row in table.rows for cell in row.cells)
                    for table in self.tables
                )
            ):
                raise ValueError("a preview-ready raster extraction requires complete geometry")
        return self


class AgentRasterDocumentExtractionRequest(BaseModel):
    """Header metadata for one raw raster OCR/layout request to Agent v3."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    document_asset_id: str = Field(
        min_length=1, max_length=128, pattern=r"^[A-Za-z0-9][A-Za-z0-9._:-]*$"
    )
    extraction_request_id: str = Field(
        min_length=1, max_length=128, pattern=r"^[A-Za-z0-9][A-Za-z0-9._:-]*$"
    )
    media_type: DocumentAssetMediaType
    source_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    byte_size: int = Field(ge=1, le=MAX_DOCUMENT_ASSET_BYTES)
    raster_profile_revision: str = Field(
        min_length=1, max_length=128, pattern=r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$"
    )

    def to_agent_wire(self) -> dict[str, str]:
        return {
            "document_asset_id": self.document_asset_id,
            "extraction_request_id": self.extraction_request_id,
            "source_sha256": self.source_sha256,
            "media_type": self.media_type,
            "raster_profile_revision": self.raster_profile_revision,
        }


class RasterDocumentPreviewStatus(BaseModel):
    """The currently usable raster profile, without implementation detail.

    This is intentionally a small internal Agent contract.  It tells Backend
    whether a profile can be pinned for a new job, but never reveals pointer
    paths, hashes, model names, or why an unavailable profile failed.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal["1.0"] = "1.0"
    availability: Literal["available", "unavailable"]
    raster_profile_revision: str | None = Field(
        default=None,
        min_length=1,
        max_length=128,
        pattern=r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$",
    )

    @model_validator(mode="after")
    def availability_matches_profile_revision(self) -> "RasterDocumentPreviewStatus":
        if (self.availability == "available") != (self.raster_profile_revision is not None):
            raise ValueError("raster preview availability does not match its profile revision")
        return self


class RasterDocumentPageRender(BaseModel):
    """One ephemeral, provenance-bound PNG page for the reviewer overlay.

    This internal model must never become a public JSON response.  The
    Backend verifies every Agent response binding before returning only the
    bytes through its authenticated, no-store image endpoint.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    page_number: int = Field(ge=1, le=MAX_DOCUMENT_ASSET_PAGES)
    width: int = Field(ge=1, le=100_000)
    height: int = Field(ge=1, le=100_000)
    media_type: Literal["image/png"]
    content: bytes = Field(min_length=1, max_length=MAX_RASTER_VIEWER_PAGE_BYTES)
    content_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    source_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    raster_profile_revision: str = Field(
        min_length=1, max_length=128, pattern=r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$"
    )
    renderer_revision: str = Field(
        min_length=1, max_length=128, pattern=r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$"
    )

    @model_validator(mode="after")
    def content_matches_declared_digest(self) -> "RasterDocumentPageRender":
        if hashlib.sha256(self.content).hexdigest() != self.content_sha256:
            raise ValueError("raster viewer page digest does not match content")
        png_width, png_height = _png_ihdr_dimensions(self.content)
        if (png_width, png_height) != (self.width, self.height):
            raise ValueError("raster viewer page dimensions do not match PNG IHDR")
        return self


class RasterExtractionJobRecord(BaseModel):
    """Persisted queue record; leases make restarts and retries fail closed."""

    model_config = ConfigDict(extra="forbid")

    raster_extraction_job_id: str = Field(
        min_length=1, max_length=128, pattern=r"^[A-Za-z0-9][A-Za-z0-9._:-]*$"
    )
    asset_id: str = Field(min_length=1, max_length=128)
    case_id: str = Field(min_length=1, max_length=128)
    org_id: str = Field(min_length=1, max_length=128)
    source_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    profile_revision: str = Field(
        min_length=1, max_length=128, pattern=r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$"
    )
    status: RasterDocumentExtractionStatus
    attempt_count: int = Field(default=0, ge=0, le=3)
    lease_token: str | None = Field(default=None, min_length=1, max_length=128)
    lease_expires_at: datetime | None = None
    result: RasterDocumentExtractionResult | None = None
    error_code: str | None = Field(default=None, min_length=1, max_length=128)
    created_by: str = Field(min_length=1, max_length=256)
    created_at: datetime
    updated_at: datetime

    @model_validator(mode="after")
    def job_result_matches_status(self) -> "RasterExtractionJobRecord":
        terminal_result = {"preview_ready", "needs_review", "unavailable"}
        if self.status in terminal_result and self.result is None:
            raise ValueError("a completed raster extraction job requires a result")
        if self.status in {"queued", "running", "failed"} and self.result is not None:
            raise ValueError("a non-completed raster extraction job must not contain a result")
        if self.status == "running" and (self.lease_token is None or self.lease_expires_at is None):
            raise ValueError("a running raster extraction job requires a lease")
        if self.status != "running" and (
            self.lease_token is not None or self.lease_expires_at is not None
        ):
            raise ValueError("only a running raster extraction job may retain a lease")
        if self.result is not None and (
            self.result.document_asset_id != self.asset_id
            or self.result.source_sha256 != self.source_sha256
            or self.result.raster_profile_revision != self.profile_revision
        ):
            raise ValueError("raster extraction result provenance does not match its job")
        return self


class RasterTextExtractionPage(BaseModel):
    """One page of text-only OCR output.

    This deliberately has no layout, field, table, cell, or coordinate
    claims.  The text remains a reviewer-editable draft, not a review input.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    page_number: int = Field(ge=1, le=MAX_DOCUMENT_ASSET_PAGES)
    text: str = Field(min_length=1, max_length=MAX_TEXT_DOCUMENT_CHARACTERS)
    confidence: float | None = Field(default=None, ge=0, le=1)


def canonical_raster_text_extraction_text(pages: list[RasterTextExtractionPage]) -> str:
    """Return the exact reviewer-visible text receipt for text-only OCR.

    Form-feed separators retain the page boundary without implying spatial
    structure.  Normalising here also makes the Agent and Backend reject a
    result whose text digest was calculated over a different representation.
    """

    return "\n\f\n".join(
        unicodedata.normalize("NFC", page.text).replace("\r\n", "\n").replace("\r", "\n")
        for page in sorted(pages, key=lambda item: item.page_number)
    )


class RasterTextDocumentExtractionResult(BaseModel):
    """Versioned, text-only OCR preview for a human to edit before review."""

    model_config = ConfigDict(extra="forbid")

    schema_version: Literal["1.0"] = "1.0"
    document_asset_id: str = Field(
        min_length=1, max_length=128, pattern=r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$"
    )
    extraction_request_id: str = Field(min_length=1, max_length=128)
    media_type: DocumentAssetMediaType
    byte_size: int = Field(ge=1, le=MAX_DOCUMENT_ASSET_BYTES)
    source_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    raster_text_profile_revision: str = Field(
        min_length=1, max_length=128, pattern=r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$"
    )
    status: Literal["preview_ready", "needs_review", "unavailable"]
    canonical_text: str | None = Field(default=None, max_length=MAX_TEXT_DOCUMENT_CHARACTERS)
    canonical_text_sha256: str | None = Field(default=None, pattern=r"^[a-f0-9]{64}$")
    extractor_revision: str | None = Field(default=None, min_length=1, max_length=256)
    pages: list[RasterTextExtractionPage] = Field(
        default_factory=list, max_length=MAX_DOCUMENT_ASSET_PAGES
    )
    warnings: list[RasterExtractionWarning] = Field(default_factory=list, max_length=32)
    needs_review_reasons: list[RasterExtractionWarning] = Field(default_factory=list, max_length=32)

    @model_validator(mode="after")
    def text_preview_is_coherent(self) -> "RasterTextDocumentExtractionResult":
        page_numbers = [page.page_number for page in self.pages]
        if page_numbers != list(range(1, len(page_numbers) + 1)):
            raise ValueError("raster text extraction page numbers must be contiguous")
        has_content = bool(self.pages)
        has_provenance = (
            self.canonical_text is not None
            or self.canonical_text_sha256 is not None
            or self.extractor_revision is not None
        )
        if self.status == "unavailable":
            if has_content or has_provenance or self.needs_review_reasons:
                raise ValueError("an unavailable raster text extraction must not expose preview content")
            return self
        if not has_content:
            if has_provenance:
                raise ValueError("an empty raster text extraction must not claim text provenance")
            if self.status == "preview_ready":
                raise ValueError("a preview-ready raster text extraction requires text pages")
            if not self.needs_review_reasons:
                raise ValueError("an empty raster text extraction requires a review reason")
            return self
        if (
            self.canonical_text is None
            or self.canonical_text_sha256 is None
            or self.extractor_revision is None
        ):
            raise ValueError("a contentful raster text extraction requires text provenance")
        expected_text = canonical_raster_text_extraction_text(self.pages)
        if self.canonical_text != expected_text:
            raise ValueError("raster text extraction receipt does not match page text")
        if len(expected_text.encode("utf-8")) > MAX_TEXT_DOCUMENT_UTF8_BYTES:
            raise ValueError("raster text extraction exceeds the text confirmation safety limit")
        if hashlib.sha256(expected_text.encode("utf-8")).hexdigest() != self.canonical_text_sha256:
            raise ValueError("raster text extraction digest does not match preview text")
        if self.status == "needs_review" and not self.needs_review_reasons:
            raise ValueError("a needs-review raster text extraction requires a reason")
        if self.status == "preview_ready" and self.needs_review_reasons:
            raise ValueError("a preview-ready raster text extraction cannot require review")
        return self


class AgentRasterTextDocumentExtractionRequest(BaseModel):
    """Header metadata for one raw raster text OCR request to Agent v3."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    document_asset_id: str = Field(
        min_length=1, max_length=128, pattern=r"^[A-Za-z0-9][A-Za-z0-9._:-]*$"
    )
    extraction_request_id: str = Field(
        min_length=1, max_length=128, pattern=r"^[A-Za-z0-9][A-Za-z0-9._:-]*$"
    )
    media_type: DocumentAssetMediaType
    source_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    byte_size: int = Field(ge=1, le=MAX_DOCUMENT_ASSET_BYTES)
    raster_text_profile_revision: str = Field(
        min_length=1, max_length=128, pattern=r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$"
    )

    def to_agent_wire(self) -> dict[str, str]:
        return {
            "document_asset_id": self.document_asset_id,
            "extraction_request_id": self.extraction_request_id,
            "source_sha256": self.source_sha256,
            "media_type": self.media_type,
            "raster_text_profile_revision": self.raster_text_profile_revision,
        }


class RasterTextDocumentPreviewStatus(BaseModel):
    """Current activation state for the separate text-only raster profile."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal["1.0"] = "1.0"
    availability: Literal["available", "unavailable"]
    raster_text_profile_revision: str | None = Field(
        default=None,
        min_length=1,
        max_length=128,
        pattern=r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$",
    )

    @model_validator(mode="after")
    def availability_matches_profile_revision(self) -> "RasterTextDocumentPreviewStatus":
        if (self.availability == "available") != (self.raster_text_profile_revision is not None):
            raise ValueError("raster text preview availability does not match its profile revision")
        return self


class RasterTextExtractionJobRecord(BaseModel):
    """Persisted text-only queue record with a profile-pinned lease."""

    model_config = ConfigDict(extra="forbid")

    raster_text_extraction_job_id: str = Field(
        min_length=1, max_length=128, pattern=r"^[A-Za-z0-9][A-Za-z0-9._:-]*$"
    )
    asset_id: str = Field(min_length=1, max_length=128)
    case_id: str = Field(min_length=1, max_length=128)
    org_id: str = Field(min_length=1, max_length=128)
    source_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    profile_revision: str = Field(
        min_length=1, max_length=128, pattern=r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$"
    )
    status: RasterTextDocumentExtractionStatus
    attempt_count: int = Field(default=0, ge=0, le=3)
    lease_token: str | None = Field(default=None, min_length=1, max_length=128)
    lease_expires_at: datetime | None = None
    result: RasterTextDocumentExtractionResult | None = None
    error_code: str | None = Field(default=None, min_length=1, max_length=128)
    created_by: str = Field(min_length=1, max_length=256)
    created_at: datetime
    updated_at: datetime

    @model_validator(mode="after")
    def job_result_matches_status(self) -> "RasterTextExtractionJobRecord":
        terminal_result = {"preview_ready", "needs_review", "unavailable"}
        if self.status in terminal_result and self.result is None:
            raise ValueError("a completed raster text extraction job requires a result")
        if self.status in {"queued", "running", "failed"} and self.result is not None:
            raise ValueError("a non-completed raster text extraction job must not contain a result")
        if self.status == "running" and (self.lease_token is None or self.lease_expires_at is None):
            raise ValueError("a running raster text extraction job requires a lease")
        if self.status != "running" and (
            self.lease_token is not None or self.lease_expires_at is not None
        ):
            raise ValueError("only a running raster text extraction job may retain a lease")
        if self.result is not None and (
            self.result.document_asset_id != self.asset_id
            or self.result.source_sha256 != self.source_sha256
            or self.result.raster_text_profile_revision != self.profile_revision
        ):
            raise ValueError("raster text extraction result provenance does not match its job")
        return self


class ConfirmedExtractedTextCreate(BaseModel):
    """Human-confirmed text may enter the existing text-review workflow."""

    model_config = ConfigDict(extra="forbid")

    # Retain the v1 name while allowing the raster v2 job to use the same
    # human-confirmation endpoint.  Exactly one value is accepted so a
    # browser cannot accidentally bind text to two previews.
    extraction_job_id: str | None = Field(
        default=None,
        min_length=1,
        max_length=128,
        pattern=r"^[A-Za-z0-9][A-Za-z0-9._:-]*$",
    )
    raster_extraction_job_id: str | None = Field(
        default=None,
        min_length=1,
        max_length=128,
        pattern=r"^[A-Za-z0-9][A-Za-z0-9._:-]*$",
    )
    raster_text_extraction_job_id: str | None = Field(
        default=None,
        min_length=1,
        max_length=128,
        pattern=r"^[A-Za-z0-9][A-Za-z0-9._:-]*$",
    )
    content_text: str = Field(min_length=1, max_length=MAX_TEXT_DOCUMENT_CHARACTERS)
    filename: str = Field(min_length=1, max_length=255)
    replaces_document_id: str | None = Field(
        default=None,
        min_length=1,
        max_length=128,
        pattern=r"^[A-Za-z0-9][A-Za-z0-9._:-]*$",
    )

    @field_validator("content_text")
    @classmethod
    def bounded_utf8_payload(cls, value: str) -> str:
        if len(value.encode("utf-8")) > MAX_TEXT_DOCUMENT_UTF8_BYTES:
            raise ValueError("confirmed extracted text exceeds the UTF-8 byte safety limit")
        return value

    @model_validator(mode="after")
    def exactly_one_extraction_job_is_selected(self) -> "ConfirmedExtractedTextCreate":
        selected = sum(
            value is not None
            for value in (
                self.extraction_job_id,
                self.raster_extraction_job_id,
                self.raster_text_extraction_job_id,
            )
        )
        if selected != 1:
            raise ValueError("exactly one extraction job identifier is required")
        return self


class ReviewEnqueueRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    review_as_of: date | None = None
    trade_type: CaseTradeType = "import"


class JobRecord(BaseModel):
    job_id: str
    case_id: str
    org_id: str
    idempotency_key: str
    idempotency_request_digest: str = Field(pattern=r"^[a-f0-9]{64}$")
    request_digest: str = Field(pattern=r"^[a-f0-9]{64}$")
    case_revision: int = Field(ge=0)
    request: dict[str, Any]
    status: JobStatus
    review_run_id: str
    error_code: str | None = None
    created_by: str
    created_at: datetime
    updated_at: datetime


class CompletionEligibility(BaseModel):
    model_config = ConfigDict(extra="forbid")

    eligible: Literal[False] = False
    unmet_reason_codes: list[str] = Field(min_length=1, max_length=20)


class RequiredAction(BaseModel):
    model_config = ConfigDict(extra="forbid")

    action_code: str = Field(min_length=1, max_length=128)
    message: str = Field(min_length=1, max_length=4000)
    finding_ids: list[str] = Field(default_factory=list, max_length=2000)


class FindingDecisionStatus(str, Enum):
    confirmed = "confirmed"
    needs_information = "needs_information"
    resolved = "resolved"


class FindingDecisionReasonCode(str, Enum):
    """Coded reviewer rationale so decisions stay comparable across cases.

    The free-text ``reason`` remains the reviewer's own words; the code makes
    the recurring rationale countable without parsing prose.  ``other`` is the
    escape hatch and is the only code that insists on free text.
    """

    document_verified = "document_verified"
    notation_difference = "notation_difference"
    supplement_requested = "supplement_requested"
    correction_proposed = "correction_proposed"
    requirement_satisfied = "requirement_satisfied"
    requirement_not_applicable = "requirement_not_applicable"
    other = "other"


# Canonical reviewer-facing wording for each code.  The Backend stores this as
# the decision reason when the reviewer picks a preset without adding words, so
# every stored decision still carries readable prose.
FINDING_DECISION_REASON_LABELS: dict[FindingDecisionReasonCode, str] = {
    FindingDecisionReasonCode.document_verified: "원본 서류 대조 결과 이상 없음",
    FindingDecisionReasonCode.notation_difference: "표기·단위·반올림 차이로 실질 불일치 아님",
    FindingDecisionReasonCode.supplement_requested: "신고인에게 보완 자료 요청",
    FindingDecisionReasonCode.correction_proposed: "정정 제안으로 처리",
    FindingDecisionReasonCode.requirement_satisfied: "법령 요건 충족 확인",
    FindingDecisionReasonCode.requirement_not_applicable: "해당 요건 미적용 대상",
    FindingDecisionReasonCode.other: "기타",
}


class FindingDecisionRequest(BaseModel):
    """A reviewer-owned decision which never changes the Agent result."""

    model_config = ConfigDict(extra="forbid")

    status: FindingDecisionStatus
    reason_code: FindingDecisionReasonCode | None = None
    reason: str = Field(default="", max_length=4000)

    @model_validator(mode="after")
    def require_reason_or_preset(self) -> FindingDecisionRequest:
        preset = (
            self.reason_code is not None and self.reason_code != FindingDecisionReasonCode.other
        )
        if not preset and not self.reason.strip():
            raise ValueError("a reviewer decision reason is required")
        return self

    def stored_reason(self) -> str:
        """The prose the Backend records: the reviewer's words, else the preset label."""

        text = self.reason.strip()
        if text:
            return text
        assert self.reason_code is not None  # the validator guarantees a preset here
        return FINDING_DECISION_REASON_LABELS[self.reason_code]


class FindingDecisionHistoryEntry(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    status: FindingDecisionStatus
    reason_code: FindingDecisionReasonCode | None = None
    reason: str = Field(min_length=1, max_length=4000)
    decided_by: str = Field(min_length=1, max_length=256)
    decided_at: datetime


class FindingDecisionRecord(BaseModel):
    """Current finding decision plus every prior reviewer decision."""

    model_config = ConfigDict(extra="forbid")

    finding_id: str = Field(min_length=1, max_length=128)
    status: FindingDecisionStatus
    reason_code: FindingDecisionReasonCode | None = None
    reason: str = Field(min_length=1, max_length=4000)
    decided_by: str = Field(min_length=1, max_length=256)
    decided_at: datetime
    history: list[FindingDecisionHistoryEntry] = Field(min_length=1, max_length=100)


class HsSelectionRequest(BaseModel):
    """A reviewer-owned HS code confirmation which never changes the Agent result."""

    model_config = ConfigDict(extra="forbid")

    code: str = Field(pattern=r"^[0-9]{10}$")
    reason: str = Field(min_length=1, max_length=4000)

    @field_validator("reason")
    @classmethod
    def non_blank_reason(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("an HS selection reason is required")
        return value


class HsSelectionHistoryEntry(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    code: str = Field(pattern=r"^[0-9]{10}$")
    reason: str = Field(min_length=1, max_length=4000)
    selected_by: str = Field(min_length=1, max_length=256)
    selected_at: datetime


class HsSelectionRecord(BaseModel):
    """Current reviewer HS code selection plus every prior selection."""

    model_config = ConfigDict(extra="forbid")

    code: str = Field(pattern=r"^[0-9]{10}$")
    reason: str = Field(min_length=1, max_length=4000)
    selected_by: str = Field(min_length=1, max_length=256)
    selected_at: datetime
    history: list[HsSelectionHistoryEntry] = Field(min_length=1, max_length=100)


class ConversationCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    title: str | None = Field(default=None, max_length=200)

    @field_validator("title")
    @classmethod
    def normalise_title(cls, value: str | None) -> str | None:
        if value is None:
            return None
        value = value.strip()
        return value or None


class ConversationRecord(BaseModel):
    model_config = ConfigDict(extra="forbid")

    conversation_id: str
    review_run_id: str
    org_id: str
    title: str | None = None
    # A Case conversation may later ask the Agent for V2 official guidance.
    # Keep the immutable selection as Backend state, never as a client-supplied
    # field.  Historic conversations remain readable with both values null.
    snapshot_id: str | None = Field(default=None, min_length=1, max_length=512)
    guidance_registry_version: GuidanceRegistryVersion | None = None
    created_by: str
    created_at: datetime
    updated_at: datetime


class KnowledgeConversationCreate(BaseModel):
    """Create a standalone official-trade knowledge conversation.

    It is intentionally not attached to a Case or a Review Run.  A case
    conversation remains the only API that can receive immutable review facts
    or create a correction draft.
    """

    model_config = ConfigDict(extra="forbid")

    title: str | None = Field(default=None, max_length=200)
    trade_type: KnowledgeTradeType = "both"
    as_of: date | None = None

    @field_validator("title")
    @classmethod
    def normalise_title(cls, value: str | None) -> str | None:
        if value is None:
            return None
        value = value.strip()
        return value or None


class KnowledgeConversationRecord(BaseModel):
    """Persisted global guidance thread with no Case/Review Run ownership."""

    model_config = ConfigDict(extra="forbid")

    conversation_id: str
    org_id: str
    title: str | None = None
    trade_type: KnowledgeTradeType
    # The public create request may omit this value.  The API resolves it to
    # the active profile's fixed knowledge date before this record is stored
    # or a request reaches Agent/Data.
    as_of: date
    # The first Agent answer that resolves a snapshot pins this thread to that
    # immutable snapshot.  A later answer may not silently switch sources
    # while keeping the same as-of date.
    snapshot_id: str | None = Field(default=None, min_length=1, max_length=512)
    # Only a V2 value enables the optional Data expected_snapshot_id selector.
    # Older V1 pins are preserved as history but must not be sent to that V2
    # selector, which would otherwise reject a compatible legacy conversation.
    guidance_registry_version: GuidanceRegistryVersion | None = None
    created_by: str
    created_at: datetime
    updated_at: datetime


class ConversationMessageRole(str, Enum):
    user = "user"
    assistant = "assistant"


class AnswerScope(str, Enum):
    case_grounded = "case_grounded"
    evidence_grounded = "evidence_grounded"
    general_customs_guidance = "general_customs_guidance"
    clarification_needed = "clarification_needed"
    insufficient_evidence = "insufficient_evidence"
    out_of_scope = "out_of_scope"


class CitationKind(str, Enum):
    fact = "fact"
    comparison = "comparison"
    evidence = "evidence"
    guidance_evidence = "guidance_evidence"


class MessageCitation(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    kind: CitationKind
    # ``reference_id`` is the retained V3 case-conversation field.  A
    # guidance citation deliberately uses ``source_id`` instead because it
    # refers to an official snapshot source rather than a Case-internal ID.
    reference_id: str | None = Field(default=None, min_length=1, max_length=256)
    source_id: str | None = Field(default=None, min_length=1, max_length=256)
    source_kind: GuidanceSourceKind | None = None
    source_title: str | None = Field(default=None, min_length=1, max_length=1_000)
    source_uri: str | None = Field(default=None, min_length=1, max_length=2_000)
    as_of: date | None = None
    snapshot_id: str | None = Field(default=None, min_length=1, max_length=512)
    coverage: GuidanceCoverage | None = None
    # A two-scope checklist exposes the exact owning official-resolution
    # position.  It is deliberately public, unlike registry_source_id, so the
    # Browser can render citations without crossing scopes.
    resolution_index: int | None = Field(default=None, ge=0, le=1)
    # The registry source is required for new V2 provenance verification but
    # was not part of the original Browser contract.  Keep it in the typed
    # Agent-boundary model and persisted object, while excluding it from all
    # public API serialisation and generated OpenAPI schemas.
    registry_source_id: str | None = Field(
        default=None,
        min_length=3,
        max_length=64,
        pattern=r"^[a-z][a-z0-9_]{2,63}$",
        exclude=True,
    )

    @model_validator(mode="after")
    def validate_reference_shape(self) -> "MessageCitation":
        if self.kind == CitationKind.guidance_evidence:
            if not all(
                (
                    self.source_id,
                    self.source_kind,
                    self.source_title,
                    self.source_uri,
                    self.as_of,
                    self.snapshot_id,
                    self.coverage,
                )
            ):
                raise ValueError("guidance evidence requires official snapshot source metadata")
            if self.reference_id is not None and self.reference_id != self.source_id:
                raise ValueError("guidance evidence reference_id must match source_id when present")
            return self
        if self.reference_id is None:
            raise ValueError("case citation requires reference_id")
        if any(
            value is not None
            for value in (
                self.source_id,
                self.source_kind,
                self.source_title,
                self.source_uri,
                self.as_of,
                self.snapshot_id,
                self.coverage,
                self.resolution_index,
                self.registry_source_id,
            )
        ):
            raise ValueError("case citation cannot contain guidance source metadata")
        return self


class GuidanceResolution(BaseModel):
    """Safe, public snapshot-resolution metadata for one guidance attempt.

    This contains only a fixed topic/scope projection, date, direction, and
    coverage state.  Registry hash and source-internal identifiers never
    appear here; Backend uses its packaged registry projection internally.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    topic: str = Field(min_length=1, max_length=128, pattern=r"^[a-z][a-z0-9_]{0,127}$")
    scope: str = Field(min_length=1, max_length=128, pattern=r"^[a-z][a-z0-9_]{0,127}$")
    requested_direction: GuidanceRequestedDirection
    applicable_directions: list[GuidanceApplicableDirection] = Field(min_length=1, max_length=2)
    coverage: GuidanceCoverage
    snapshot_id: str | None = Field(default=None, min_length=1, max_length=512)
    as_of: date
    registry_version: GuidanceRegistryVersion

    @model_validator(mode="after")
    def validate_applicable_directions(self) -> "GuidanceResolution":
        if len(self.applicable_directions) != len(set(self.applicable_directions)):
            raise ValueError("guidance applicable directions must be unique")
        canonical_directions = [
            direction
            for direction in ("import", "export")
            if direction in self.applicable_directions
        ]
        if self.applicable_directions != canonical_directions:
            raise ValueError("guidance applicable directions must be canonical")
        if self.coverage in {"covered", "partial"} and self.snapshot_id is None:
            raise ValueError("covered guidance resolution requires a snapshot")
        return self


class AnswerSectionKind(str, Enum):
    case_analysis = "case_analysis"
    general_guidance = "general_guidance"
    case_application = "case_application"


class AnswerSection(BaseModel):
    """A user-facing answer part, kept alongside the legacy one-string answer."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    kind: AnswerSectionKind
    title: str = Field(min_length=1, max_length=200)
    content: str = Field(min_length=1, max_length=4_000)
    citations: list[MessageCitation] = Field(default_factory=list, max_length=100)
    # In a multi-scope checklist only each general-guidance section owns one
    # index.  Case analysis and the final checklist remain unindexed.
    resolution_index: int | None = Field(default=None, ge=0, le=1)

    @field_validator("title", "content")
    @classmethod
    def non_blank_text(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("answer section text is required")
        return value.strip()


class CorrectionDraft(BaseModel):
    """A non-binding assistant suggestion which a reviewer may copy into a proposal."""

    model_config = ConfigDict(extra="forbid")

    document_id: str = Field(min_length=1, max_length=128)
    item_id: str | None = Field(default=None, max_length=256)
    fact_id: str | None = Field(default=None, max_length=256)
    field_name: str = Field(min_length=1, max_length=64, pattern=r"^[a-z][a-z0-9_]{0,63}$")
    new_value: str = Field(min_length=1, max_length=2000)
    unit: str | None = Field(default=None, max_length=64)
    reason: str = Field(min_length=1, max_length=2000)


class ConversationMessageCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    content: str = Field(min_length=1, max_length=4000)

    @field_validator("content")
    @classmethod
    def non_blank_content(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("message content is required")
        return value.strip()


class ConversationMessageRecord(BaseModel):
    model_config = ConfigDict(extra="forbid")

    message_id: str
    conversation_id: str
    role: ConversationMessageRole
    content: str = Field(min_length=1, max_length=4000)
    answer_scope: AnswerScope | None = None
    citations: list[MessageCitation] = Field(default_factory=list, max_length=100)
    # Optional and additive: existing persisted V3 messages without sections
    # remain valid and retain their legacy direct ``content`` field.
    answer_sections: list[AnswerSection] = Field(default_factory=list, max_length=4)
    suggested_questions: list[str] = Field(default_factory=list, max_length=3)
    correction_draft: CorrectionDraft | None = None
    # Standalone knowledge conversations retain the Agent/Data resolution for
    # every answer, including no-coverage and clarification answers that have
    # no citation to carry it.  These fields stay absent for Case messages.
    knowledge_coverage: GuidanceCoverage | None = None
    knowledge_snapshot_id: str | None = Field(default=None, min_length=1, max_length=512)
    knowledge_as_of: date | None = None
    # Case and standalone knowledge messages use this same safe projection.
    # It stays optional so existing persisted V3 messages remain readable.
    guidance_resolution: GuidanceResolution | None = None
    # Two independently resolved V2 scopes are additive, but never coexist
    # with the legacy singular resolution.  The fixed answer format makes the
    # Browser/API surface unambiguous without broadening ordinary answers.
    guidance_resolutions: list[GuidanceResolution] | None = Field(
        default=None, min_length=2, max_length=2
    )
    answer_format: Literal["multi_scope_checklist"] | None = None
    case_input_notice: CaseInputNotice | None = None
    created_by: str
    created_at: datetime

    @model_validator(mode="after")
    def validate_knowledge_provenance(self) -> "ConversationMessageRecord":
        supplied = (
            self.knowledge_coverage is not None
            or self.knowledge_snapshot_id is not None
            or self.knowledge_as_of is not None
        )
        if supplied and (self.knowledge_coverage is None or self.knowledge_as_of is None):
            raise ValueError("knowledge message provenance requires coverage and as_of")
        if self.guidance_resolution is not None and self.role != ConversationMessageRole.assistant:
            raise ValueError("guidance resolution belongs only to assistant messages")
        if self.guidance_resolutions is not None and self.role != ConversationMessageRole.assistant:
            raise ValueError("guidance resolutions belong only to assistant messages")
        if self.answer_format is not None and self.role != ConversationMessageRole.assistant:
            raise ValueError("answer format belongs only to assistant messages")
        if self.guidance_resolution is not None and self.guidance_resolutions is not None:
            raise ValueError("guidance resolution forms are mutually exclusive")
        if self.guidance_resolutions is None and self.answer_format is not None:
            raise ValueError("multi-scope format requires two guidance resolutions")
        if self.guidance_resolutions is not None:
            if self.answer_format != "multi_scope_checklist":
                raise ValueError("two guidance resolutions require the multi-scope format")
            if any(item.registry_version != "2.0" for item in self.guidance_resolutions):
                raise ValueError("multi-scope guidance requires the V2 registry")
            scopes = {(item.topic, item.scope) for item in self.guidance_resolutions}
            if len(scopes) != 2:
                raise ValueError("multi-scope guidance resolutions must be independent")
        if self.guidance_resolutions is None and any(
            item.resolution_index is not None for item in self.citations
        ):
            raise ValueError("single-scope citations cannot carry a resolution index")
        if self.guidance_resolutions is None and any(
            item.resolution_index is not None for item in self.answer_sections
        ):
            raise ValueError("single-scope sections cannot carry a resolution index")
        if self.case_input_notice is not None:
            if self.role != ConversationMessageRole.assistant:
                raise ValueError("case input notice belongs only to assistant messages")
            if self.answer_scope != AnswerScope.insufficient_evidence:
                raise ValueError("case input notice requires an insufficient-evidence response")
            if (
                self.citations
                or self.answer_sections
                or self.correction_draft is not None
                or self.guidance_resolution is not None
                or self.guidance_resolutions is not None
                or self.answer_format is not None
            ):
                raise ValueError("case input notice cannot create review evidence")
        return self


class ConversationMessageExchange(BaseModel):
    model_config = ConfigDict(extra="forbid")

    user_message: ConversationMessageRecord
    assistant_message: ConversationMessageRecord


class AgentConversationTurn(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    role: ConversationMessageRole
    content: str = Field(min_length=1, max_length=4000)


class AgentAssistantAnswerRequest(BaseModel):
    """Typed internal request.  The canonical JSON schema is packaged separately."""

    model_config = ConfigDict(extra="forbid")

    schema_version: Literal["1.0"] = "1.0"
    case_id: str = Field(min_length=1, max_length=128)
    review_run_id: str = Field(min_length=1, max_length=128)
    conversation_id: str = Field(min_length=1, max_length=128)
    review_result: dict[str, Any]
    question: str = Field(min_length=1, max_length=4000)
    recent_turns: list[AgentConversationTurn] = Field(default_factory=list, max_length=8)
    expected_snapshot_id: str | None = Field(
        default=None,
        min_length=3,
        max_length=81,
        pattern=r"^[a-z0-9][a-z0-9-]{2,80}$",
    )

    def to_agent_wire(self) -> dict[str, Any]:
        return self.model_dump(mode="json", exclude_none=True)


class AgentAssistantAnswerResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: Literal["1.0"]
    case_id: str = Field(min_length=1, max_length=128)
    review_run_id: str = Field(min_length=1, max_length=128)
    conversation_id: str = Field(min_length=1, max_length=128)
    answer_scope: AnswerScope
    direct_answer: str = Field(min_length=1, max_length=4000)
    citations: list[MessageCitation] = Field(default_factory=list, max_length=100)
    answer_sections: list[AnswerSection] = Field(default_factory=list, max_length=4)
    suggested_questions: list[str] = Field(default_factory=list, max_length=3)
    correction_draft: CorrectionDraft | None = None
    guidance_resolution: GuidanceResolution | None = None
    guidance_resolutions: list[GuidanceResolution] | None = Field(
        default=None, min_length=2, max_length=2
    )
    answer_format: Literal["multi_scope_checklist"] | None = None
    case_input_notice: CaseInputNotice | None = None
    # Agent→Backend provenance only.  The Backend compares this against its
    # exact packaged Data manifest before persistence, then excludes it from
    # every Browser-facing DTO/OpenAPI projection.
    guidance_registry_sha256: str | None = Field(
        default=None,
        min_length=64,
        max_length=64,
        pattern=r"^[a-f0-9]{64}$",
        exclude=True,
    )
    engine: dict[str, Any]


class AgentKnowledgeAnswerRequest(BaseModel):
    """One bounded official-snapshot guidance request to the Agent."""

    model_config = ConfigDict(extra="forbid")

    schema_version: Literal["1.0"] = "1.0"
    conversation_id: str = Field(min_length=1, max_length=128)
    question: str = Field(min_length=1, max_length=4_000)
    trade_type: KnowledgeTradeType
    recent_turns: list[AgentConversationTurn] = Field(default_factory=list, max_length=8)
    as_of: date
    expected_snapshot_id: str | None = Field(
        default=None,
        min_length=3,
        max_length=81,
        pattern=r"^[a-z0-9][a-z0-9-]{2,80}$",
    )

    def to_agent_wire(self) -> dict[str, Any]:
        return self.model_dump(mode="json", exclude_none=True)


class AgentKnowledgeAnswerResponse(BaseModel):
    """A general trade answer backed only by official snapshot citations."""

    model_config = ConfigDict(extra="forbid")

    schema_version: Literal["1.0"]
    conversation_id: str = Field(min_length=1, max_length=128)
    answer_scope: AnswerScope
    direct_answer: str = Field(min_length=1, max_length=4_000)
    citations: list[MessageCitation] = Field(default_factory=list, max_length=100)
    answer_sections: list[AnswerSection] = Field(default_factory=list, max_length=2)
    suggested_questions: list[str] = Field(default_factory=list, max_length=3)
    # Data resolves these at query time.  They remain explicit even when no
    # source was returned, so a global conversation cannot silently drift to
    # another snapshot between turns.
    coverage: GuidanceCoverage
    snapshot_id: str | None = Field(default=None, min_length=1, max_length=512)
    as_of: date
    guidance_resolution: GuidanceResolution | None = None
    guidance_resolutions: list[GuidanceResolution] | None = Field(
        default=None, min_length=2, max_length=2
    )
    answer_format: Literal["multi_scope_checklist"] | None = None
    # Internal Agent→Backend registry digest; never copied into a stored
    # conversation message or public response.
    guidance_registry_sha256: str | None = Field(
        default=None,
        min_length=64,
        max_length=64,
        pattern=r"^[a-f0-9]{64}$",
        exclude=True,
    )
    engine: dict[str, Any]


class CorrectionProposalStatus(str, Enum):
    proposed = "proposed"
    approved = "approved"
    rejected = "rejected"


class CorrectionProposalCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    document_id: str = Field(min_length=1, max_length=128)
    field_name: str = Field(min_length=1, max_length=64, pattern=r"^[a-z][a-z0-9_]{0,63}$")
    new_value: str = Field(min_length=1, max_length=2000)
    unit: str | None = Field(default=None, max_length=64)
    fact_id: str | None = Field(default=None, max_length=256)
    item_id: str | None = Field(default=None, max_length=256)
    finding_id: str | None = Field(default=None, max_length=256)
    reason: str = Field(min_length=1, max_length=2000)

    @field_validator("new_value", "reason")
    @classmethod
    def correction_text_is_non_blank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("correction text is required")
        return value.strip()


class CorrectionProposalDecisionCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    status: Literal["approved", "rejected"]
    reason: str = Field(min_length=1, max_length=2000)

    @field_validator("reason")
    @classmethod
    def decision_reason_is_non_blank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("decision reason is required")
        return value.strip()


class CorrectionProposalDecisionRecord(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    status: Literal["approved", "rejected"]
    reason: str = Field(min_length=1, max_length=2000)
    decided_by: str = Field(min_length=1, max_length=256)
    decided_at: datetime


class CorrectionProposalHistoryEntry(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    event: Literal["proposed", "approved", "rejected", "applied"]
    reason: str = Field(min_length=1, max_length=2000)
    actor: str = Field(min_length=1, max_length=256)
    occurred_at: datetime


class CorrectionProposalRecord(BaseModel):
    model_config = ConfigDict(extra="forbid")

    correction_proposal_id: str
    review_run_id: str
    case_id: str
    org_id: str
    parent_result_digest: str = Field(pattern=r"^[a-f0-9]{64}$")
    parent_input_manifest_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    document_id: str
    field_name: str
    new_value: str
    unit: str | None = None
    fact_id: str | None = None
    item_id: str | None = None
    finding_id: str | None = None
    reason: str
    status: CorrectionProposalStatus = CorrectionProposalStatus.proposed
    created_by: str
    created_at: datetime
    decision: CorrectionProposalDecisionRecord | None = None
    applied_review_run_id: str | None = None
    applied_by: str | None = None
    applied_at: datetime | None = None
    history: list[CorrectionProposalHistoryEntry] = Field(min_length=1, max_length=10)


class RevisionCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    proposal_ids: list[str] = Field(min_length=1, max_length=50)

    @field_validator("proposal_ids")
    @classmethod
    def proposal_ids_are_unique(cls, value: list[str]) -> list[str]:
        if len(value) != len(set(value)):
            raise ValueError("proposal_ids must be unique")
        return value


class InputDocumentManifestEntry(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    document_id: str = Field(min_length=1, max_length=128)
    document_type: DocumentType
    content_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")


class TemporalContext(BaseModel):
    """Immutable Evidence snapshot dates supplied by the Agent.

    The values are provenance only.  They do not decide whether an HSK or
    requirement snapshot applies to a particular historical case; the Agent
    has already validated that ``law_as_of`` is bound to the review date.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    law_as_of: date
    hsk_snapshot_as_of: date
    requirements_snapshot_as_of: date


class TemporalCoverage(BaseModel):
    """Explicit Evidence availability for each independently dated source."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    law: Literal["covered", "unavailable"]
    hsk: Literal["covered", "unavailable"]
    requirements: Literal["covered", "unavailable"]


class ReviewRunRecord(BaseModel):
    model_config = ConfigDict(extra="forbid")

    review_run_id: str
    case_id: str
    org_id: str
    job_id: str
    parent_review_run_id: str | None = None
    applied_correction_proposal_ids: list[str] = Field(default_factory=list, max_length=100)
    case_revision: int = Field(ge=0)
    review_as_of: date
    trade_type: Literal["import", "export"]
    input_document_manifest: list[InputDocumentManifestEntry] = Field(min_length=1, max_length=20)
    input_manifest_digest: str = Field(pattern=r"^[a-f0-9]{64}$")
    agent_input_manifest_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    provider_result_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    result_digest: str = Field(pattern=r"^[a-f0-9]{64}$")
    execution_status: ExecutionStatus
    assessment: Assessment
    # This is the Agent contract version, not a Backend API version. Defaults
    # keep historic local functional-UAT state readable after the V3.2 rollout.
    agent_schema_version: str = Field(default="3.0", min_length=1, max_length=32)
    agent_status: str = Field(default="needs_review", min_length=1, max_length=64)
    agent_engine: dict[str, Any] | None = None
    agent_jurisdiction: Literal["KR"] = "KR"
    agent_requires_human_review: Literal[True] = True
    # Additive Evidence provenance.  Historic Agent results did not carry
    # this context and must remain readable as ``null``.
    temporal_context: TemporalContext | None = None
    # Availability is deliberately separate from dates.  A historical request
    # can retain a baseline HSK/CCCT date while safely reporting that its
    # corresponding official archive is unavailable.
    temporal_coverage: TemporalCoverage | None = None
    # Agent 3.3 additions.  Both are advisory: confidence only labels the
    # review track and the verdict never closes a Case.  Historic runs load
    # with ``None``; both stay inside the provider hash when populated.
    agent_confidence: dict[str, Any] | None = None
    agent_eligibility_verdict: dict[str, Any] | None = None
    review_disposition: ReviewDisposition = ReviewDisposition.human_review_required
    assurance_level: AssuranceLevel = AssuranceLevel.diagnostic
    completion_eligibility: CompletionEligibility
    report: dict[str, Any]
    evidence: list[dict[str, Any]]
    findings: list[dict[str, Any]]
    required_actions: list[RequiredAction]
    # V3.2 workflow detail is returned by the Agent and is immutable for a
    # completed run.  It is intentionally separate from reviewer decisions.
    workflow: dict[str, Any] = Field(default_factory=dict)
    document_analyses: list[dict[str, Any]] = Field(default_factory=list)
    comparisons: list[dict[str, Any]] = Field(default_factory=list)
    reviewer_decisions: list[FindingDecisionRecord] = Field(default_factory=list)
    # Reviewer-owned HS code confirmation.  Like reviewer decisions it is
    # Backend state layered next to the Agent result, never inside it, so it
    # must stay out of the provider hash reconstruction.  Historic runs load
    # with no selection.
    hs_selection: HsSelectionRecord | None = None
    # Existing Browser key; it is display-only and never controls completion.
    release_identity: dict[str, Any] = Field(default_factory=dict)
    created_by: str
    created_at: datetime

    @model_validator(mode="after")
    def human_review_only(self) -> "ReviewRunRecord":
        if self.review_disposition != ReviewDisposition.human_review_required:
            raise ValueError("local Backend review runs always require human review")
        if self.assurance_level != AssuranceLevel.diagnostic:
            raise ValueError("local Backend review runs use diagnostic assurance only")
        if self.temporal_coverage is not None and self.temporal_context is None:
            raise ValueError("temporal coverage requires temporal context")
        return self


class AgentLayoutSummary(BaseModel):
    model_config = ConfigDict(extra="forbid")

    page_count: int = Field(ge=0, le=1000)
    table_count: int = Field(ge=0, le=10_000)
    span_count: int = Field(ge=0, le=1_000_000)
    field_candidate_count: int = Field(ge=0, le=100_000)
    mean_confidence: float | None = Field(default=None, ge=0, le=1)


class AgentDocumentExtraction(BaseModel):
    """Provenance of one confirmed text document, forwarded to the Agent."""

    model_config = ConfigDict(extra="forbid")

    source: DocumentExtractionSource
    layout_summary: AgentLayoutSummary | None = None


class AgentDocumentInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    document_id: str = Field(min_length=1, max_length=128)
    filename: str = Field(min_length=1, max_length=255)
    content_text: str = Field(min_length=1, max_length=MAX_TEXT_DOCUMENT_CHARACTERS)
    declared_type: str | None = Field(default=None, max_length=64)
    extraction: AgentDocumentExtraction | None = None

    def to_agent_wire(self) -> dict[str, Any]:
        return self.model_dump(mode="json", exclude_none=True)


class AgentHumanCorrection(BaseModel):
    """Approved reviewer input carried only into a child Agent review pass."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    correction_proposal_id: str = Field(min_length=1, max_length=128)
    document_id: str = Field(min_length=1, max_length=128)
    item_id: str | None = Field(default=None, max_length=256)
    fact_id: str | None = Field(default=None, max_length=256)
    field_name: str = Field(min_length=1, max_length=64, pattern=r"^[a-z][a-z0-9_]{0,63}$")
    new_value: str = Field(min_length=1, max_length=2000)
    unit: str | None = Field(default=None, max_length=64)
    reason: str = Field(min_length=1, max_length=2000)
    source_kind: Literal["human_provided"] = "human_provided"


class AgentReviewerHsSelection(BaseModel):
    """Reviewer-confirmed HSK code carried as labelled human-provided context.

    It never overrides a document-cited HS code; the Agent may use it only
    as the retrieval commodity when no input document cites a code itself.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    code: str = Field(pattern=r"^[0-9]{10}$")
    source_kind: Literal["human_provided"] = "human_provided"


class AgentReviewRequest(BaseModel):
    """The exact compact input sent to the Agent V3.2 endpoint."""

    model_config = ConfigDict(extra="forbid")

    review_run_id: str = Field(
        min_length=1, max_length=128, pattern=r"^[A-Za-z0-9][A-Za-z0-9._:-]*$"
    )
    case_id: str = Field(min_length=1, max_length=128, pattern=r"^[A-Za-z0-9][A-Za-z0-9._:-]*$")
    documents: list[AgentDocumentInput] = Field(min_length=1, max_length=20)
    review_as_of: date
    trade_type: Literal["import", "export"]
    jurisdiction: Literal["KR"] = "KR"
    parent_review_run_id: str | None = Field(default=None, max_length=128)
    applied_correction_proposal_ids: list[str] = Field(default_factory=list, max_length=50)
    human_corrections: list[AgentHumanCorrection] = Field(default_factory=list, max_length=50)
    reviewer_hs_selection: AgentReviewerHsSelection | None = None
    # The parent run's stored document_analyses, so a child run reuses the
    # parent extraction and only appends the approved human facts.
    parent_document_analyses: list[dict[str, Any]] | None = Field(default=None, max_length=20)

    @model_validator(mode="after")
    def correction_context_is_bound(self) -> "AgentReviewRequest":
        correction_ids = [item.correction_proposal_id for item in self.human_corrections]
        if self.parent_review_run_id is None:
            if (
                self.applied_correction_proposal_ids
                or correction_ids
                or self.parent_document_analyses is not None
            ):
                raise ValueError("correction context requires a parent review run")
            return self
        if not self.applied_correction_proposal_ids or not correction_ids:
            raise ValueError("a child review requires approved correction context")
        if len(set(self.applied_correction_proposal_ids)) != len(
            self.applied_correction_proposal_ids
        ):
            raise ValueError("applied correction proposal identifiers must be unique")
        if set(correction_ids) != set(self.applied_correction_proposal_ids):
            raise ValueError(
                "human corrections must exactly match the approved proposal identifiers"
            )
        input_document_ids = {document.document_id for document in self.documents}
        if any(item.document_id not in input_document_ids for item in self.human_corrections):
            raise ValueError("a human correction must target an immutable input document")
        return self

    def to_agent_wire(self) -> dict[str, Any]:
        wire: dict[str, Any] = {
            "review_run_id": self.review_run_id,
            "case_id": self.case_id,
            "documents": [item.to_agent_wire() for item in self.documents],
            "review_as_of": self.review_as_of.isoformat(),
            "trade_type": self.trade_type,
            "jurisdiction": self.jurisdiction,
        }
        if self.parent_review_run_id is not None:
            # Omit unset optional target references to keep correction context
            # compact; the canonical Agent schema also accepts explicit nulls.
            wire.update(
                {
                    "parent_review_run_id": self.parent_review_run_id,
                    "applied_correction_proposal_ids": self.applied_correction_proposal_ids,
                    "human_corrections": [
                        item.model_dump(mode="json", exclude_none=True)
                        for item in self.human_corrections
                    ],
                }
            )
            if self.parent_document_analyses is not None:
                wire["parent_document_analyses"] = self.parent_document_analyses
        if self.reviewer_hs_selection is not None:
            wire["reviewer_hs_selection"] = self.reviewer_hs_selection.model_dump(mode="json")
        return wire

    @property
    def input_manifest_sha256(self) -> str:
        return canonical_digest(self.to_agent_wire())
