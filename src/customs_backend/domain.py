from __future__ import annotations

import hashlib
import json
from datetime import UTC, date, datetime
from enum import Enum
from typing import Any, Literal

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, field_validator, model_validator


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
MAX_SUPPORTING_DOCUMENTS_PER_CASE = 100
DocumentType = Literal["CI", "PL", "BL", "CO", "SUPPORTING", "UNKNOWN"]


class Role(str, Enum):
    reviewer = "reviewer"
    approver = "approver"
    auditor = "auditor"
    admin = "admin"


class Principal(BaseModel):
    subject: str = Field(
        min_length=1,
        max_length=256,
        pattern=r"^[A-Za-z0-9][A-Za-z0-9._:@/-]*$",
    )
    org_id: str = Field(
        min_length=1,
        max_length=128,
        pattern=r"^[A-Za-z0-9][A-Za-z0-9._:-]*$",
    )
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
    delivery_uncertain = "delivery_uncertain"
    response_received_rejected = "response_received_rejected"
    response_persistence_uncertain = "response_persistence_uncertain"


class AgentCallState(str, Enum):
    not_started = "not_started"
    started = "started"
    completed = "completed"
    delivery_uncertain = "delivery_uncertain"
    delivery_reconciled = "delivery_reconciled"
    response_received_rejected = "response_received_rejected"
    response_persistence_uncertain = "response_persistence_uncertain"


class ExecutionStatus(str, Enum):
    succeeded = "succeeded"
    partial = "partial"
    failed = "failed"


class Assessment(str, Enum):
    no_issue_detected = "no_issue_detected"
    issues_detected = "issues_detected"
    indeterminate = "indeterminate"


class ReviewDisposition(str, Enum):
    auto_review_complete = "auto_review_complete"
    human_review_required = "human_review_required"
    blocked = "blocked"


class AssuranceLevel(str, Enum):
    diagnostic = "diagnostic"
    production_candidate = "production_candidate"
    production = "production"


class CaseCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    title: str = Field(min_length=1, max_length=200)
    external_reference: str | None = Field(default=None, max_length=200)
    metadata: dict[str, Any] = Field(default_factory=dict)
    assigned_reviewer_subjects: list[str] = Field(default_factory=list, max_length=100)

    @field_validator("assigned_reviewer_subjects")
    @classmethod
    def unique_assignees(cls, values: list[str]) -> list[str]:
        normalized = [value.strip() for value in values]
        if any(not value or len(value) > 256 for value in normalized):
            raise ValueError("assigned reviewer subjects must be 1..256 characters")
        if len(normalized) != len(set(normalized)):
            raise ValueError("assigned reviewer subjects must be unique")
        return sorted(normalized)


class CaseRecord(BaseModel):
    case_id: str
    org_id: str
    title: str
    external_reference: str | None = None
    status: CaseStatus = CaseStatus.open
    revision: int = 0
    document_manifest_digest: str
    metadata: dict[str, Any] = Field(default_factory=dict)
    owner_subject: str = Field(min_length=1, max_length=256)
    assigned_reviewer_subjects: list[str] = Field(default_factory=list, max_length=100)
    created_by: str
    created_at: datetime
    updated_at: datetime


class TextDocumentCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    document_type: DocumentType
    filename: str = Field(min_length=1, max_length=255)
    content_text: str = Field(min_length=1, max_length=MAX_TEXT_DOCUMENT_CHARACTERS)
    media_type: Literal["text/plain"] = "text/plain"

    @field_validator("filename")
    @classmethod
    def text_only_filename(cls, value: str) -> str:
        lowered = value.casefold()
        if lowered.endswith((".pdf", ".png", ".jpg", ".jpeg", ".tif", ".tiff")):
            raise ValueError("visual document input is not released; submit extracted text")
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


class VerificationReceiptRecord(BaseModel):
    """Agent-owned V1 detached receipt wire, persisted without private key material."""

    model_config = ConfigDict(extra="forbid")

    trust_domain: Literal["customs-ai-agent/supporting-document-verification/v1"]
    case_id: str = Field(
        min_length=1,
        max_length=128,
        pattern=r"^[A-Za-z0-9][A-Za-z0-9._:-]*$",
    )
    method: Literal["trusted_connector", "human_approver"]
    authority_id: str = Field(min_length=1, max_length=128)
    authority_release: str = Field(min_length=1, max_length=128)
    key_id: str = Field(
        min_length=1,
        max_length=128,
        pattern=r"^[A-Za-z0-9][A-Za-z0-9._:-]*$",
    )
    supporting_document_claims_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    policy_source_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    policy_release: str = Field(min_length=1, max_length=128)
    decision_record_sha256: str | None = Field(default=None, pattern=r"^[a-f0-9]{64}$")
    connector_result_sha256: str | None = Field(default=None, pattern=r"^[a-f0-9]{64}$")
    signed_at: AwareDatetime
    expires_at: AwareDatetime
    signature_base64url: str = Field(pattern=r"^[A-Za-z0-9_-]{86}$")

    @model_validator(mode="after")
    def method_bound_record_and_time(self) -> "VerificationReceiptRecord":
        if self.expires_at <= self.signed_at:
            raise ValueError("verification receipt expiry must follow its signature time")
        if self.method == "human_approver":
            if self.decision_record_sha256 is None or self.connector_result_sha256 is not None:
                raise ValueError("human verification receipt requires one decision record")
        elif self.connector_result_sha256 is None or self.decision_record_sha256 is not None:
            raise ValueError("connector verification receipt requires one connector result")
        return self

    def unsigned_payload(self) -> dict[str, Any]:
        return self.model_dump(mode="json", exclude={"signature_base64url"})


class SupportingVerificationDecisionRecord(BaseModel):
    """Backend-owned immutable lineage whose digest is embedded in a receipt."""

    model_config = ConfigDict(extra="forbid")

    schema_version: Literal["backend-supporting-document-human-decision/v1"] = (
        "backend-supporting-document-human-decision/v1"
    )
    case_id: str
    document_id: str
    source_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    supporting_document_claims_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    actor_id: str = Field(min_length=1, max_length=256)
    actor_role: Literal["approver"] = "approver"
    policy_source_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    policy_release: str = Field(min_length=1, max_length=128)
    decided_at: AwareDatetime
    decision: Literal["verify_supporting_document"] = "verify_supporting_document"

    @property
    def sha256(self) -> str:
        return canonical_digest(self.model_dump(mode="json"))


