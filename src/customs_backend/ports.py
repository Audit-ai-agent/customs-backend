from __future__ import annotations

from collections.abc import Callable
from typing import Any, Protocol

from customs_backend.domain import (
    AgentAnswerResponse,
    AgentReviewRequest,
    AgentRevisionRequest,
    AuditRecord,
    CaseRecord,
    ChatInteractionRecord,
    ChatInteractionState,
    ChatRecord,
    CorrectionProposalRecord,
    DocumentRecord,
    HumanDecisionRecord,
    JobRecord,
    OutboxRecord,
    Principal,
    ReviewRunRecord,
    StageEvent,
    SupportingDocumentRecord,
    VerificationAssertionRecord,
)


class ConflictError(RuntimeError):
    """A caller supplied stale state or reused an idempotency key incorrectly."""


class NotFoundError(RuntimeError):
    pass


class AuthorizationError(RuntimeError):
    pass


class AgentResponseRejectedError(RuntimeError):
    """The Agent definitely returned a response, but it was unsafe to consume."""


class QuotaExceededError(RuntimeError):
    """A bounded user interaction quota or rate limit was reached."""


class WorkbenchRepository(Protocol):
    def health(self) -> bool: ...
    def create_case(self, record: CaseRecord, audit: AuditRecord) -> CaseRecord: ...
    def get_case(self, org_id: str, case_id: str) -> CaseRecord | None: ...
    def list_cases(self, org_id: str) -> list[CaseRecord]: ...
    def add_document(
        self,
        record: DocumentRecord,
        *,
        expected_revision: int,
        new_manifest_digest: str,
        audit: AuditRecord,
    ) -> CaseRecord: ...
    def list_documents(self, org_id: str, case_id: str) -> list[DocumentRecord]: ...
    def save_supporting_document(
        self,
        record: SupportingDocumentRecord,
        audit: AuditRecord,
        *,
        expected_revision: int,
    ) -> SupportingDocumentRecord: ...
    def get_supporting_document(
        self, org_id: str, case_id: str, document_id: str
    ) -> SupportingDocumentRecord | None: ...
    def list_supporting_documents(
        self, org_id: str, case_id: str
    ) -> list[SupportingDocumentRecord]: ...
    def add_verification_assertion(
        self,
        *,
        org_id: str,
        case_id: str,
        document_id: str,
        assertion: VerificationAssertionRecord,
        audit: AuditRecord,
        expected_revision: int,
    ) -> SupportingDocumentRecord: ...
    def enqueue_job(
        self,
        job: JobRecord,
        event: StageEvent,
        outbox: OutboxRecord,
        audit: AuditRecord,
    ) -> tuple[JobRecord, bool]: ...
    def enqueue_review_snapshot(
        self,
        *,
        org_id: str,
        case_id: str,
        idempotency_key: str,
        idempotency_request_digest: str,
        build: Callable[
            [CaseRecord, list[DocumentRecord], list[SupportingDocumentRecord]],
            tuple[JobRecord, StageEvent, OutboxRecord, AuditRecord],
        ],
    ) -> tuple[JobRecord, bool]: ...
    def get_job(self, org_id: str, job_id: str) -> JobRecord | None: ...
    def get_job_by_idempotency_key(self, org_id: str, idempotency_key: str) -> JobRecord | None: ...
    def claim_job(
        self,
        *,
        org_id: str,
        job_id: str,
        lease_id: str,
        lease_expires_at: Any,
        event: StageEvent,
        audit: AuditRecord,
    ) -> JobRecord | None: ...
    def start_agent_call_once(
        self,
        *,
        org_id: str,
        job_id: str,
        expected_lease_id: str,
        endpoint: str,
        request_sha256: str,
        audit: AuditRecord,
    ) -> JobRecord: ...
    def mark_agent_delivery_uncertain(
        self,
        *,
        org_id: str,
        job_id: str,
        expected_lease_id: str | None,
        error_code: str,
        event: StageEvent,
        audit: AuditRecord,
    ) -> JobRecord: ...
    def mark_agent_response_rejected(
        self,
        *,
        org_id: str,
        job_id: str,
        expected_lease_id: str,
        error_code: str,
        event: StageEvent,
        audit: AuditRecord,
    ) -> JobRecord: ...
    def mark_agent_response_persistence_uncertain(
        self,
        *,
        org_id: str,
        job_id: str,
        expected_lease_id: str,
        error_code: str,
        event: StageEvent,
        audit: AuditRecord,
    ) -> JobRecord: ...
    def reconcile_delivery_uncertain(
        self,
        *,
        org_id: str,
        job_id: str,
        reconciled_by: str,
        evidence_reference_sha256: str,
        audit: AuditRecord,
    ) -> JobRecord: ...
    def update_job(
        self,
        job: JobRecord,
        event: StageEvent,
        audit: AuditRecord,
        *,
        expected_lease_id: str | None = None,
    ) -> JobRecord: ...
    def list_stage_events(self, org_id: str, job_id: str) -> list[StageEvent]: ...
    def save_run(self, run: ReviewRunRecord, audit: AuditRecord) -> ReviewRunRecord: ...
    def get_provider_result(self, org_id: str, review_run_id: str) -> dict[str, Any] | None: ...
    def complete_job(
        self,
        *,
        run: ReviewRunRecord,
        case_drift_run: ReviewRunRecord,
        provider_result: dict[str, Any],
        job: JobRecord,
        event: StageEvent,
        run_audit: AuditRecord,
        job_audit: AuditRecord,
        expected_lease_id: str,
    ) -> ReviewRunRecord: ...
    def get_run(self, org_id: str, review_run_id: str) -> ReviewRunRecord | None: ...
    def list_runs(self, org_id: str, case_id: str) -> list[ReviewRunRecord]: ...
    def save_run_decision(
        self,
        decision: HumanDecisionRecord,
        audit: AuditRecord,
        *,
        expected_case_revision: int,
    ) -> HumanDecisionRecord: ...
    def list_decisions(self, org_id: str, review_run_id: str) -> list[HumanDecisionRecord]: ...
    def get_proposal_decision(
        self, org_id: str, proposal_id: str
    ) -> HumanDecisionRecord | None: ...
    def get_decision_by_idempotency_key(
        self, org_id: str, idempotency_key: str
    ) -> HumanDecisionRecord | None: ...
    def create_correction_proposal(
        self, proposal: CorrectionProposalRecord, audit: AuditRecord
    ) -> CorrectionProposalRecord: ...
    def get_correction_proposal(
        self, org_id: str, proposal_id: str
    ) -> CorrectionProposalRecord | None: ...
    def get_correction_proposal_by_idempotency_key(
        self, org_id: str, idempotency_key: str
    ) -> CorrectionProposalRecord | None: ...
    def list_correction_proposals(
        self, org_id: str, review_run_id: str
    ) -> list[CorrectionProposalRecord]: ...
    def decide_correction_proposal(
        self,
        *,
        proposal: CorrectionProposalRecord,
        decision: HumanDecisionRecord,
        audit: AuditRecord,
        job: JobRecord | None = None,
        event: StageEvent | None = None,
        outbox: OutboxRecord | None = None,
    ) -> CorrectionProposalRecord: ...
    def reserve_chat_interaction(
        self, interaction: ChatInteractionRecord
    ) -> tuple[ChatInteractionRecord, bool]: ...
    def start_chat_call_once(
        self, *, org_id: str, interaction_id: str
    ) -> ChatInteractionRecord: ...
    def complete_chat_interaction(
        self,
        *,
        interaction: ChatInteractionRecord,
        message: ChatRecord,
        audit: AuditRecord,
    ) -> ChatRecord: ...
    def mark_chat_interaction_terminal(
        self,
        *,
        org_id: str,
        interaction_id: str,
        state: ChatInteractionState,
        error_code: str,
        audit: AuditRecord,
    ) -> ChatInteractionRecord: ...
    def get_chat_interaction_by_idempotency_key(
        self, org_id: str, idempotency_key: str
    ) -> ChatInteractionRecord | None: ...
    def get_chat_by_idempotency_key(
        self, org_id: str, idempotency_key: str
    ) -> ChatRecord | None: ...
    def list_chat(self, org_id: str, review_run_id: str) -> list[ChatRecord]: ...
    def list_audit(self, org_id: str, case_id: str) -> list[AuditRecord]: ...
    def claim_pending_outbox(
        self, *, lease_id: str, lease_expires_at: Any, limit: int = 100
    ) -> list[OutboxRecord]: ...
    def mark_outbox_published(self, outbox_id: str, *, lease_id: str) -> None: ...
    def release_outbox_claim(self, outbox_id: str, *, lease_id: str) -> None: ...


class ObjectStore(Protocol):
    def health(self) -> bool: ...
    def put_text(self, *, key: str, content: str, content_sha256: str) -> None: ...
    def get_text(self, *, key: str, expected_sha256: str) -> str: ...


class JobPublisher(Protocol):
    def health(self) -> bool: ...
    def publish(self, *, org_id: str, job_id: str, outbox_id: str) -> None: ...


class AgentClient(Protocol):
    def review(self, request: AgentReviewRequest) -> dict[str, Any]: ...
    def revise(self, request: AgentRevisionRequest) -> dict[str, Any]: ...
    def answer(
        self,
        *,
        provider_result: dict[str, Any],
        question: str,
        question_id: str,
    ) -> AgentAnswerResponse: ...
    def metadata(self) -> dict[str, Any]: ...


class TokenVerifier(Protocol):
    def verify(self, token: str) -> Principal: ...
