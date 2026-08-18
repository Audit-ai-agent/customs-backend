"""Small port set for the direct local review flow."""

from __future__ import annotations

from typing import Any, Protocol

from customs_backend.domain import (
    CaseClosureRecord,
    CaseRecord,
    ConversationMessageExchange,
    ConversationMessageRecord,
    ConversationRecord,
    CorrectionProposalDecisionRecord,
    CorrectionProposalRecord,
    DocumentAssetRecord,
    ExtractionJobRecord,
    DocumentRecord,
    FindingDecisionRecord,
    FindingDecisionStatus,
    GuidanceRegistryVersion,
    HsSelectionRecord,
    JobRecord,
    KnowledgeConversationRecord,
    ReviewRunRecord,
)


class ConflictError(RuntimeError):
    """A caller supplied stale state or reused an idempotency key differently."""


class NotFoundError(RuntimeError):
    pass


class AuthorizationError(RuntimeError):
    pass


class KnowledgeInputRejectedError(ValueError):
    """A global knowledge request attempted to carry Case-like material.

    The fixed exception text is intentionally non-diagnostic: API handlers
    must not echo a document field, identifier, filename, or raw prompt.
    """

    def __init__(self) -> None:
        super().__init__("knowledge input is not allowed")


class AgentResponseRejectedError(RuntimeError):
    """The Agent response did not satisfy the direct HTTP boundary."""