class VerificationAssertionRecord(BaseModel):
    model_config = ConfigDict(extra="forbid")

    assertion_id: str = Field(pattern=r"^assert_[a-f0-9]{64}$")
    method: Literal["trusted_connector", "human_approver", "test_issuer"]
    subject_document_id: str = Field(min_length=1, max_length=128)
    asserted_by: str = Field(min_length=1, max_length=256)
    asserted_at: datetime
    authority_id: str = Field(
        min_length=1,
        max_length=128,
        pattern=r"^[A-Za-z0-9][A-Za-z0-9._:/-]*$",
    )
    authority_release: str = Field(min_length=1, max_length=128)
    source_reference_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    receipt: VerificationReceiptRecord | None = None
    decision_record: SupportingVerificationDecisionRecord | None = None

    @model_validator(mode="after")
    def content_bound_identifier(self) -> "VerificationAssertionRecord":
        payload = self.model_dump(
            mode="json",
            exclude={"assertion_id", "decision_record"},
            exclude_none=True,
        )
        receipt = payload.get("receipt")
        if isinstance(receipt, dict):
            receipt.pop("signature_base64url", None)
        if self.assertion_id != f"assert_{canonical_digest(payload)}":
            raise ValueError("verification assertion ID must bind its canonical contents")
        if self.receipt is not None:
            decision_record = self.decision_record
            if decision_record is None:
                raise ValueError("signed assertions require their immutable decision lineage")
            if (
                self.receipt.decision_record_sha256 != decision_record.sha256
                or self.receipt.case_id != decision_record.case_id
                or self.receipt.supporting_document_claims_sha256
                != decision_record.supporting_document_claims_sha256
            ):
                raise ValueError("verification receipt does not bind its Backend decision")
        return self

    def to_agent_wire(self) -> dict[str, Any]:
        return self.model_dump(mode="json", exclude={"decision_record"})


class VerificationAssertionCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    method: Literal["human_approver"] = "human_approver"


class SupportingDocumentCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    document_id: str = Field(
        min_length=1,
        max_length=128,
        pattern=r"^[A-Za-z0-9][A-Za-z0-9._:-]*$",
    )
    document_code: str = Field(
        min_length=1,
        max_length=128,
        pattern=r"^[A-Za-z0-9][A-Za-z0-9._:-]*$",
    )
    issuer: str = Field(min_length=1, max_length=512)
    document_number: str = Field(min_length=1, max_length=256)
    issued_on: date
    valid_until: date | None = None
    subject_item_ids: list[str] = Field(min_length=1, max_length=100)
    hs_codes: list[str] = Field(default_factory=list, max_length=100)
    source_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")

    @field_validator("subject_item_ids", "hs_codes")
    @classmethod
    def unique_references(cls, values: list[str]) -> list[str]:
        normalized = [value.strip() for value in values]
        if any(not value or len(value) > 128 for value in normalized):
            raise ValueError("supporting-document references must be 1..128 characters")
        if len(normalized) != len(set(normalized)):
            raise ValueError("supporting-document references must be unique")
        return sorted(normalized)

    @field_validator("hs_codes")
    @classmethod
    def hsk10_only(cls, values: list[str]) -> list[str]:
        if any(len(value) != 10 or not value.isdigit() for value in values):
            raise ValueError("supporting-document HS codes must be 10-digit HSK values")
        return values

    @model_validator(mode="after")
    def validity_follows_issue_date(self) -> "SupportingDocumentCreate":
        if self.valid_until is not None and self.valid_until < self.issued_on:
            raise ValueError("supporting-document validity cannot precede issue date")
        return self


class SupportingDocumentRecord(SupportingDocumentCreate):
    case_id: str
    org_id: str
    verification_assertions: list[VerificationAssertionRecord] = Field(
        default_factory=list,
        max_length=20,
    )
    created_by: str
    created_at: datetime

    @model_validator(mode="after")
    def assertions_are_unique_and_bound(self) -> "SupportingDocumentRecord":
        assertion_ids = [item.assertion_id for item in self.verification_assertions]
        if len(assertion_ids) != len(set(assertion_ids)):
            raise ValueError("verification assertion IDs must be unique")
        if any(
            item.subject_document_id != self.document_id for item in self.verification_assertions
        ):
            raise ValueError("verification assertion subject must match its document")
        if any(
            item.source_reference_sha256 != self.source_sha256
            for item in self.verification_assertions
        ):
            raise ValueError("verification assertion must bind the supporting-document source")
        if any(
            item.method == "human_approver"
            and (
                not item.authority_id.startswith("backend:principal-sha256:")
                or item.authority_release != "backend-human-decision-policy/v1"
            )
            for item in self.verification_assertions
        ):
            raise ValueError("human assertion must bind the approved Backend authority")
        return self

    def to_agent_wire(self) -> dict[str, Any]:
        return {
            "document_id": self.document_id,
            "document_code": self.document_code,
            "issuer": self.issuer,
            "document_number": self.document_number,
            "issued_on": self.issued_on.isoformat(),
            "valid_until": self.valid_until.isoformat() if self.valid_until else None,
            "subject_item_ids": list(self.subject_item_ids),
            "hs_codes": list(self.hs_codes),
            "source_sha256": self.source_sha256,
            "verification_assertions": [
                item.to_agent_wire() for item in self.verification_assertions
            ],
        }

    @property
    def claims_sha256(self) -> str:
        return canonical_digest(
            self.model_dump(
                mode="json",
                exclude={
                    "case_id",
                    "org_id",
                    "verification_assertions",
                    "created_by",
                    "created_at",
                },
            )
        )


class ReviewEnqueueRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    review_as_of: date | None = None
    trade_type: Literal["import", "export"] = "import"
    submitted_document_codes: list[str] = Field(default_factory=list, max_length=0)

    @field_validator("submitted_document_codes")
    @classmethod
    def normalize_codes(cls, values: list[str]) -> list[str]:
        return sorted({item.strip() for item in values if item.strip()})


class DeliveryReconciliationCreate(BaseModel):
    """Human evidence that an uncertain request was not processed by Agent."""

    model_config = ConfigDict(extra="forbid")

    resolution: Literal["confirmed_not_processed"]
    evidence_reference_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    reason: str = Field(min_length=10, max_length=2_000)


