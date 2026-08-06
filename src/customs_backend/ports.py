"""Small port set for the direct local review flow."""

from __future__ import annotations

from typing import Any, Protocol

from customs_backend.domain import (
    CaseRecord,
    ConversationMessageExchange,
    ConversationMessageRecord,
    ConversationRecord,
    CorrectionProposalDecisionRecord,
    CorrectionProposalRecord,
    DocumentRecord,
    FindingDecisionRecord,
    FindingDecisionStatus,
    JobRecord,
    ReviewRunRecord,
)


class ConflictError(RuntimeError):
    """A caller supplied stale state or reused an idempotency key differently."""


class NotFoundError(RuntimeError):
    pass


class AuthorizationError(RuntimeError):
    pass


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
    def get_run(self, org_id: str, review_run_id: str) -> ReviewRunRecord | None: ...
    def list_runs(self, org_id: str, case_id: str) -> list[ReviewRunRecord]: ...


class ObjectStore(Protocol):
    def health(self) -> bool: ...
    def put_text(self, *, key: str, content: str, content_sha256: str) -> None: ...
    def get_text(self, *, key: str, expected_sha256: str) -> str: ...


class AgentClient(Protocol):
    def review(self, request: Any) -> dict[str, Any]: ...
    def answer(self, request: Any) -> dict[str, Any]: ...
    def metadata(self) -> dict[str, Any]: ...


class TokenVerifier(Protocol):
    def verify(self, token: str) -> Any: ...