class WorkbenchRepository(Protocol):
    def health(self) -> bool: ...
    def create_case(self, record: CaseRecord) -> CaseRecord: ...
    def get_case(self, org_id: str, case_id: str) -> CaseRecord | None: ...
    def list_cases(self, org_id: str) -> list[CaseRecord]: ...
    def add_document(
        self,
        record: DocumentRecord,
        *,
        expected_revision: int,
        new_manifest_digest: str,
    ) -> CaseRecord: ...
    def list_documents(self, org_id: str, case_id: str) -> list[DocumentRecord]: ...
    def add_document_asset(self, record: DocumentAssetRecord) -> DocumentAssetRecord: ...
    def list_document_assets(self, org_id: str, case_id: str) -> list[DocumentAssetRecord]: ...
    def get_document_asset(
        self, org_id: str, case_id: str, asset_id: str
    ) -> DocumentAssetRecord | None: ...
    def create_extraction_job(self, job: ExtractionJobRecord) -> ExtractionJobRecord: ...
    def update_extraction_job(self, job: ExtractionJobRecord) -> ExtractionJobRecord: ...
    def get_extraction_job(
        self, org_id: str, case_id: str, extraction_job_id: str
    ) -> ExtractionJobRecord | None: ...
    def create_review_job(
        self, job: JobRecord, *, expected_case_revision: int
    ) -> tuple[JobRecord, bool]: ...
    def get_review_job_by_idempotency(
        self, org_id: str, idempotency_key: str
    ) -> JobRecord | None: ...
    def update_job(self, job: JobRecord) -> JobRecord: ...
    def complete_review(self, job: JobRecord, run: ReviewRunRecord) -> ReviewRunRecord: ...
    def complete_review_and_apply_correction_proposals(
        self,
        *,
        job: JobRecord,
        run: ReviewRunRecord,
        correction_proposal_ids: list[str],
        applied_by: str,
    ) -> ReviewRunRecord: ...
    def record_finding_decision(
        self,
        *,
        org_id: str,
        review_run_id: str,
        finding_id: str,
        status: FindingDecisionStatus,
        reason: str,
        decided_by: str,
    ) -> FindingDecisionRecord: ...
    def delete_case(self, *, org_id: str, case_id: str) -> None: ...
    def close_case(
        self, *, org_id: str, case_id: str, closure: CaseClosureRecord
    ) -> CaseRecord: ...
    def record_hs_selection(
        self,
        *,
        org_id: str,
        review_run_id: str,
        code: str,
        reason: str,
        selected_by: str,
    ) -> HsSelectionRecord: ...
    def create_conversation(self, record: ConversationRecord) -> ConversationRecord: ...
    def get_conversation(self, org_id: str, conversation_id: str) -> ConversationRecord | None: ...
    def list_conversations(self, org_id: str, review_run_id: str) -> list[ConversationRecord]: ...
    def list_messages(
        self, org_id: str, conversation_id: str
    ) -> list[ConversationMessageRecord]: ...
    def get_message_exchange(
        self,
        *,
        org_id: str,
        conversation_id: str,
        idempotency_key: str,
        idempotency_request_digest: str,
    ) -> ConversationMessageExchange | None: ...
    def append_message_exchange(
        self,
        *,
        user_message: ConversationMessageRecord,
        assistant_message: ConversationMessageRecord,
        idempotency_key: str,
        idempotency_request_digest: str,
        conversation_snapshot_id: str | None = None,
        guidance_registry_version: GuidanceRegistryVersion | None = None,
    ) -> tuple[ConversationMessageExchange, bool]: ...
    def create_knowledge_conversation(
        self, record: KnowledgeConversationRecord
    ) -> KnowledgeConversationRecord: ...
    def get_knowledge_conversation(
        self, org_id: str, owner_subject: str, conversation_id: str
    ) -> KnowledgeConversationRecord | None: ...
    def list_knowledge_conversations(
        self, org_id: str, owner_subject: str
    ) -> list[KnowledgeConversationRecord]: ...
    def list_knowledge_messages(
        self, org_id: str, owner_subject: str, conversation_id: str
    ) -> list[ConversationMessageRecord]: ...
    def get_knowledge_message_exchange(
        self,
        *,
        org_id: str,
        owner_subject: str,
        conversation_id: str,
        idempotency_key: str,
        idempotency_request_digest: str,
    ) -> ConversationMessageExchange | None: ...
    def append_knowledge_message_exchange(
        self,
        *,
        user_message: ConversationMessageRecord,
        assistant_message: ConversationMessageRecord,
        knowledge_snapshot_id: str | None,
        idempotency_key: str,
        idempotency_request_digest: str,
        guidance_registry_version: GuidanceRegistryVersion | None = None,
    ) -> tuple[ConversationMessageExchange, bool]: ...
    def create_correction_proposal(
        self, record: CorrectionProposalRecord
    ) -> CorrectionProposalRecord: ...
    def get_correction_proposal(
        self, org_id: str, correction_proposal_id: str
    ) -> CorrectionProposalRecord | None: ...
    def list_correction_proposals(
        self, org_id: str, review_run_id: str
    ) -> list[CorrectionProposalRecord]: ...
    def decide_correction_proposal(
        self,
        *,
        org_id: str,
        correction_proposal_id: str,
        decision: CorrectionProposalDecisionRecord,
    ) -> CorrectionProposalRecord: ...
    def get_job(self, org_id: str, job_id: str) -> JobRecord | None: ...
    def latest_review_job(self, org_id: str, case_id: str) -> JobRecord | None: ...
    def get_run(self, org_id: str, review_run_id: str) -> ReviewRunRecord | None: ...
    def list_runs(self, org_id: str, case_id: str) -> list[ReviewRunRecord]: ...


class ObjectStore(Protocol):
    def health(self) -> bool: ...
    def put_text(self, *, key: str, content: str, content_sha256: str) -> None: ...
    def get_text(self, *, key: str, expected_sha256: str) -> str: ...
    def put_bytes(
        self, *, key: str, content: bytes, content_sha256: str, media_type: str
    ) -> None: ...
    def get_bytes(
        self, *, key: str, expected_sha256: str, maximum_bytes: int
    ) -> bytes: ...


class AgentClient(Protocol):
    def review(self, request: Any) -> dict[str, Any]: ...
    def get_review_progress(self, review_run_id: str) -> dict[str, Any] | None: ...
    def law_search(self, request: dict[str, Any]) -> dict[str, Any]: ...
    def law_catalog(self, request: dict[str, Any]) -> dict[str, Any]: ...
    def extract_document(self, request: Any, content: bytes) -> dict[str, Any]: ...
    def answer(self, request: Any) -> dict[str, Any]: ...
    def answer_knowledge(self, request: Any) -> dict[str, Any]: ...
    def metadata(self) -> dict[str, Any]: ...


class TokenVerifier(Protocol):
    def verify(self, token: str) -> Any: ...
