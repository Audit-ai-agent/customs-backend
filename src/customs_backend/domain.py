"""Small Backend-owned models for local development and functional checks."""

from __future__ import annotations

import hashlib
import json
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
MAX_DOCUMENT_ASSET_BYTES = 20 * 1024 * 1024
MAX_DOCUMENT_ASSET_PAGES = 20
MAX_DOCUMENT_ASSET_PIXELS = 80_000_000
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


class CaseRecord(BaseModel):
    case_id: str
    org_id: str
    title: str
    external_reference: str | None = None
    status: CaseStatus = CaseStatus.open
    revision: int = Field(default=0, ge=0)
    document_manifest_digest: str = Field(pattern=r"^[a-f0-9]{64}$")
    metadata: dict[str, Any] = Field(default_factory=dict)
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
    # A document is immutable.  A correction creates a new record and marks
    # the earlier record as superseded, so historic review input snapshots
    # remain reproducible.
    replaces_document_id: str | None = None
    superseded_by_document_id: str | None = None
    superseded_at: datetime | None = None


DocumentAssetMediaType = Literal["application/pdf", "image/jpeg", "image/png"]
DocumentExtractionStatus = Literal["queued", "preview_ready", "needs_review", "unavailable", "failed"]


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


class ExtractionBoundingBox(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    x: float = Field(ge=0, le=1)
    y: float = Field(ge=0, le=1)
    width: float = Field(gt=0, le=1)
    height: float = Field(gt=0, le=1)


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


class ExtractionWarning(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    code: str = Field(min_length=1, max_length=128, pattern=r"^[a-z][a-z0-9_]{1,127}$")


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
        if self.status == "preview_ready" and (
            self.canonical_text_sha256 is None or not self.extractor_revision or not self.pages
        ):
            raise ValueError("a preview-ready extraction requires text provenance and pages")
        if self.status != "preview_ready" and self.canonical_text_sha256 is not None:
            raise ValueError("a non-preview extraction must not expose canonical text provenance")
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


class ConfirmedExtractedTextCreate(BaseModel):
    """Human-confirmed text may enter the existing text-review workflow."""

    model_config = ConfigDict(extra="forbid")

    extraction_job_id: str = Field(
        min_length=1, max_length=128, pattern=r"^[A-Za-z0-9][A-Za-z0-9._:-]*$"
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


class FindingDecisionRequest(BaseModel):
    """A reviewer-owned decision which never changes the Agent result."""

    model_config = ConfigDict(extra="forbid")

    status: FindingDecisionStatus
    reason: str = Field(min_length=1, max_length=4000)

    @field_validator("reason")
    @classmethod
    def non_blank_reason(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("a reviewer decision reason is required")
        return value


class FindingDecisionHistoryEntry(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    status: FindingDecisionStatus
    reason: str = Field(min_length=1, max_length=4000)
    decided_by: str = Field(min_length=1, max_length=256)
    decided_at: datetime


class FindingDecisionRecord(BaseModel):
    """Current finding decision plus every prior reviewer decision."""

    model_config = ConfigDict(extra="forbid")

    finding_id: str = Field(min_length=1, max_length=128)
    status: FindingDecisionStatus
    reason: str = Field(min_length=1, max_length=4000)
    decided_by: str = Field(min_length=1, max_length=256)
    decided_at: datetime
    history: list[FindingDecisionHistoryEntry] = Field(min_length=1, max_length=100)


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
    applicable_directions: list[GuidanceApplicableDirection] = Field(
        min_length=1, max_length=2
    )
    coverage: GuidanceCoverage
    snapshot_id: str | None = Field(default=None, min_length=1, max_length=512)
    as_of: date
    registry_version: GuidanceRegistryVersion

    @model_validator(mode="after")
    def validate_applicable_directions(self) -> "GuidanceResolution":
        if len(self.applicable_directions) != len(set(self.applicable_directions)):
            raise ValueError("guidance applicable directions must be unique")
        canonical_directions = [
            direction for direction in ("import", "export") if direction in self.applicable_directions
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
    answer_sections: list[AnswerSection] = Field(default_factory=list, max_length=3)
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

    def to_agent_wire(self) -> dict[str, Any]:
        return self.model_dump(mode="json")


class AgentAssistantAnswerResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: Literal["1.0"]
    case_id: str = Field(min_length=1, max_length=128)
    review_run_id: str = Field(min_length=1, max_length=128)
    conversation_id: str = Field(min_length=1, max_length=128)
    answer_scope: AnswerScope
    direct_answer: str = Field(min_length=1, max_length=4000)
    citations: list[MessageCitation] = Field(default_factory=list, max_length=100)
    answer_sections: list[AnswerSection] = Field(default_factory=list, max_length=3)
    suggested_questions: list[str] = Field(default_factory=list, max_length=3)
    correction_draft: CorrectionDraft | None = None
    guidance_resolution: GuidanceResolution | None = None
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

    def to_agent_wire(self) -> dict[str, Any]:
        return self.model_dump(mode="json")


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
        return self


class AgentDocumentInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    document_id: str = Field(min_length=1, max_length=128)
    filename: str = Field(min_length=1, max_length=255)
    content_text: str = Field(min_length=1, max_length=MAX_TEXT_DOCUMENT_CHARACTERS)
    declared_type: str | None = Field(default=None, max_length=64)

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

    @model_validator(mode="after")
    def correction_context_is_bound(self) -> "AgentReviewRequest":
        correction_ids = [item.correction_proposal_id for item in self.human_corrections]
        if self.parent_review_run_id is None:
            if self.applied_correction_proposal_ids or correction_ids:
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
        return wire

    @property
    def input_manifest_sha256(self) -> str:
        return canonical_digest(self.to_agent_wire())