class JobRecord(BaseModel):
    job_id: str
    case_id: str
    org_id: str
    idempotency_key: str
    idempotency_request_digest: str = Field(pattern=r"^[a-f0-9]{64}$")
    request_digest: str = Field(pattern=r"^[a-f0-9]{64}$")
    case_revision: int = Field(ge=0)
    logical_request_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    kind: Literal["review", "revision"] = "review"
    parent_run_id: str | None = None
    request: dict[str, Any] = Field(default_factory=dict)
    status: JobStatus
    review_run_id: str
    error_code: str | None = None
    lease_id: str | None = None
    lease_expires_at: datetime | None = None
    attempt_count: int = Field(default=0, ge=0)
    agent_call_state: AgentCallState = AgentCallState.not_started
    agent_call_endpoint: Literal["review", "revision"] | None = None
    agent_call_request_sha256: str | None = Field(default=None, pattern=r"^[a-f0-9]{64}$")
    agent_call_started_at: datetime | None = None
    delivery_reconciled_by: str | None = None
    delivery_reconciled_at: datetime | None = None
    delivery_reconciliation_evidence_sha256: str | None = Field(
        default=None, pattern=r"^[a-f0-9]{64}$"
    )
    created_by: str
    created_at: datetime
    updated_at: datetime

    @model_validator(mode="after")
    def call_once_state_is_bound(self) -> "JobRecord":
        binding = (
            self.agent_call_endpoint,
            self.agent_call_request_sha256,
            self.agent_call_started_at,
        )
        if self.agent_call_state == AgentCallState.not_started and any(
            item is not None for item in binding
        ):
            raise ValueError("an unstarted Agent call cannot contain delivery binding")
        if self.agent_call_state != AgentCallState.not_started and any(
            item is None for item in binding
        ):
            raise ValueError("a started Agent call requires endpoint, request hash, and time")
        if self.status == JobStatus.succeeded and self.agent_call_state != AgentCallState.completed:
            raise ValueError("a succeeded Job requires a completed Agent call")
        if (
            self.status == JobStatus.delivery_uncertain
            and self.agent_call_state != AgentCallState.delivery_uncertain
        ):
            raise ValueError("a delivery-uncertain Job requires a matching call state")
        if (
            self.status == JobStatus.response_received_rejected
            and self.agent_call_state != AgentCallState.response_received_rejected
        ):
            raise ValueError("a rejected Agent response requires a matching call state")
        if (
            self.agent_call_state == AgentCallState.response_received_rejected
            and self.status != JobStatus.response_received_rejected
        ):
            raise ValueError("a rejected Agent call requires a matching Job status")
        if (
            self.status == JobStatus.response_persistence_uncertain
            and self.agent_call_state != AgentCallState.response_persistence_uncertain
        ):
            raise ValueError("an unpersisted Agent response requires a matching call state")
        if (
            self.agent_call_state == AgentCallState.response_persistence_uncertain
            and self.status != JobStatus.response_persistence_uncertain
        ):
            raise ValueError(
                "an uncertain Agent response persistence requires a matching Job status"
            )
        reconciliation = (
            self.delivery_reconciled_by,
            self.delivery_reconciled_at,
            self.delivery_reconciliation_evidence_sha256,
        )
        if self.agent_call_state == AgentCallState.delivery_reconciled:
            if self.status != JobStatus.failed or any(item is None for item in reconciliation):
                raise ValueError("a reconciled Agent delivery requires complete failure lineage")
        elif any(item is not None for item in reconciliation):
            raise ValueError("delivery reconciliation lineage requires a reconciled call state")
        return self


class CompletionEligibility(BaseModel):
    model_config = ConfigDict(extra="forbid")

    eligible: bool = False
    profile_id: str | None = None
    scope: list[str] = Field(default_factory=list, max_length=100)
    unmet_reason_codes: list[str] = Field(default_factory=list, max_length=100)

    @model_validator(mode="after")
    def consistent(self) -> "CompletionEligibility":
        if self.eligible and (not self.profile_id or self.unmet_reason_codes):
            raise ValueError("eligible completion requires a profile and no unmet reasons")
        if not self.eligible and not self.unmet_reason_codes:
            raise ValueError("ineligible completion requires at least one unmet reason")
        return self


class ReleaseIdentityV3(BaseModel):
    model_config = ConfigDict(extra="forbid")

    agent_package_version: str = Field(min_length=1, max_length=64)
    agent_git_sha: str | None = Field(default=None, pattern=r"^[a-f0-9]{40}$")
    agent_source_snapshot_sha256: str | None = Field(
        default=None,
        pattern=r"^[a-f0-9]{64}$",
    )
    agent_code_snapshot_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    model_id: str = Field(min_length=1, max_length=128)
    model_revision: str | None = Field(default=None, min_length=1, max_length=128)
    extraction_prompt_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    extraction_schema_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    coordinator_prompt_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    narrative_prompt_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    chat_prompt_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    coordinator_schema_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    narrative_schema_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    chat_schema_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    result_schema_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    policy_id: str = Field(min_length=1, max_length=128)
    tool_registry_id: str = Field(min_length=1, max_length=128)
    assertion_trust_store_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    assertion_trust_store_state: Literal["unreleased", "active"]
    evidence_snapshot_id: str | None = Field(default=None, min_length=1, max_length=256)
    evidence_runtime_v3_sha256: str | None = Field(
        default=None,
        pattern=r"^[a-f0-9]{64}$",
    )
    release_profile_id: str = Field(min_length=1, max_length=256)
    release_profile_state: Literal["unreleased", "approved"]
    release_profile_sha256: str | None = Field(default=None, pattern=r"^[a-f0-9]{64}$")
    release_profile_subject_sha256: str | None = Field(
        default=None,
        pattern=r"^[a-f0-9]{64}$",
    )
    release_profile_evaluation_report_sha256: str | None = Field(
        default=None,
        pattern=r"^[a-f0-9]{64}$",
    )
    reviewer_trust_store_sha256: str | None = Field(
        default=None,
        pattern=r"^[a-f0-9]{64}$",
    )
    kill_switch_active: bool

    @model_validator(mode="after")
    def approved_release_is_complete(self) -> "ReleaseIdentityV3":
        approved_values = (
            self.agent_git_sha,
            self.agent_source_snapshot_sha256,
            self.evidence_snapshot_id,
            self.evidence_runtime_v3_sha256,
            self.release_profile_sha256,
            self.release_profile_subject_sha256,
            self.release_profile_evaluation_report_sha256,
            self.reviewer_trust_store_sha256,
        )
        if self.release_profile_state == "approved" and (
            any(value is None for value in approved_values)
            or self.kill_switch_active
            or self.assertion_trust_store_state != "active"
        ):
            raise ValueError("approved Agent runtime release identity is incomplete")
        return self


class RequiredAction(BaseModel):
    model_config = ConfigDict(extra="forbid")

    action_code: str = Field(min_length=1, max_length=128)
    message: str = Field(min_length=1, max_length=2000)
    source_reference_ids: list[str] = Field(default_factory=list, max_length=100)


class ReviewGap(BaseModel):
    model_config = ConfigDict(extra="forbid")

    gap_id: str = Field(pattern=r"^gap_[a-f0-9]{64}$")
    gap_type: Literal[
        "field_uncertainty",
        "validation_issue",
        "regulatory_finding",
        "evidence_incomplete",
        "human_approval_required",
        "release_gate",
    ]
    reason_code: str = Field(min_length=1, max_length=128)
    source_reference_ids: list[str] = Field(default_factory=list, max_length=100)
    status: Literal["open", "resolved", "deferred"] = "open"


ModelExecutionUnmetReason = Literal[
    "no_model_calls",
    "provider_not_anthropic",
    "model_id_incomplete_or_mixed",
    "non_text_model_call",
    "non_positive_input_tokens",
    "non_positive_output_tokens",
    "prompt_hash_incomplete",
    "schema_hash_incomplete",
    "runtime_identity_mismatch",
]


