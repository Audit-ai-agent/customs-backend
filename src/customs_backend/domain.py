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
DocumentType = Literal["CI", "PL", "BL", "CO", "UNKNOWN"]


class Role(str, Enum):
    reviewer = "reviewer"


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
    # A document is immutable.  A correction creates a new record and marks
    # the earlier record as superseded, so historic review input snapshots
    # remain reproducible.
    replaces_document_id: str | None = None
    superseded_by_document_id: str | None = None
    superseded_at: datetime | None = None


class ReviewEnqueueRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    review_as_of: date | None = None
    trade_type: Literal["import", "export"] = "import"


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


class InputDocumentManifestEntry(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    document_id: str = Field(min_length=1, max_length=128)
    document_type: DocumentType
    content_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")


class ReviewRunRecord(BaseModel):
    model_config = ConfigDict(extra="forbid")

    review_run_id: str
    case_id: str
    org_id: str
    job_id: str
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
    # This is the Agent contract version, not a Backend API version.  Defaults
    # keep historic local functional-UAT state readable after the V3.1 rollout.
    agent_schema_version: str = Field(default="3.0", min_length=1, max_length=32)
    agent_status: str = Field(default="needs_review", min_length=1, max_length=64)
    agent_engine: dict[str, Any] | None = None
    agent_jurisdiction: Literal["KR"] = "KR"
    agent_requires_human_review: Literal[True] = True
    review_disposition: ReviewDisposition = ReviewDisposition.human_review_required
    assurance_level: AssuranceLevel = AssuranceLevel.diagnostic
    completion_eligibility: CompletionEligibility
    report: dict[str, Any]
    evidence: list[dict[str, Any]]
    findings: list[dict[str, Any]]
    required_actions: list[RequiredAction]
    # V3.1 workflow detail is returned by the Agent and is immutable for a
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


class AgentReviewRequest(BaseModel):
    """The exact compact input sent to the Agent V3 endpoint."""

    model_config = ConfigDict(extra="forbid")

    review_run_id: str = Field(
        min_length=1, max_length=128, pattern=r"^[A-Za-z0-9][A-Za-z0-9._:-]*$"
    )
    case_id: str = Field(min_length=1, max_length=128, pattern=r"^[A-Za-z0-9][A-Za-z0-9._:-]*$")
    documents: list[AgentDocumentInput] = Field(min_length=1, max_length=20)
    review_as_of: date
    trade_type: Literal["import", "export"]
    jurisdiction: Literal["KR"] = "KR"

    def to_agent_wire(self) -> dict[str, Any]:
        return self.model_dump(mode="json", exclude_none=True)

    @property
    def input_manifest_sha256(self) -> str:
        return canonical_digest(self.to_agent_wire())