class ModelExecutionSummary(BaseModel):
    """Bounded, non-sensitive projection of provider model-call audit records.

    This deliberately excludes document/request/message IDs, raw content, hashes,
    latency detail, credentials, and provider responses.  ``audit_complete`` means
    only that the provider-owned audit records are structurally complete and bound
    to the runtime identity; it is not a model-quality or Production-release claim.
    """

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    provider: Literal["anthropic"] | None
    model_id: str | None = Field(min_length=1, max_length=128)
    text_only: bool
    call_count: int = Field(ge=0, le=1000)
    input_tokens_total: int = Field(ge=0, le=1_000_000_000_000)
    output_tokens_total: int = Field(ge=0, le=1_000_000_000_000)
    prompt_hashes_complete: bool
    schema_hashes_complete: bool
    runtime_identity_match: bool
    audit_complete: bool
    unmet_reason_codes: list[ModelExecutionUnmetReason] = Field(max_length=9)

    @staticmethod
    def expected_unmet_reason_codes(
        *,
        provider: str | None,
        model_id: str | None,
        text_only: bool,
        call_count: int,
        input_tokens_total: int,
        output_tokens_total: int,
        prompt_hashes_complete: bool,
        schema_hashes_complete: bool,
        runtime_identity_match: bool,
    ) -> list[ModelExecutionUnmetReason]:
        if call_count == 0:
            return ["no_model_calls"]
        reasons: list[ModelExecutionUnmetReason] = []
        if provider != "anthropic":
            reasons.append("provider_not_anthropic")
        if model_id is None:
            reasons.append("model_id_incomplete_or_mixed")
        if not text_only:
            reasons.append("non_text_model_call")
        if input_tokens_total <= 0:
            reasons.append("non_positive_input_tokens")
        if output_tokens_total <= 0:
            reasons.append("non_positive_output_tokens")
        if not prompt_hashes_complete:
            reasons.append("prompt_hash_incomplete")
        if not schema_hashes_complete:
            reasons.append("schema_hash_incomplete")
        if not runtime_identity_match:
            reasons.append("runtime_identity_mismatch")
        return reasons

    @model_validator(mode="after")
    def derived_audit_state_is_consistent(self) -> "ModelExecutionSummary":
        if self.call_count == 0 and (
            self.provider is not None
            or self.model_id is not None
            or self.text_only
            or self.input_tokens_total != 0
            or self.output_tokens_total != 0
            or self.prompt_hashes_complete
            or self.schema_hashes_complete
            or self.runtime_identity_match
        ):
            raise ValueError("an empty model execution summary cannot advertise observed calls")
        expected = self.expected_unmet_reason_codes(
            provider=self.provider,
            model_id=self.model_id,
            text_only=self.text_only,
            call_count=self.call_count,
            input_tokens_total=self.input_tokens_total,
            output_tokens_total=self.output_tokens_total,
            prompt_hashes_complete=self.prompt_hashes_complete,
            schema_hashes_complete=self.schema_hashes_complete,
            runtime_identity_match=self.runtime_identity_match,
        )
        if self.unmet_reason_codes != expected or self.audit_complete != (not expected):
            raise ValueError("model execution summary derived state is inconsistent")
        return self


class AgenticCallAuditV1(BaseModel):
    """Strict inbound audit for a single bounded Agentic Claude call."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    task_id: Literal[
        "customs_review_coordinator",
        "customs_narrative_report",
        "customs_case_answer",
    ]
    provider: Literal["anthropic"]
    model_id: str = Field(min_length=1, max_length=128)
    prompt_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    output_schema_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    request_id_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    message_id_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    input_tokens: int = Field(ge=0, le=1_000_000_000, strict=True)
    output_tokens: int = Field(ge=0, le=1_000_000_000, strict=True)
    latency_ms: int = Field(ge=0, le=300_000, strict=True)


class AgentReviewResultV3(BaseModel):
    """Strict top-level consumer for the provider-owned V3 wire.

    Complex V2-compatible details remain opaque JSON, but every provider top-level
    member is enumerated and additional members are rejected.  The provider's
    canonical result hash is independently recomputed before any field is used.
    """

    model_config = ConfigDict(extra="forbid")

    schema_version: Literal["3.0"]
    case_id: str = Field(min_length=1, max_length=128)
    review_run_id: str = Field(min_length=1, max_length=128)
    review_as_of: date
    trade_type: Literal["import", "export"]
    jurisdiction: Literal["KR"]
    status: Literal["completed", "needs_review", "blocked"]
    requires_human_review: bool
    retrieval_status: Literal["ok", "partial", "failed"]
    retrieval_coverage: Literal["complete", "partial", "unknown"]
    knowledge_snapshot_id: str | None = None
    retrieval_request: dict[str, Any] | None = None
    retrieval_results: list[dict[str, Any]] = Field(default_factory=list)
    retrieval_errors: list[dict[str, Any]] = Field(default_factory=list)
    validation_issues: list[dict[str, Any]] = Field(default_factory=list)
    regulatory_findings: list[dict[str, Any]] = Field(default_factory=list)
    rag_evidence: list[dict[str, Any]] = Field(default_factory=list)
    report: dict[str, Any]
    audit_trail: list[dict[str, Any]] = Field(default_factory=list)
    model_calls: list[dict[str, Any]] = Field(default_factory=list)
    model_errors: list[dict[str, Any]] = Field(default_factory=list)
    model_conflicts: list[dict[str, Any]] = Field(default_factory=list)
    document_results: list[dict[str, Any]] = Field(default_factory=list)
    execution_status: ExecutionStatus
    assessment: Assessment
    review_disposition: ReviewDisposition
    assurance_level: AssuranceLevel
    completion_eligibility: CompletionEligibility
    required_actions: list[RequiredAction] = Field(default_factory=list, max_length=100)
    fact_ledger: dict[str, Any]
    review_gaps: list[ReviewGap] = Field(default_factory=list, max_length=1000)
    review_plan: dict[str, Any]
    narrative_report: dict[str, Any] = Field(default_factory=dict)
    action_trace: list[dict[str, Any]] = Field(default_factory=list)
    observations: list[dict[str, Any]] = Field(default_factory=list)
    runtime_release: ReleaseIdentityV3
    input_manifest_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    result_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")

    @model_validator(mode="after")
    def provider_invariants(self) -> "AgentReviewResultV3":
        fact_references = {
            (item.get("kind"), item.get("reference_id"))
            for item in self.fact_ledger.get("entries", [])
            if isinstance(item, dict)
        }
        claims: list[Any] = []
        for section in (
            "summary",
            "document_consistency",
            "regulatory_review",
            "required_actions",
        ):
            value = self.narrative_report.get(section, [])
            if isinstance(value, list):
                claims.extend(value)
        for claim in claims:
            if not isinstance(claim, dict):
                raise ValueError("narrative report contains a malformed claim")
            citations = claim.get("citations", [])
            if (claim.get("material", True) and not citations) or any(
                (citation.get("kind"), citation.get("reference_id")) not in fact_references
                for citation in citations
                if isinstance(citation, dict)
            ):
                raise ValueError("narrative claim cites a fact outside the canonical ledger")
        gap_ids = {gap.gap_id for gap in self.review_gaps}
        planned_actions = self.review_plan.get("actions", [])
        tool_call_limit = self.review_plan.get("tool_call_limit")
        cycle_limit = self.review_plan.get("cycle_limit")
        if (
            not isinstance(planned_actions, list)
            or isinstance(tool_call_limit, bool)
            or not isinstance(tool_call_limit, int)
            or not 0 <= tool_call_limit <= 6
            or isinstance(cycle_limit, bool)
            or not isinstance(cycle_limit, int)
            or not 0 <= cycle_limit <= 2
            or len(planned_actions) > tool_call_limit
            or len(self.action_trace) > tool_call_limit
        ):
            raise ValueError("Agent plan or trace exceeds the bounded Tool-call limit")
        if any(
            not isinstance(action, dict) or action.get("gap_id") not in gap_ids
            for action in planned_actions
        ):
            raise ValueError("planned Agent actions must reference declared review gaps")
        planned_ids = [action.get("action_id") for action in planned_actions]
        if any(not isinstance(action_id, str) for action_id in planned_ids) or len(
            planned_ids
        ) != len(set(planned_ids)):
            raise ValueError("planned Agent action IDs must be present and unique")
        planned_by_id = {action["action_id"]: action for action in planned_actions}
        trace_ids = [item.get("action_id") for item in self.action_trace]
        observation_ids = [item.get("action_id") for item in self.observations]
        if any(action_id not in planned_by_id for action_id in trace_ids) or len(trace_ids) != len(
            set(trace_ids)
        ):
            raise ValueError("Agent action trace is not bound to the compiled plan")
        if any(action_id not in planned_by_id for action_id in observation_ids) or len(
            observation_ids
        ) != len(set(observation_ids)):
            raise ValueError("Agent observation is not bound to the compiled plan")
        if len(self.observations) != len(self.action_trace) or set(observation_ids) != set(
            trace_ids
        ):
            raise ValueError("every Agent Tool action requires exactly one observation")
        trace_by_id = {item["action_id"]: item for item in self.action_trace}
        observations_by_id = {item["action_id"]: item for item in self.observations}
        if any(
            isinstance(item.get("cycle"), bool)
            or not isinstance(item.get("cycle"), int)
            or not 0 <= item["cycle"] <= cycle_limit
            or item.get("tool_name") != planned_by_id[item["action_id"]].get("tool_name")
            or observations_by_id[item["action_id"]].get("tool_name") != item.get("tool_name")
            or observations_by_id[item["action_id"]].get("cycle") != item.get("cycle")
            for item in trace_by_id.values()
        ):
            raise ValueError("Agent Tool execution differs from its bounded compiled plan")
        expected_assessment = (
            Assessment.issues_detected
            if self.validation_issues or self.regulatory_findings
            else (
                Assessment.no_issue_detected
                if self.retrieval_status == "ok" and self.retrieval_coverage == "complete"
                else Assessment.indeterminate
            )
        )
        if self.assessment != expected_assessment:
            raise ValueError("assessment is not derived from issues and Evidence status")
        if self.review_disposition == ReviewDisposition.auto_review_complete and (
            self.execution_status != ExecutionStatus.succeeded
            or self.assessment != Assessment.no_issue_detected
            or self.assurance_level != AssuranceLevel.production
            or not self.completion_eligibility.eligible
            or self.required_actions
            or any(gap.status == "open" for gap in self.review_gaps)
            or self.status != "completed"
            or self.requires_human_review
            or any(item.get("status") != "succeeded" for item in self.observations)
            or self.runtime_release.release_profile_state != "approved"
            or self.runtime_release.release_profile_sha256 is None
            or self.runtime_release.release_profile_subject_sha256 is None
            or self.runtime_release.release_profile_evaluation_report_sha256 is None
            or self.runtime_release.reviewer_trust_store_sha256 is None
            or self.runtime_release.evidence_runtime_v3_sha256 is None
            or self.runtime_release.kill_switch_active
        ):
            raise ValueError("provider auto completion violates deterministic V3 gates")
        if self.review_disposition == ReviewDisposition.blocked and self.status != "blocked":
            raise ValueError("blocked disposition must preserve the deterministic status")
        payload = self.model_dump(mode="json")
        payload["result_sha256"] = "0" * 64
        if self.result_sha256 != canonical_digest(payload):
            raise ValueError("provider result_sha256 does not match canonical V3 result")
        return self


class InputDocumentManifestEntry(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    document_id: str = Field(min_length=1, max_length=128)
    document_type: Literal["CI", "PL", "BL", "CO", "UNKNOWN"]
    content_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")


class AutoCompletionProfileBinding(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    profile_id: str | None = Field(default=None, min_length=1, max_length=256)
    approved_document_types: list[Literal["CI", "PL", "BL", "CO"]] = Field(
        default_factory=list,
        max_length=4,
    )
    approved_required_document_codes: list[str] = Field(default_factory=list, max_length=100)
    approval_digest: str | None = Field(default=None, pattern=r"^[a-f0-9]{64}$")

    @field_validator("approved_document_types")
    @classmethod
    def approved_document_type_order(cls, values: list[str]) -> list[str]:
        if values and values != ["CI", "PL", "BL", "CO"]:
            raise ValueError("approved document types must use the signed release order")
        return values

    @field_validator("approved_required_document_codes")
    @classmethod
    def sorted_unique_required_codes(cls, values: list[str]) -> list[str]:
        if values != sorted(set(values)):
            raise ValueError("approved required-document codes must be sorted and unique")
        return values

    @model_validator(mode="after")
    def signed_binding_is_complete(self) -> "AutoCompletionProfileBinding":
        if (self.profile_id is None) != (self.approval_digest is None):
            raise ValueError("profile identity and approval digest must be present together")
        if self.profile_id is None and (
            self.approved_document_types or self.approved_required_document_codes
        ):
            raise ValueError("unapproved profile cannot advertise approved scope")
        return self


MAX_REVIEW_STAGE_TRACE_ENTRIES = 64


class ReviewStageTraceEntry(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    # A revision result contains the original analysis plus the deterministic
    # stages rerun after an approved correction.  The bounded Agent currently
    # emits 21 mandatory-stage events for that valid path, so the former limit
    # of 20 rejected a legitimate child Review at the persistence boundary.
    # Keep a finite consumer-side bound without assuming that one run can only
    # visit each mandatory stage once.
    sequence: int = Field(ge=1, le=MAX_REVIEW_STAGE_TRACE_ENTRIES)
    stage: Literal[
        "load_case",
        "classify_documents",
        "extract_documents",
        "normalize_documents",
        "pair_documents",
        "validate_cross_documents",
        "retrieve_regulations",
        "compliance_review",
        "generate_xai_report",
        "route_to_human_or_complete",
        "redact_sensitive_inputs",
    ]
    status: Literal["completed", "needs_review", "partial", "failed"]
    reason_ids: list[str] = Field(default_factory=list, max_length=100)


class ReviewRunRecord(BaseModel):
    model_config = ConfigDict(extra="forbid")

    review_run_id: str
    case_id: str
    org_id: str
    job_id: str | None = None
    parent_run_id: str | None = None
    case_revision: int = Field(ge=0)
    review_as_of: date | None = None
    trade_type: Literal["import", "export"] | None = None
    input_document_manifest: list[InputDocumentManifestEntry] = Field(
        default_factory=list,
        max_length=6,
    )
    input_manifest_digest: str = Field(pattern=r"^[a-f0-9]{64}$")
    agent_input_manifest_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    provider_result_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    result_digest: str = Field(pattern=r"^[a-f0-9]{64}$")
    execution_status: ExecutionStatus
    assessment: Assessment
    review_disposition: ReviewDisposition
    assurance_level: AssuranceLevel
    completion_eligibility: CompletionEligibility
    required_actions: list[RequiredAction] = Field(default_factory=list)
    narrative_report: dict[str, Any] = Field(default_factory=dict)
    action_trace: list[dict[str, Any]] = Field(default_factory=list)
    review_stage_trace: list[ReviewStageTraceEntry] = Field(
        default_factory=list,
        max_length=MAX_REVIEW_STAGE_TRACE_ENTRIES,
    )
    result_detail: dict[str, Any]
    release_identity: dict[str, Any] = Field(default_factory=dict)
    auto_completion_profile: AutoCompletionProfileBinding = Field(
        default_factory=AutoCompletionProfileBinding
    )
    created_by: str
    created_at: datetime

    @model_validator(mode="after")
    def immutable_input_manifest_is_bound(self) -> "ReviewRunRecord":
        model_execution_summary = ModelExecutionSummary.model_validate(
            self.result_detail.get("model_execution_summary")
        )
        if self.input_document_manifest:
            if self.review_as_of is None or self.trade_type is None:
                raise ValueError("bound input manifest requires immutable review context")
            ordered = sorted(self.input_document_manifest, key=lambda item: item.document_id)
            if ordered != self.input_document_manifest:
                raise ValueError("input document manifest must be sorted by document_id")
            expected = canonical_digest(
                {
                    "case_id": self.case_id,
                    "case_revision": self.case_revision,
                    "review_as_of": self.review_as_of.isoformat(),
                    "trade_type": self.trade_type,
                    "documents": [item.model_dump(mode="json") for item in ordered],
                }
            )
            if self.input_manifest_digest != expected:
                raise ValueError("input_manifest_digest does not bind the public input manifest")
        if self.review_disposition == ReviewDisposition.auto_review_complete:
            if (
                not self.input_document_manifest
                or self.auto_completion_profile.profile_id is None
                or self.auto_completion_profile.approval_digest is None
            ):
                raise ValueError("automatic completion requires a signed public profile binding")
        expected_result_digest = canonical_digest(
            {
                "provider_result_sha256": self.provider_result_sha256,
                "input_manifest_digest": self.input_manifest_digest,
                "input_document_manifest": [
                    item.model_dump(mode="json") for item in self.input_document_manifest
                ],
                "execution_status": self.execution_status.value,
                "assessment": self.assessment.value,
                "review_disposition": self.review_disposition.value,
                "assurance_level": self.assurance_level.value,
                "completion_eligibility": self.completion_eligibility.model_dump(mode="json"),
                "required_actions": [
                    item.model_dump(mode="json") for item in self.required_actions
                ],
                "review_stage_trace": [
                    item.model_dump(mode="json") for item in self.review_stage_trace
                ],
                "auto_completion_profile": self.auto_completion_profile.model_dump(mode="json"),
                "model_execution_summary": model_execution_summary.model_dump(mode="json"),
            }
        )
        if self.result_digest != expected_result_digest:
            raise ValueError("result_digest does not bind the public effective review summary")
        return self


class CorrectionOperation(BaseModel):
    model_config = ConfigDict(extra="forbid")

    document_id: str = Field(min_length=1)
    field_path: str = Field(min_length=1, max_length=512)
    new_value: Any
    reason: str = Field(min_length=1, max_length=1000)
    confidence: float = Field(default=1.0, ge=0, le=1)

    @field_validator("new_value", mode="before")
    @classmethod
    def bounded_canonical_json_value(cls, value: Any) -> Any:
        try:
            encoded = json.dumps(
                value,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
                allow_nan=False,
            )
        except (TypeError, ValueError) as exc:
            raise ValueError("correction value must be finite canonical JSON") from exc
        if len(encoded.encode("utf-8")) > 20_000:
            raise ValueError("correction value exceeds the 20KB safety limit")
        return value

    @field_validator("field_path")
    @classmethod
    def immutable_result_fields_are_forbidden(cls, value: str) -> str:
        allowed_roots = {"fields", "line_items"}
        root = value.split(".", 1)[0]
        if root not in allowed_roots:
            raise ValueError("corrections may target only extracted source fields or line items")
        if ".." in value or value.startswith(".") or value.endswith("."):
            raise ValueError("correction field path is malformed")
        return value


class CorrectionProposalCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    parent_result_digest: str = Field(pattern=r"^[a-f0-9]{64}$")
    parent_input_manifest_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    case_revision: int = Field(ge=0)
    corrections: list[CorrectionOperation] = Field(min_length=1, max_length=100)


class RevisionAction(BaseModel):
    """Typed, inert proposal action; it reaches Agent only after maker-checker approval."""

    model_config = ConfigDict(extra="forbid")

    decision: Literal[
        "confirm_field",
        "correct_field",
        "add_supporting_document",
        "approve_review",
        "reject_review",
    ]
    reason: str = Field(min_length=1, max_length=2000)
    confirmed_reference_id: str | None = Field(default=None, min_length=1, max_length=256)
    correction: CorrectionOperation | None = None
    supporting_document_id: str | None = Field(default=None, min_length=1, max_length=128)

    @model_validator(mode="after")
    def decision_payload_is_exact(self) -> "RevisionAction":
        if (self.decision == "confirm_field") != (self.confirmed_reference_id is not None):
            raise ValueError("only confirm_field contains a confirmed reference")
        if (self.decision == "correct_field") != (self.correction is not None):
            raise ValueError("only correct_field contains a correction")
        if (self.decision == "add_supporting_document") != (
            self.supporting_document_id is not None
        ):
            raise ValueError("only add_supporting_document contains a document ID")
        return self


class RevisionProposalCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    parent_result_digest: str = Field(pattern=r"^[a-f0-9]{64}$")
    parent_input_manifest_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    case_revision: int = Field(ge=0)
    actions: list[RevisionAction] = Field(min_length=1, max_length=100)

    @model_validator(mode="after")
    def one_terminal_decision(self) -> "RevisionProposalCreate":
        terminal = [
            item for item in self.actions if item.decision in {"approve_review", "reject_review"}
        ]
        if len(terminal) > 1:
            raise ValueError("a revision proposal permits at most one terminal decision")
        return self


class CorrectionProposalStatus(str, Enum):
    pending = "pending"
    approved = "approved"
    rejected = "rejected"


class CorrectionProposalRecord(BaseModel):
    model_config = ConfigDict(extra="forbid")

    proposal_id: str
    org_id: str
    case_id: str
    review_run_id: str
    idempotency_key: str = Field(min_length=1, max_length=512)
    idempotency_request_digest: str = Field(pattern=r"^[a-f0-9]{64}$")
    parent_result_digest: str = Field(pattern=r"^[a-f0-9]{64}$")
    parent_input_manifest_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    case_revision: int = Field(ge=0)
    corrections: list[CorrectionOperation] = Field(default_factory=list, max_length=100)
    actions: list[RevisionAction] = Field(default_factory=list, max_length=100)
    status: CorrectionProposalStatus = CorrectionProposalStatus.pending
    created_by: str
    created_at: datetime
    decided_by: str | None = None
    decided_at: datetime | None = None
    decision_reason: str | None = None
    child_job_id: str | None = None

    @model_validator(mode="after")
    def exactly_one_proposal_payload(self) -> "CorrectionProposalRecord":
        if bool(self.corrections) == bool(self.actions):
            raise ValueError("proposal must contain corrections or typed actions")
        return self


class CorrectionProposalDecisionCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    decision: Literal["approve", "reject"]
    reason: str = Field(min_length=1, max_length=2000)
    parent_result_digest: str = Field(pattern=r"^[a-f0-9]{64}$")
    case_revision: int = Field(ge=0)


# Kept as a parse-only compatibility name. Direct revision execution is no longer
# exposed; callers must use a persisted, approved CorrectionProposal.
class RevisionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    proposal_id: str = Field(min_length=1, max_length=128)
    parent_result_digest: str = Field(pattern=r"^[a-f0-9]{64}$")
    case_revision: int = Field(ge=0)


class HumanDecisionCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    decision: Literal["approve", "reject"]
    reason: str = Field(min_length=1, max_length=2000)
    target_revision_id: str | None = None


class HumanDecisionRecord(BaseModel):
    decision_id: str
    review_run_id: str
    case_id: str
    org_id: str
    decision: Literal["approve", "reject"]
    reason: str
    target_revision_id: str | None = None
    proposal_id: str | None = None
    idempotency_key: str | None = Field(default=None, min_length=1, max_length=512)
    idempotency_request_digest: str | None = Field(
        default=None,
        pattern=r"^[a-f0-9]{64}$",
    )
    decided_by: str
    created_at: datetime

    @model_validator(mode="after")
    def idempotency_binding_is_complete(self) -> "HumanDecisionRecord":
        if (self.idempotency_key is None) != (self.idempotency_request_digest is None):
            raise ValueError("decision idempotency key and request digest must be stored together")
        if self.proposal_id is not None and self.idempotency_key is None:
            raise ValueError("proposal decisions require durable idempotency binding")
        return self


class GroundedQuestion(BaseModel):
    model_config = ConfigDict(extra="forbid")

    question: str = Field(min_length=1, max_length=2000)
    bound_result_digest: str = Field(pattern=r"^[a-f0-9]{64}$")


class ChatRecord(BaseModel):
    model_config = ConfigDict(extra="forbid")

    message_id: str
    review_run_id: str
    case_id: str
    org_id: str
    question: str
    answer: str
    answer_status: Literal["answered", "insufficient_evidence", "out_of_scope"]
    citations: list[dict[str, str]] = Field(default_factory=list)
    generation_mode: Literal["claude", "deterministic_fallback"]
    model_execution_summary: ModelExecutionSummary
    bound_result_digest: str
    idempotency_key: str = Field(min_length=1, max_length=512)
    idempotency_request_digest: str = Field(pattern=r"^[a-f0-9]{64}$")
    created_by: str
    created_at: datetime

    @model_validator(mode="after")
    def generation_mode_matches_model_execution(self) -> "ChatRecord":
        expected_count = 1 if self.generation_mode == "claude" else 0
        if self.model_execution_summary.call_count != expected_count:
            raise ValueError("Chat generation mode does not match its model execution summary")
        return self


class ChatInteractionState(str, Enum):
    reserved = "reserved"
    call_started = "call_started"
    completed = "completed"
    delivery_uncertain = "delivery_uncertain"
    response_received_rejected = "response_received_rejected"
    response_persistence_uncertain = "response_persistence_uncertain"


class ChatInteractionRecord(BaseModel):
    model_config = ConfigDict(extra="forbid")

    interaction_id: str
    review_run_id: str
    case_id: str
    org_id: str
    idempotency_key: str = Field(min_length=1, max_length=512)
    idempotency_request_digest: str = Field(pattern=r"^[a-f0-9]{64}$")
    agent_request_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    state: ChatInteractionState = ChatInteractionState.reserved
    message_id: str | None = None
    error_code: str | None = Field(default=None, max_length=256)
    call_started_at: datetime | None = None
    created_by: str
    created_at: datetime
    updated_at: datetime

    @model_validator(mode="after")
    def interaction_state_is_complete(self) -> "ChatInteractionRecord":
        if self.state == ChatInteractionState.reserved:
            if self.call_started_at is not None or self.message_id is not None:
                raise ValueError("reserved chat interaction cannot contain call/result lineage")
        else:
            if self.call_started_at is None:
                raise ValueError("started chat interaction requires a call timestamp")
        if self.state == ChatInteractionState.completed:
            if self.message_id is None or self.error_code is not None:
                raise ValueError("completed chat interaction requires only its immutable message")
        elif self.message_id is not None:
            raise ValueError("non-completed chat interaction cannot reference a message")
        if (
            self.state
            in {
                ChatInteractionState.delivery_uncertain,
                ChatInteractionState.response_received_rejected,
                ChatInteractionState.response_persistence_uncertain,
            }
            and self.error_code is None
        ):
            raise ValueError("terminal uncertain/rejected chat interaction requires an error code")
        return self


class AuditRecord(BaseModel):
    audit_id: str
    org_id: str
    case_id: str | None = None
    review_run_id: str | None = None
    actor_id: str
    action: str
    detail: dict[str, Any] = Field(default_factory=dict)
    created_at: datetime


class OutboxRecord(BaseModel):
    outbox_id: str
    org_id: str
    topic: str
    aggregate_id: str
    payload: dict[str, Any]
    created_at: datetime
    published_at: datetime | None = None
    lease_id: str | None = None
    lease_expires_at: datetime | None = None
    attempt_count: int = Field(default=0, ge=0)


class StageEvent(BaseModel):
    event_id: str
    job_id: str
    sequence: int = Field(ge=1)
    stage: Literal[
        "queued",
        "document_analysis",
        "normalization",
        "validation",
        "evidence",
        "compliance",
        "report",
        "completed",
        "failed",
    ]
    state: Literal["started", "progress", "completed", "failed"]
    message_code: str
    occurred_at: datetime
    detail: dict[str, Any] = Field(default_factory=dict)


class AgentDocumentInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    document_id: str = Field(min_length=1, max_length=128)
    filename: str = Field(min_length=1, max_length=255)
    content_text: str = Field(min_length=1, max_length=200_000)
    image_pages: list[Any] = Field(default_factory=list, max_length=0)
    pdf_pages: None = None
    declared_type: (
        Literal[
            "invoice",
            "packing_list",
            "bill_of_lading",
            "certificate_of_origin",
            "unknown",
        ]
        | None
    ) = None
    requirement_document_codes: list[str] = Field(default_factory=list, max_length=100)

    def to_agent_wire(self) -> dict[str, Any]:
        # The first service release is text-only. Agent ingress intentionally
        # rejects even empty visual keys, so they must be absent on the wire.
        return self.model_dump(
            mode="json",
            exclude={"image_pages", "pdf_pages"},
        )


class AgentReviewRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    review_run_id: str
    case_id: str
    documents: list[AgentDocumentInput] = Field(min_length=1, max_length=6)
    llm_mode: Literal["llm"] = "llm"
    review_as_of: date
    assertion_cutoff_at: AwareDatetime
    trade_type: Literal["import", "export"]
    jurisdiction: Literal["KR"] = "KR"
    uploaded_requirement_document_codes: list[str] = Field(default_factory=list, max_length=0)
    supporting_documents: list[SupportingDocumentRecord] = Field(
        default_factory=list, max_length=100
    )
    document_language: Literal["unknown", "ko", "en"] = "unknown"

    @field_validator("supporting_documents", mode="before")
    @classmethod
    def strip_backend_supporting_fields(cls, values: Any) -> Any:
        if not isinstance(values, list):
            return values
        normalized: list[Any] = []
        for value in values:
            if isinstance(value, SupportingDocumentRecord):
                normalized.append(value)
            else:
                normalized.append(value)
        return normalized

    def to_agent_wire(self) -> dict[str, Any]:
        payload = self.model_dump(mode="json")
        payload["documents"] = [item.to_agent_wire() for item in self.documents]
        payload["supporting_documents"] = [
            item.to_agent_wire() for item in self.supporting_documents
        ]
        return payload

    def to_agent_manifest_wire(self) -> dict[str, Any]:
        """Return the provider-parsed canonical shape used by Agent V3 hashes.

        The text-only transport deliberately omits the unreleased visual keys,
        while the pinned Agent model materializes their safe empty defaults
        before calculating ``input_manifest_sha256``. Keep this semantic hash
        distinct from the bytes sent through the call-once barrier.
        """

        payload = self.to_agent_wire()
        payload["documents"] = [
            {**document, "image_pages": [], "pdf_pages": None} for document in payload["documents"]
        ]
        return payload

    @property
    def input_manifest_sha256(self) -> str:
        return canonical_digest(self.to_agent_manifest_wire())


class AgentHumanDecision(BaseModel):
    model_config = ConfigDict(extra="forbid")

    decision_id: str = Field(pattern=r"^decision_[a-f0-9]{64}$")
    decision: Literal[
        "confirm_field",
        "correct_field",
        "add_supporting_document",
        "approve_review",
        "reject_review",
    ]
    actor_id: str = Field(min_length=1, max_length=256)
    actor_role: Literal["reviewer", "approver"]
    decided_at: AwareDatetime
    reason: str = Field(min_length=1, max_length=2000)
    confirmed_reference_id: str | None = Field(default=None, min_length=1, max_length=256)
    correction: dict[str, Any] | None = None
    supporting_document: dict[str, Any] | None = None

    @model_validator(mode="after")
    def decision_id_binds_normalized_wire(self) -> "AgentHumanDecision":
        if (self.decision == "correct_field") != (self.correction is not None):
            raise ValueError("only correct_field contains a correction")
        if (self.decision == "add_supporting_document") != (self.supporting_document is not None):
            raise ValueError("only add_supporting_document contains a document")
        if (self.decision == "confirm_field") != (self.confirmed_reference_id is not None):
            raise ValueError("only confirm_field contains a confirmed reference")
        if self.decision == "approve_review" and self.actor_role != "approver":
            raise ValueError("review approval requires the approver role")
        payload = self.model_dump(mode="json", exclude={"decision_id"})
        if self.decision_id != f"decision_{canonical_digest(payload)}":
            raise ValueError("Agent decision ID does not bind its canonical contents")
        return self

    def to_agent_wire(self) -> dict[str, Any]:
        return self.model_dump(mode="json")


class AgentRevisionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    parent_result_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    parent_input_manifest_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    parent_result: dict[str, Any]
    parent_request: AgentReviewRequest
    revised_request: AgentReviewRequest
    decisions: list[AgentHumanDecision] = Field(min_length=1, max_length=100)

    @model_validator(mode="after")
    def revision_is_bound_and_decision_compiled(self) -> "AgentRevisionRequest":
        if self.parent_result.get("result_sha256") != self.parent_result_sha256:
            raise ValueError("revision parent result hash is stale")
        if self.parent_result.get("input_manifest_sha256") != (self.parent_input_manifest_sha256):
            raise ValueError("revision parent input hash is stale")
        if self.parent_request.input_manifest_sha256 != self.parent_input_manifest_sha256:
            raise ValueError("revision parent request does not match its immutable hash")
        expected = self.parent_request.model_copy(
            deep=True,
            update={"review_run_id": self.revised_request.review_run_id},
        )
        addition_wires = [
            item.supporting_document
            for item in self.decisions
            if item.decision == "add_supporting_document" and item.supporting_document is not None
        ]
        additions = [
            supporting
            for wire in addition_wires
            for supporting in self.revised_request.supporting_documents
            if supporting.to_agent_wire() == wire
        ]
        if len(additions) != len(addition_wires):
            raise ValueError("supporting-document decision is not bound to the revised request")
        expected.supporting_documents = [*expected.supporting_documents, *additions]
        if expected != self.revised_request:
            raise ValueError("revision request contains an unapproved input change")
        allowed_paths = {
            (str(document.get("document_id")), f"fields.{field.get('name')}")
            for document in self.parent_result.get("document_results", [])
            if isinstance(document, dict)
            for field in document.get("fields", [])
            if isinstance(field, dict) and isinstance(field.get("name"), str)
        }
        allowed_paths.update(
            {
                (
                    str(document.get("document_id")),
                    f"line_items.{index}.{field.get('name')}",
                )
                for document in self.parent_result.get("document_results", [])
                if isinstance(document, dict)
                for index, item in enumerate(document.get("line_items", []))
                if isinstance(item, dict)
                for field in item.get("fields", [])
                if isinstance(field, dict) and isinstance(field.get("name"), str)
            }
        )
        if any(
            (
                decision.correction.get("document_id"),
                decision.correction.get("field_path"),
            )
            not in allowed_paths
            for decision in self.decisions
            if decision.correction is not None
        ):
            raise ValueError("revision correction is not bound to a parent result field")
        allowed_confirmations = {
            str(item.get("reference_id"))
            for item in self.parent_result.get("fact_ledger", {}).get("entries", [])
            if isinstance(item, dict) and item.get("kind") == "field_source"
        }
        if any(
            item.confirmed_reference_id not in allowed_confirmations
            for item in self.decisions
            if item.confirmed_reference_id is not None
        ):
            raise ValueError("confirmation is not bound to a parent result field")
        terminal = [
            item for item in self.decisions if item.decision in {"approve_review", "reject_review"}
        ]
        if len(terminal) > 1:
            raise ValueError("revision permits at most one terminal human decision")
        return self

    def to_agent_wire(self) -> dict[str, Any]:
        payload = self.model_dump(mode="json")
        payload["parent_request"] = self.parent_request.to_agent_wire()
        payload["revised_request"] = self.revised_request.to_agent_wire()
        payload["decisions"] = [item.to_agent_wire() for item in self.decisions]
        return payload


class AgentRevisionResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    parent_result_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    child_result: AgentReviewResultV3
    applied_decision_ids: list[str] = Field(min_length=1, max_length=100)


class AgentAnswerResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    question_id: str = Field(pattern=r"^question_[a-f0-9]{64}$")
    answer: str
    status: Literal["answered", "insufficient_evidence", "out_of_scope"]
    citations: list[dict[str, str]] = Field(default_factory=list, max_length=100)
    generation_mode: Literal["claude", "deterministic_fallback"]
    citation_verification: Literal["passed"] = "passed"
    model_call: AgenticCallAuditV1 | None = None

    @model_validator(mode="after")
    def claude_requires_auditable_model_call(self) -> "AgentAnswerResponse":
        if (self.generation_mode == "claude") != (self.model_call is not None):
            raise ValueError("Claude answers require a complete model-call audit")
        return self
