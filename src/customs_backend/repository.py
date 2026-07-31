from __future__ import annotations

from collections.abc import Callable
from datetime import datetime
from threading import RLock
from typing import Any

from sqlalchemy import (
    JSON,
    DateTime,
    Integer,
    String,
    UniqueConstraint,
    create_engine,
    func,
    select,
    text,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, Session, mapped_column
from sqlalchemy.schema import ForeignKey

from customs_backend.domain import (
    AgentCallState,
    AuditRecord,
    CaseRecord,
    CaseStatus,
    ChatInteractionRecord,
    ChatInteractionState,
    ChatRecord,
    CorrectionProposalRecord,
    CorrectionProposalStatus,
    DocumentRecord,
    HumanDecisionRecord,
    JobRecord,
    JobStatus,
    MAX_SUPPORTING_DOCUMENTS_PER_CASE,
    OutboxRecord,
    ReviewRunRecord,
    StageEvent,
    SupportingDocumentRecord,
    VerificationAssertionRecord,
    utc_now,
)
from customs_backend.experience import (
    REVIEW_TIMED_STAGE_CODES,
    AssistantConversationRecord,
    AssistantJobRecord,
    AssistantJobState,
    AssistantMessageRecord,
    AssistantStageEvent,
    ReviewPresentationRecord,
)
from customs_backend.ports import ConflictError, QuotaExceededError

_ACTIVE_JOB_STATUSES = {
    JobStatus.queued,
    JobStatus.running,
    JobStatus.delivery_uncertain,
    JobStatus.response_persistence_uncertain,
}
_ACTIVE_JOB_STATUS_VALUES = tuple(item.value for item in _ACTIVE_JOB_STATUSES)
_CHAT_QUOTA_STATES = frozenset(ChatInteractionState)
_CHAT_QUOTA_STATE_VALUES = tuple(item.value for item in _CHAT_QUOTA_STATES)
_REVIEW_TIMED_STAGE_CODE_SET = frozenset(REVIEW_TIMED_STAGE_CODES)


def _review_stage_duration_sample(events: list[StageEvent]) -> dict[str, int]:
    """Aggregate retries inside one successful review into one stage sample."""

    sample: dict[str, int] = {}
    for event in events:
        if event.stage not in _REVIEW_TIMED_STAGE_CODE_SET:
            continue
        duration = event.detail.get("duration_ms")
        if isinstance(duration, bool) or not isinstance(duration, int) or duration < 0:
            continue
        sample[event.stage] = sample.get(event.stage, 0) + duration
    return sample


def _review_total_duration_ms(events: list[StageEvent]) -> int | None:
    durations: list[int] = []
    for event in events:
        duration = event.detail.get("duration_ms")
        if isinstance(duration, bool) or not isinstance(duration, int) or duration < 0:
            continue
        durations.append(duration)
    return sum(durations) if durations else None


def _updated_assistant_job(
    record: AssistantJobRecord,
    **updates: Any,
) -> AssistantJobRecord:
    payload = record.model_dump(mode="python")
    payload.update(updates)
    return AssistantJobRecord.model_validate(payload)


def _validate_assistant_event_append(
    record: AssistantJobRecord,
    event: AssistantStageEvent,
) -> None:
    if (
        event.assistant_job_id != record.assistant_job_id
        or event.sequence != len(record.events) + 1
        or len(record.events) >= 100
    ):
        raise ConflictError("assistant event is not the next bounded job event")


def _assistant_completion_events(
    record: AssistantJobRecord,
    events: tuple[AssistantStageEvent, AssistantStageEvent],
) -> list[AssistantStageEvent]:
    persistence_completed, terminal_completed = events
    if (
        not record.events
        or record.events[-1].stage_code != "persistence"
        or record.events[-1].state != "started"
        or persistence_completed.stage_code != "persistence"
        or persistence_completed.state != "completed"
        or terminal_completed.stage_code != "completed"
        or terminal_completed.state != "completed"
    ):
        raise ConflictError("assistant completion events do not prove persistence")
    accumulated = list(record.events)
    current = record
    for event in events:
        _validate_assistant_event_append(current, event)
        accumulated.append(event)
        current = _updated_assistant_job(current, events=accumulated)
    return accumulated


class InMemoryWorkbenchRepository:
    """Thread-safe test adapter implementing the same atomic aggregate operations."""

    def __init__(self) -> None:
        self._lock = RLock()
        self._cases: dict[str, CaseRecord] = {}
        self._documents: dict[str, DocumentRecord] = {}
        self._supporting_documents: dict[tuple[str, str], SupportingDocumentRecord] = {}
        self._jobs: dict[str, JobRecord] = {}
        self._idempotency: dict[tuple[str, str], str] = {}
        self._runs: dict[str, ReviewRunRecord] = {}
        self._provider_results: dict[str, dict[str, Any]] = {}
        self._decisions: dict[str, HumanDecisionRecord] = {}
        self._correction_proposals: dict[str, CorrectionProposalRecord] = {}
        self._chat_interactions: dict[str, ChatInteractionRecord] = {}
        self._chat: dict[str, ChatRecord] = {}
        self._audits: dict[str, AuditRecord] = {}
        self._outbox: dict[str, OutboxRecord] = {}
        self._events: dict[str, list[StageEvent]] = {}
        self._review_presentations: dict[str, ReviewPresentationRecord] = {}
        self._assistant_conversations: dict[str, AssistantConversationRecord] = {}
        self._assistant_jobs: dict[str, AssistantJobRecord] = {}
        self._assistant_messages: dict[str, AssistantMessageRecord] = {}

    def health(self) -> bool:
        return True

    @staticmethod
    def _copy(value: Any) -> Any:
        if isinstance(value, list):
            return [item.model_copy(deep=True) for item in value]
        return value.model_copy(deep=True)

    def create_case(self, record: CaseRecord, audit: AuditRecord) -> CaseRecord:
        with self._lock:
            if record.case_id in self._cases:
                raise ConflictError("case_id already exists")
            self._cases[record.case_id] = self._copy(record)
            self._audits[audit.audit_id] = self._copy(audit)
            return self._copy(record)

    def get_case(self, org_id: str, case_id: str) -> CaseRecord | None:
        with self._lock:
            record = self._cases.get(case_id)
            return self._copy(record) if record and record.org_id == org_id else None

    def list_cases(self, org_id: str) -> list[CaseRecord]:
        with self._lock:
            values = [item for item in self._cases.values() if item.org_id == org_id]
            return self._copy(sorted(values, key=lambda item: item.created_at, reverse=True))

    def add_document(
        self,
        record: DocumentRecord,
        *,
        expected_revision: int,
        new_manifest_digest: str,
        audit: AuditRecord,
    ) -> CaseRecord:
        with self._lock:
            case = self._cases.get(record.case_id)
            if not case or case.org_id != record.org_id:
                raise ConflictError("case does not exist")
            if case.revision != expected_revision:
                raise ConflictError("case revision is stale")
            if record.document_id in self._documents:
                raise ConflictError("document_id already exists")
            case_documents = [
                item
                for item in self._documents.values()
                if item.org_id == record.org_id and item.case_id == record.case_id
            ]
            if record.document_type == "SUPPORTING":
                supporting_source_count = sum(
                    1 for item in case_documents if item.document_type == "SUPPORTING"
                )
                if supporting_source_count >= MAX_SUPPORTING_DOCUMENTS_PER_CASE:
                    raise ConflictError("Case supporting-document source limit of 100 is exhausted")
            updated = case.model_copy(
                update={
                    "revision": case.revision + 1,
                    "document_manifest_digest": new_manifest_digest,
                    "updated_at": record.created_at,
                }
            )
            self._documents[record.document_id] = self._copy(record)
            self._cases[case.case_id] = self._copy(updated)
            self._audits[audit.audit_id] = self._copy(audit)
            return self._copy(updated)

    def list_documents(self, org_id: str, case_id: str) -> list[DocumentRecord]:
        with self._lock:
            values = [
                item
                for item in self._documents.values()
                if item.org_id == org_id and item.case_id == case_id
            ]
            return self._copy(sorted(values, key=lambda item: item.created_at))

    def save_supporting_document(
        self,
        record: SupportingDocumentRecord,
        audit: AuditRecord,
        *,
        expected_revision: int,
    ) -> SupportingDocumentRecord:
        with self._lock:
            case = self._cases.get(record.case_id)
            source = self._documents.get(record.document_id)
            key = (record.case_id, record.document_id)
            if not case or case.org_id != record.org_id:
                raise ConflictError("case does not exist")
            if case.revision != expected_revision:
                raise ConflictError("case revision is stale")
            if (
                not source
                or source.case_id != record.case_id
                or source.org_id != record.org_id
                or source.document_type != "SUPPORTING"
                or source.content_sha256 != record.source_sha256
            ):
                raise ConflictError("supporting document source is missing or hash-mismatched")
            if key in self._supporting_documents:
                raise ConflictError("supporting document already exists")
            current_count = sum(
                1
                for (stored_case_id, _document_id), item in self._supporting_documents.items()
                if stored_case_id == record.case_id and item.org_id == record.org_id
            )
            if current_count >= MAX_SUPPORTING_DOCUMENTS_PER_CASE:
                raise ConflictError("Case supporting-document limit of 100 is exhausted")
            self._supporting_documents[key] = self._copy(record)
            self._cases[case.case_id] = case.model_copy(
                update={"revision": case.revision + 1, "updated_at": record.created_at}
            )
            self._audits[audit.audit_id] = self._copy(audit)
            return self._copy(record)

    def get_supporting_document(
        self, org_id: str, case_id: str, document_id: str
    ) -> SupportingDocumentRecord | None:
        with self._lock:
            record = self._supporting_documents.get((case_id, document_id))
            return self._copy(record) if record and record.org_id == org_id else None

    def list_supporting_documents(
        self, org_id: str, case_id: str
    ) -> list[SupportingDocumentRecord]:
        with self._lock:
            values = [
                item
                for (stored_case_id, _document_id), item in self._supporting_documents.items()
                if stored_case_id == case_id and item.org_id == org_id
            ]
            return self._copy(sorted(values, key=lambda item: item.created_at))

    def add_verification_assertion(
        self,
        *,
        org_id: str,
        case_id: str,
        document_id: str,
        assertion: VerificationAssertionRecord,
        audit: AuditRecord,
        expected_revision: int,
    ) -> SupportingDocumentRecord:
        with self._lock:
            key = (case_id, document_id)
            record = self._supporting_documents.get(key)
            if record is None or record.org_id != org_id:
                raise ConflictError("supporting document does not exist")
            case = self._cases.get(case_id)
            if case is None or case.org_id != org_id or case.revision != expected_revision:
                raise ConflictError("case revision is stale")
            source = self._documents.get(document_id)
            if source is None or source.org_id != org_id or source.case_id != case_id:
                raise ConflictError("supporting source document does not exist")
            if assertion.method == "human_approver" and assertion.asserted_by in {
                source.created_by,
                record.created_by,
            }:
                raise ConflictError("supporting verification requires an independent approver")
            if any(
                item.assertion_id == assertion.assertion_id
                for item in record.verification_assertions
            ):
                return self._copy(record)
            updated = record.model_copy(
                update={"verification_assertions": [*record.verification_assertions, assertion]}
            )
            self._supporting_documents[key] = self._copy(updated)
            self._cases[case_id] = case.model_copy(
                update={"revision": case.revision + 1, "updated_at": assertion.asserted_at}
            )
            self._audits[audit.audit_id] = self._copy(audit)
            return self._copy(updated)

    def enqueue_job(
        self,
        job: JobRecord,
        event: StageEvent,
        outbox: OutboxRecord,
        audit: AuditRecord,
    ) -> tuple[JobRecord, bool]:
        with self._lock:
            key = (job.org_id, job.idempotency_key)
            existing_id = self._idempotency.get(key)
            if existing_id:
                existing = self._jobs[existing_id]
                if existing.request_digest != job.request_digest:
                    raise ConflictError("idempotency key was reused with a different request")
                return self._copy(existing), False
            active = [
                item
                for item in self._jobs.values()
                if item.org_id == job.org_id
                and item.case_id == job.case_id
                and item.case_revision == job.case_revision
                and item.status in _ACTIVE_JOB_STATUSES
            ]
            matching = next(
                (
                    item
                    for item in active
                    if item.logical_request_sha256 == job.logical_request_sha256
                ),
                None,
            )
            if matching is not None:
                if matching.created_by != job.created_by:
                    raise ConflictError("an active logical request belongs to another actor")
                return self._copy(matching), False
            if any(
                item.status
                in {JobStatus.delivery_uncertain, JobStatus.response_persistence_uncertain}
                for item in active
            ):
                raise ConflictError("delivery uncertainty must be reconciled before a new call")
            case = self._cases.get(job.case_id)
            if case is None or case.org_id != job.org_id:
                raise ConflictError("case does not exist")
            if job.kind == "review" and case.status == CaseStatus.closed:
                raise ConflictError("a new review cannot target a closed case")
            self._jobs[job.job_id] = self._copy(job)
            self._idempotency[key] = job.job_id
            self._events[job.job_id] = [self._copy(event)]
            self._outbox[outbox.outbox_id] = self._copy(outbox)
            self._audits[audit.audit_id] = self._copy(audit)
            self._cases[job.case_id] = case.model_copy(
                update={"status": CaseStatus.in_review, "updated_at": job.created_at}
            )
            return self._copy(job), True

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
    ) -> tuple[JobRecord, bool]:
        with self._lock:
            existing_id = self._idempotency.get((org_id, idempotency_key))
            if existing_id is not None:
                existing = self._jobs[existing_id]
                if existing.idempotency_request_digest != idempotency_request_digest:
                    raise ConflictError("idempotency key was reused with a different request")
                return self._copy(existing), False
            case = self._cases.get(case_id)
            if case is None or case.org_id != org_id:
                raise ConflictError("case does not exist")
            documents = sorted(
                (
                    item
                    for item in self._documents.values()
                    if item.org_id == org_id and item.case_id == case_id
                ),
                key=lambda item: (item.created_at, item.document_id),
            )
            supporting = sorted(
                (
                    item
                    for (stored_case_id, _), item in self._supporting_documents.items()
                    if stored_case_id == case_id and item.org_id == org_id
                ),
                key=lambda item: (item.created_at, item.document_id),
            )
            job, event, outbox, audit = build(
                self._copy(case), self._copy(documents), self._copy(supporting)
            )
            if job.idempotency_request_digest != idempotency_request_digest:
                raise ConflictError("snapshot builder changed the idempotency request digest")
            return self.enqueue_job(job, event, outbox, audit)

    def get_job(self, org_id: str, job_id: str) -> JobRecord | None:
        with self._lock:
            record = self._jobs.get(job_id)
            return self._copy(record) if record and record.org_id == org_id else None

    def get_job_by_idempotency_key(self, org_id: str, idempotency_key: str) -> JobRecord | None:
        with self._lock:
            job_id = self._idempotency.get((org_id, idempotency_key))
            record = self._jobs.get(job_id) if job_id is not None else None
            return self._copy(record) if record is not None else None

    def claim_job(
        self,
        *,
        org_id: str,
        job_id: str,
        lease_id: str,
        lease_expires_at: Any,
        event: StageEvent,
        audit: AuditRecord,
    ) -> JobRecord | None:
        with self._lock:
            record = self._jobs.get(job_id)
            if record is None or record.org_id != org_id:
                return None
            active_lease = (
                record.status.value == "running"
                and record.lease_id is not None
                and record.lease_expires_at is not None
                and record.lease_expires_at > utc_now()
            )
            if (
                record.status.value not in {"queued", "running"}
                or record.agent_call_state != AgentCallState.not_started
                or active_lease
            ):
                return None
            claimed = record.model_copy(
                update={
                    "status": JobStatus.running,
                    "error_code": None,
                    "lease_id": lease_id,
                    "lease_expires_at": lease_expires_at,
                    "attempt_count": record.attempt_count + 1,
                    "updated_at": utc_now(),
                }
            )
            self._jobs[job_id] = self._copy(claimed)
            self._events.setdefault(job_id, []).append(self._copy(event))
            self._audits[audit.audit_id] = self._copy(audit)
            return self._copy(claimed)

    def start_agent_call_once(
        self,
        *,
        org_id: str,
        job_id: str,
        expected_lease_id: str,
        endpoint: str,
        request_sha256: str,
        audit: AuditRecord,
    ) -> JobRecord:
        with self._lock:
            record = self._jobs.get(job_id)
            if (
                record is None
                or record.org_id != org_id
                or record.status != JobStatus.running
                or record.lease_id != expected_lease_id
                or record.agent_call_state != AgentCallState.not_started
            ):
                raise ConflictError("Agent call-once barrier is already closed")
            started = record.model_copy(
                update={
                    "agent_call_state": AgentCallState.started,
                    "agent_call_endpoint": endpoint,
                    "agent_call_request_sha256": request_sha256,
                    "agent_call_started_at": utc_now(),
                    "updated_at": utc_now(),
                }
            )
            self._jobs[job_id] = self._copy(started)
            self._audits[audit.audit_id] = self._copy(audit)
            return self._copy(started)

    def mark_agent_delivery_uncertain(
        self,
        *,
        org_id: str,
        job_id: str,
        expected_lease_id: str | None,
        error_code: str,
        event: StageEvent,
        audit: AuditRecord,
    ) -> JobRecord:
        with self._lock:
            record = self._jobs.get(job_id)
            if record is None or record.org_id != org_id:
                raise ConflictError("job does not exist")
            if expected_lease_id is not None and record.lease_id != expected_lease_id:
                raise ConflictError("job lease is stale")
            if record.agent_call_state == AgentCallState.not_started:
                raise ConflictError("Agent delivery was never attempted")
            if record.status == JobStatus.succeeded:
                return self._copy(record)
            uncertain = record.model_copy(
                update={
                    "status": JobStatus.delivery_uncertain,
                    "agent_call_state": AgentCallState.delivery_uncertain,
                    "error_code": error_code,
                    "lease_id": None,
                    "lease_expires_at": None,
                    "updated_at": utc_now(),
                }
            )
            self._jobs[job_id] = self._copy(uncertain)
            self._events.setdefault(job_id, []).append(self._copy(event))
            self._audits[audit.audit_id] = self._copy(audit)
            return self._copy(uncertain)

    def mark_agent_response_rejected(
        self,
        *,
        org_id: str,
        job_id: str,
        expected_lease_id: str,
        error_code: str,
        event: StageEvent,
        audit: AuditRecord,
    ) -> JobRecord:
        with self._lock:
            record = self._jobs.get(job_id)
            if (
                record is None
                or record.org_id != org_id
                or record.lease_id != expected_lease_id
                or record.status != JobStatus.running
                or record.agent_call_state != AgentCallState.started
            ):
                raise ConflictError("job lease is stale")
            rejected = record.model_copy(
                update={
                    "status": JobStatus.response_received_rejected,
                    "agent_call_state": AgentCallState.response_received_rejected,
                    "error_code": error_code,
                    "lease_id": None,
                    "lease_expires_at": None,
                    "updated_at": utc_now(),
                }
            )
            self._jobs[job_id] = self._copy(rejected)
            self._events.setdefault(job_id, []).append(self._copy(event))
            self._audits[audit.audit_id] = self._copy(audit)
            return self._copy(rejected)

    def mark_agent_pre_execution_retryable(
        self,
        *,
        org_id: str,
        job_id: str,
        expected_lease_id: str,
        expected_endpoint: str,
        expected_request_sha256: str,
        error_code: str,
        terminal: bool,
        event: StageEvent,
        audit: AuditRecord,
    ) -> JobRecord:
        with self._lock:
            record = self._jobs.get(job_id)
            if (
                record is None
                or record.org_id != org_id
                or record.status != JobStatus.running
                or record.agent_call_state != AgentCallState.started
                or record.lease_id != expected_lease_id
                or record.agent_call_endpoint != expected_endpoint
                or record.agent_call_request_sha256 != expected_request_sha256
            ):
                raise ConflictError("Agent pre-execution retry binding is stale")
            retry_state = record.model_copy(
                update={
                    "status": JobStatus.failed if terminal else JobStatus.queued,
                    "agent_call_state": AgentCallState.not_started,
                    "agent_call_endpoint": None,
                    "agent_call_request_sha256": None,
                    "agent_call_started_at": None,
                    "error_code": error_code,
                    "lease_id": None,
                    "lease_expires_at": None,
                    "updated_at": utc_now(),
                }
            )
            self._jobs[job_id] = self._copy(retry_state)
            self._events.setdefault(job_id, []).append(self._copy(event))
            self._audits[audit.audit_id] = self._copy(audit)
            return self._copy(retry_state)

    def mark_agent_response_persistence_uncertain(
        self,
        *,
        org_id: str,
        job_id: str,
        expected_lease_id: str,
        error_code: str,
        event: StageEvent,
        audit: AuditRecord,
    ) -> JobRecord:
        with self._lock:
            record = self._jobs.get(job_id)
            if (
                record is None
                or record.org_id != org_id
                or record.lease_id != expected_lease_id
                or record.status != JobStatus.running
                or record.agent_call_state != AgentCallState.started
            ):
                raise ConflictError("job lease is stale")
            uncertain = record.model_copy(
                update={
                    "status": JobStatus.response_persistence_uncertain,
                    "agent_call_state": AgentCallState.response_persistence_uncertain,
                    "error_code": error_code,
                    "lease_id": None,
                    "lease_expires_at": None,
                    "updated_at": utc_now(),
                }
            )
            self._jobs[job_id] = self._copy(uncertain)
            self._events.setdefault(job_id, []).append(self._copy(event))
            self._audits[audit.audit_id] = self._copy(audit)
            return self._copy(uncertain)

    def reconcile_delivery_uncertain(
        self,
        *,
        org_id: str,
        job_id: str,
        reconciled_by: str,
        evidence_reference_sha256: str,
        audit: AuditRecord,
    ) -> JobRecord:
        with self._lock:
            record = self._jobs.get(job_id)
            if (
                record is None
                or record.org_id != org_id
                or record.status != JobStatus.delivery_uncertain
                or record.agent_call_state != AgentCallState.delivery_uncertain
            ):
                raise ConflictError("only a delivery-uncertain Job can be reconciled")
            now = utc_now()
            reconciled = record.model_copy(
                update={
                    "status": JobStatus.failed,
                    "agent_call_state": AgentCallState.delivery_reconciled,
                    "error_code": "delivery_confirmed_not_processed",
                    "delivery_reconciled_by": reconciled_by,
                    "delivery_reconciled_at": now,
                    "delivery_reconciliation_evidence_sha256": evidence_reference_sha256,
                    "updated_at": now,
                }
            )
            self._jobs[job_id] = self._copy(reconciled)
            active_others = any(
                item.job_id != job_id
                and item.org_id == org_id
                and item.case_id == record.case_id
                and item.case_revision == record.case_revision
                and item.status in _ACTIVE_JOB_STATUSES
                for item in self._jobs.values()
            )
            case = self._cases.get(record.case_id)
            if case is not None and not active_others:
                self._cases[case.case_id] = case.model_copy(
                    update={"status": CaseStatus.open, "updated_at": now}
                )
            self._audits[audit.audit_id] = self._copy(audit)
            return self._copy(reconciled)

    def update_job(
        self,
        job: JobRecord,
        event: StageEvent,
        audit: AuditRecord,
        *,
        expected_lease_id: str | None = None,
    ) -> JobRecord:
        with self._lock:
            existing = self._jobs.get(job.job_id)
            if not existing or existing.org_id != job.org_id:
                raise ConflictError("job does not exist")
            if expected_lease_id is not None and existing.lease_id != expected_lease_id:
                raise ConflictError("job lease is stale")
            if existing.agent_call_state != job.agent_call_state:
                raise ConflictError("generic Job update cannot change Agent call-once state")
            self._jobs[job.job_id] = self._copy(job)
            self._events.setdefault(job.job_id, []).append(self._copy(event))
            self._audits[audit.audit_id] = self._copy(audit)
            return self._copy(job)

    def list_stage_events(self, org_id: str, job_id: str) -> list[StageEvent]:
        with self._lock:
            job = self._jobs.get(job_id)
            if not job or job.org_id != org_id:
                return []
            return self._copy(self._events.get(job_id, []))

    def recent_review_durations_ms(
        self,
        *,
        org_id: str,
        evidence_profile: str,
        document_count: int,
        limit: int = 30,
    ) -> list[int]:
        bounded_limit = max(0, min(limit, 30))
        if bounded_limit == 0:
            return []
        with self._lock:
            jobs = sorted(
                (
                    item
                    for item in self._jobs.values()
                    if item.org_id == org_id
                    and item.status == JobStatus.succeeded
                    and item.request.get("experience_api_version") == "3.0"
                    and item.request.get("evidence_profile") == evidence_profile
                    and item.request.get("document_count") == document_count
                ),
                key=lambda item: (
                    item.agent_call_started_at or item.updated_at,
                    item.job_id,
                ),
                reverse=True,
            )[:bounded_limit]
            values: list[int] = []
            for job in jobs:
                duration = _review_total_duration_ms(self._events.get(job.job_id, []))
                if duration is not None:
                    values.append(duration)
            return values

    def recent_review_stage_durations_ms(
        self,
        *,
        org_id: str,
        evidence_profile: str,
        document_count: int,
        limit: int = 30,
    ) -> list[dict[str, int]]:
        bounded_limit = max(0, min(limit, 30))
        if bounded_limit == 0:
            return []
        with self._lock:
            jobs = sorted(
                (
                    item
                    for item in self._jobs.values()
                    if item.org_id == org_id
                    and item.status == JobStatus.succeeded
                    and item.request.get("experience_api_version") == "3.0"
                    and item.request.get("evidence_profile") == evidence_profile
                    and item.request.get("document_count") == document_count
                ),
                key=lambda item: (
                    item.agent_call_started_at or item.updated_at,
                    item.job_id,
                ),
                reverse=True,
            )[:bounded_limit]
            return [_review_stage_duration_sample(self._events.get(job.job_id, [])) for job in jobs]

    def save_run(self, run: ReviewRunRecord, audit: AuditRecord) -> ReviewRunRecord:
        with self._lock:
            existing = self._runs.get(run.review_run_id)
            if existing:
                if existing.result_digest != run.result_digest:
                    raise ConflictError("immutable review run already contains another result")
                return self._copy(existing)
            if run.parent_run_id:
                parent = self._runs.get(run.parent_run_id)
                if not parent or parent.case_id != run.case_id:
                    raise ConflictError("parent review run does not exist in the case")
            self._runs[run.review_run_id] = self._copy(run)
            self._audits[audit.audit_id] = self._copy(audit)
            return self._copy(run)

    def get_provider_result(self, org_id: str, review_run_id: str) -> dict[str, Any] | None:
        with self._lock:
            run = self._runs.get(review_run_id)
            value = self._provider_results.get(review_run_id)
            return dict(value) if run and run.org_id == org_id and value is not None else None

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
        presentation: ReviewPresentationRecord | None = None,
    ) -> ReviewRunRecord:
        with self._lock:
            current = self._jobs.get(job.job_id)
            current_case = self._cases.get(job.case_id)
            if (
                current is None
                or current.org_id != job.org_id
                or current.lease_id != expected_lease_id
                or current.status.value != "running"
                or current.agent_call_state != AgentCallState.started
                or job.agent_call_state != AgentCallState.completed
                or current.agent_call_endpoint != job.agent_call_endpoint
                or current.agent_call_request_sha256 != job.agent_call_request_sha256
                or current_case is None
                or current_case.org_id != job.org_id
            ):
                raise ConflictError("job lease is stale")
            selected_run = case_drift_run if current_case.revision != run.case_revision else run
            if (
                selected_run.review_run_id != run.review_run_id
                or selected_run.provider_result_sha256 != run.provider_result_sha256
            ):
                raise ConflictError("case-drift Run is not bound to the provider result")
            existing = self._runs.get(selected_run.review_run_id)
            if existing and existing.result_digest != selected_run.result_digest:
                raise ConflictError("immutable review run already contains another result")
            self._runs[selected_run.review_run_id] = self._copy(selected_run)
            self._provider_results[selected_run.review_run_id] = dict(provider_result)
            if presentation is not None:
                if (
                    presentation.review_run_id != run.review_run_id
                    or presentation.org_id != run.org_id
                    or presentation.case_id != run.case_id
                    or presentation.bound_provider_result_sha256 != run.provider_result_sha256
                    or presentation.bound_effective_result_digest != run.result_digest
                ):
                    raise ConflictError("experience presentation is not bound to its effective Run")
                if presentation.bound_effective_result_digest == selected_run.result_digest:
                    self._review_presentations[selected_run.review_run_id] = self._copy(
                        presentation
                    )
            self._jobs[job.job_id] = self._copy(job)
            self._events.setdefault(job.job_id, []).append(self._copy(event))
            self._audits[run_audit.audit_id] = self._copy(run_audit)
            self._audits[job_audit.audit_id] = self._copy(job_audit)
            return self._copy(selected_run)

    def get_run(self, org_id: str, review_run_id: str) -> ReviewRunRecord | None:
        with self._lock:
            record = self._runs.get(review_run_id)
            return self._copy(record) if record and record.org_id == org_id else None

    def list_runs(self, org_id: str, case_id: str) -> list[ReviewRunRecord]:
        with self._lock:
            values = [
                item
                for item in self._runs.values()
                if item.org_id == org_id and item.case_id == case_id
            ]
            return self._copy(
                sorted(
                    values,
                    key=lambda item: (item.created_at, item.review_run_id),
                    reverse=True,
                )
            )

    def save_run_decision(
        self,
        decision: HumanDecisionRecord,
        audit: AuditRecord,
        *,
        expected_case_revision: int,
    ) -> HumanDecisionRecord:
        with self._lock:
            run = self._runs.get(decision.review_run_id)
            case = self._cases.get(decision.case_id)
            if (
                run is None
                or run.org_id != decision.org_id
                or case is None
                or case.org_id != decision.org_id
                or run.case_revision != expected_case_revision
                or case.revision != expected_case_revision
            ):
                raise ConflictError("review decision is stale because the Case changed")
            if any(
                item.org_id == decision.org_id
                and item.review_run_id == decision.review_run_id
                and item.proposal_id is None
                for item in self._decisions.values()
            ):
                raise ConflictError("review run already has a human decision")
            if decision.target_revision_id is not None:
                target = self._runs.get(decision.target_revision_id)
                if (
                    target is None
                    or target.org_id != decision.org_id
                    or target.parent_run_id != decision.review_run_id
                    or target.case_revision != expected_case_revision
                ):
                    raise ConflictError("target revision is stale or unrelated")
            if decision.decision_id in self._decisions:
                raise ConflictError("decision_id already exists")
            self._decisions[decision.decision_id] = self._copy(decision)
            self._audits[audit.audit_id] = self._copy(audit)
            return self._copy(decision)

    def list_decisions(self, org_id: str, review_run_id: str) -> list[HumanDecisionRecord]:
        with self._lock:
            values = [
                item
                for item in self._decisions.values()
                if item.org_id == org_id and item.review_run_id == review_run_id
            ]
            return self._copy(sorted(values, key=lambda item: item.created_at))

    def get_proposal_decision(self, org_id: str, proposal_id: str) -> HumanDecisionRecord | None:
        with self._lock:
            value = next(
                (
                    item
                    for item in self._decisions.values()
                    if item.org_id == org_id and item.proposal_id == proposal_id
                ),
                None,
            )
            return self._copy(value) if value is not None else None

    def get_decision_by_idempotency_key(
        self, org_id: str, idempotency_key: str
    ) -> HumanDecisionRecord | None:
        with self._lock:
            value = next(
                (
                    item
                    for item in self._decisions.values()
                    if item.org_id == org_id and item.idempotency_key == idempotency_key
                ),
                None,
            )
            return self._copy(value) if value is not None else None

    def create_correction_proposal(
        self, proposal: CorrectionProposalRecord, audit: AuditRecord
    ) -> CorrectionProposalRecord:
        with self._lock:
            existing = next(
                (
                    item
                    for item in self._correction_proposals.values()
                    if item.org_id == proposal.org_id
                    and item.idempotency_key == proposal.idempotency_key
                ),
                None,
            )
            if existing is not None:
                if existing.idempotency_request_digest != proposal.idempotency_request_digest:
                    raise ConflictError("proposal idempotency key was reused with another request")
                return self._copy(existing)
            if proposal.proposal_id in self._correction_proposals:
                raise ConflictError("correction proposal already exists")
            self._correction_proposals[proposal.proposal_id] = self._copy(proposal)
            self._audits[audit.audit_id] = self._copy(audit)
            return self._copy(proposal)

    def get_correction_proposal(
        self, org_id: str, proposal_id: str
    ) -> CorrectionProposalRecord | None:
        with self._lock:
            record = self._correction_proposals.get(proposal_id)
            return self._copy(record) if record and record.org_id == org_id else None

    def get_correction_proposal_by_idempotency_key(
        self, org_id: str, idempotency_key: str
    ) -> CorrectionProposalRecord | None:
        with self._lock:
            record = next(
                (
                    item
                    for item in self._correction_proposals.values()
                    if item.org_id == org_id and item.idempotency_key == idempotency_key
                ),
                None,
            )
            return self._copy(record) if record is not None else None

    def list_correction_proposals(
        self, org_id: str, review_run_id: str
    ) -> list[CorrectionProposalRecord]:
        with self._lock:
            values = [
                item
                for item in self._correction_proposals.values()
                if item.org_id == org_id and item.review_run_id == review_run_id
            ]
            return self._copy(sorted(values, key=lambda item: item.created_at))

    def decide_correction_proposal(
        self,
        *,
        proposal: CorrectionProposalRecord,
        decision: HumanDecisionRecord,
        audit: AuditRecord,
        job: JobRecord | None = None,
        event: StageEvent | None = None,
        outbox: OutboxRecord | None = None,
    ) -> CorrectionProposalRecord:
        with self._lock:
            if decision.idempotency_key is None:
                raise ConflictError("proposal decision omitted its idempotency binding")
            if any(
                item.org_id == decision.org_id and item.idempotency_key == decision.idempotency_key
                for item in self._decisions.values()
            ):
                raise ConflictError("decision idempotency key is already committed")
            current = self._correction_proposals.get(proposal.proposal_id)
            if current is None or current.org_id != proposal.org_id:
                raise ConflictError("correction proposal does not exist")
            if current.status != CorrectionProposalStatus.pending:
                raise ConflictError("correction proposal was already decided")
            case = self._cases.get(current.case_id)
            run = self._runs.get(current.review_run_id)
            if (
                case is None
                or run is None
                or case.org_id != current.org_id
                or run.org_id != current.org_id
                or case.revision != current.case_revision
                or run.result_digest != current.parent_result_digest
            ):
                raise ConflictError("correction proposal decision is stale")
            if current.created_by == decision.decided_by:
                raise ConflictError("correction proposal requires an independent decision maker")
            if proposal.status == CorrectionProposalStatus.approved:
                if job is None or event is None or outbox is None:
                    raise ConflictError("approved correction proposal requires a child job")
                key = (job.org_id, job.idempotency_key)
                if key in self._idempotency:
                    raise ConflictError("idempotency key already belongs to another decision")
                active = [
                    item
                    for item in self._jobs.values()
                    if item.org_id == job.org_id
                    and item.case_id == job.case_id
                    and item.case_revision == job.case_revision
                    and item.status in _ACTIVE_JOB_STATUSES
                ]
                if any(
                    item.status
                    in {JobStatus.delivery_uncertain, JobStatus.response_persistence_uncertain}
                    for item in active
                ):
                    raise ConflictError("delivery uncertainty must be reconciled before a new call")
                if any(
                    item.logical_request_sha256 == job.logical_request_sha256 for item in active
                ):
                    raise ConflictError("logical revision request is already active")
                self._jobs[job.job_id] = self._copy(job)
                self._idempotency[key] = job.job_id
                self._events[job.job_id] = [self._copy(event)]
                self._outbox[outbox.outbox_id] = self._copy(outbox)
                self._cases[job.case_id] = case.model_copy(
                    update={"status": CaseStatus.in_review, "updated_at": job.created_at}
                )
            self._correction_proposals[proposal.proposal_id] = self._copy(proposal)
            self._decisions[decision.decision_id] = self._copy(decision)
            self._audits[audit.audit_id] = self._copy(audit)
            return self._copy(proposal)

    def reserve_chat_interaction(
        self,
        interaction: ChatInteractionRecord,
        *,
        actor_window_started_at: datetime,
        actor_window_limit: int,
        review_run_limit: int,
    ) -> tuple[ChatInteractionRecord, bool]:
        with self._lock:
            existing = next(
                (
                    item
                    for item in self._chat_interactions.values()
                    if item.org_id == interaction.org_id
                    and item.idempotency_key == interaction.idempotency_key
                ),
                None,
            )
            if existing is not None:
                if existing.idempotency_request_digest != interaction.idempotency_request_digest:
                    raise ConflictError("chat idempotency key was reused with another request")
                return self._copy(existing), False
            counted = [
                item
                for item in self._chat_interactions.values()
                if item.org_id == interaction.org_id
                and item.review_run_id == interaction.review_run_id
                and item.state in _CHAT_QUOTA_STATES
            ]
            if len(counted) >= review_run_limit:
                raise QuotaExceededError("chat quota for this review run is exhausted")
            recent_by_actor = sum(
                item.created_by == interaction.created_by
                and item.created_at >= actor_window_started_at
                for item in counted
            )
            if recent_by_actor >= actor_window_limit:
                raise QuotaExceededError("chat rate limit exceeded; retry after one minute")
            self._chat_interactions[interaction.interaction_id] = self._copy(interaction)
            return self._copy(interaction), True

    def start_chat_call_once(self, *, org_id: str, interaction_id: str) -> ChatInteractionRecord:
        with self._lock:
            interaction = self._chat_interactions.get(interaction_id)
            if (
                interaction is None
                or interaction.org_id != org_id
                or interaction.state != ChatInteractionState.reserved
            ):
                raise ConflictError("chat Agent call is already started or terminal")
            started = interaction.model_copy(
                update={
                    "state": ChatInteractionState.call_started,
                    "call_started_at": utc_now(),
                    "updated_at": utc_now(),
                }
            )
            self._chat_interactions[interaction_id] = self._copy(started)
            return self._copy(started)

    def complete_chat_interaction(
        self,
        *,
        interaction: ChatInteractionRecord,
        message: ChatRecord,
        audit: AuditRecord,
    ) -> ChatRecord:
        with self._lock:
            current = self._chat_interactions.get(interaction.interaction_id)
            if (
                current is None
                or current.org_id != interaction.org_id
                or current.state != ChatInteractionState.call_started
                or current.idempotency_request_digest != interaction.idempotency_request_digest
            ):
                raise ConflictError("chat interaction call state is stale")
            if message.message_id in self._chat:
                raise ConflictError("chat message already exists without completed interaction")
            completed = current.model_copy(
                update={
                    "state": ChatInteractionState.completed,
                    "message_id": message.message_id,
                    "error_code": None,
                    "updated_at": utc_now(),
                }
            )
            self._chat[message.message_id] = self._copy(message)
            self._chat_interactions[current.interaction_id] = self._copy(completed)
            self._audits[audit.audit_id] = self._copy(audit)
            return self._copy(message)

    def mark_chat_interaction_terminal(
        self,
        *,
        org_id: str,
        interaction_id: str,
        state: ChatInteractionState,
        error_code: str,
        audit: AuditRecord,
    ) -> ChatInteractionRecord:
        if state not in {
            ChatInteractionState.delivery_uncertain,
            ChatInteractionState.response_received_rejected,
            ChatInteractionState.response_persistence_uncertain,
        }:
            raise ValueError("chat terminal state is not an allowed failure state")
        with self._lock:
            current = self._chat_interactions.get(interaction_id)
            if (
                current is None
                or current.org_id != org_id
                or current.state != ChatInteractionState.call_started
            ):
                raise ConflictError("chat interaction call state is stale")
            terminal = current.model_copy(
                update={"state": state, "error_code": error_code, "updated_at": utc_now()}
            )
            self._chat_interactions[interaction_id] = self._copy(terminal)
            self._audits[audit.audit_id] = self._copy(audit)
            return self._copy(terminal)

    def get_chat_interaction_by_idempotency_key(
        self, org_id: str, idempotency_key: str
    ) -> ChatInteractionRecord | None:
        with self._lock:
            value = next(
                (
                    item
                    for item in self._chat_interactions.values()
                    if item.org_id == org_id and item.idempotency_key == idempotency_key
                ),
                None,
            )
            return self._copy(value) if value is not None else None

    def get_chat_by_idempotency_key(self, org_id: str, idempotency_key: str) -> ChatRecord | None:
        with self._lock:
            value = next(
                (
                    item
                    for item in self._chat.values()
                    if item.org_id == org_id and item.idempotency_key == idempotency_key
                ),
                None,
            )
            return self._copy(value) if value is not None else None

    def list_chat(self, org_id: str, review_run_id: str) -> list[ChatRecord]:
        with self._lock:
            values = [
                item
                for item in self._chat.values()
                if item.org_id == org_id and item.review_run_id == review_run_id
            ]
            return self._copy(sorted(values, key=lambda item: item.created_at))

    def get_review_presentation(
        self, org_id: str, review_run_id: str
    ) -> ReviewPresentationRecord | None:
        with self._lock:
            run = self._runs.get(review_run_id)
            value = self._review_presentations.get(review_run_id)
            return self._copy(value) if run and run.org_id == org_id and value else None

    def create_assistant_conversation(
        self, record: AssistantConversationRecord, audit: AuditRecord
    ) -> AssistantConversationRecord:
        with self._lock:
            run = self._runs.get(record.review_run_id)
            if (
                run is None
                or run.org_id != record.org_id
                or run.case_id != record.case_id
                or run.provider_result_sha256 != record.bound_result_sha256
                or run.result_digest != record.bound_effective_result_digest
            ):
                raise ConflictError("assistant conversation is not bound to the current Run")
            existing = self._assistant_conversations.get(record.conversation_id)
            if existing is not None:
                if existing != record:
                    raise ConflictError("conversation ID already binds another record")
                return self._copy(existing)
            self._assistant_conversations[record.conversation_id] = self._copy(record)
            self._audits[audit.audit_id] = self._copy(audit)
            return self._copy(record)

    def get_assistant_conversation(
        self, org_id: str, conversation_id: str
    ) -> AssistantConversationRecord | None:
        with self._lock:
            value = self._assistant_conversations.get(conversation_id)
            return self._copy(value) if value and value.org_id == org_id else None

    def list_assistant_conversations(
        self, org_id: str, review_run_id: str
    ) -> list[AssistantConversationRecord]:
        with self._lock:
            values = [
                item
                for item in self._assistant_conversations.values()
                if item.org_id == org_id and item.review_run_id == review_run_id
            ]
            return self._copy(sorted(values, key=lambda item: item.created_at))

    def enqueue_assistant_message(
        self,
        *,
        conversation: AssistantConversationRecord,
        user_message: AssistantMessageRecord,
        job: AssistantJobRecord,
        outbox: OutboxRecord,
        audit: AuditRecord,
        expected_case_revision: int,
        actor_window_started_at: datetime,
        actor_window_limit: int,
        review_run_limit: int,
    ) -> tuple[AssistantJobRecord, AssistantMessageRecord, bool]:
        with self._lock:
            current = self._assistant_conversations.get(conversation.conversation_id)
            if current != conversation:
                raise ConflictError("assistant conversation binding is stale")
            case = self._cases.get(conversation.case_id)
            run = self._runs.get(conversation.review_run_id)
            if (
                case is None
                or run is None
                or case.org_id != conversation.org_id
                or run.org_id != conversation.org_id
                or run.case_id != conversation.case_id
                or case.revision != expected_case_revision
                or run.case_revision != expected_case_revision
                or run.provider_result_sha256 != conversation.bound_result_sha256
                or run.result_digest != conversation.bound_effective_result_digest
            ):
                raise ConflictError("assistant Case or review result binding is stale")
            existing = next(
                (
                    item
                    for item in self._assistant_jobs.values()
                    if item.org_id == job.org_id and item.idempotency_key == job.idempotency_key
                ),
                None,
            )
            if existing is not None:
                if existing.idempotency_request_digest != job.idempotency_request_digest:
                    raise ConflictError("assistant idempotency key was reused")
                stored_message = self._assistant_messages.get(existing.user_message_id)
                if stored_message is None:
                    raise ConflictError("assistant job references a missing user message")
                return self._copy(existing), self._copy(stored_message), False
            counted = [
                item
                for item in self._assistant_jobs.values()
                if item.org_id == job.org_id and item.review_run_id == job.review_run_id
            ]
            if len(counted) >= review_run_limit:
                raise QuotaExceededError("assistant quota for this review run is exhausted")
            recent_by_actor = sum(
                item.created_by == job.created_by and item.created_at >= actor_window_started_at
                for item in counted
            )
            if recent_by_actor >= actor_window_limit:
                raise QuotaExceededError("assistant rate limit exceeded; retry after one minute")
            if any(
                item.conversation_id == conversation.conversation_id
                and item.state in {AssistantJobState.queued, AssistantJobState.running}
                for item in self._assistant_jobs.values()
            ):
                raise ConflictError("assistant conversation already has an active job")
            messages = sorted(
                (
                    item
                    for item in self._assistant_messages.values()
                    if item.org_id == job.org_id
                    and item.conversation_id == conversation.conversation_id
                ),
                key=lambda item: (item.created_at, item.message_id),
            )
            expected_parent = messages[-1].message_id if messages else None
            if user_message.parent_message_id != expected_parent:
                raise ConflictError("assistant message parent is stale")
            if (
                user_message.conversation_id != conversation.conversation_id
                or user_message.review_run_id != conversation.review_run_id
                or user_message.case_id != conversation.case_id
                or user_message.org_id != conversation.org_id
                or user_message.bound_result_sha256 != conversation.bound_result_sha256
                or job.user_message_id != user_message.message_id
                or job.conversation_id != conversation.conversation_id
            ):
                raise ConflictError("assistant message or job binding is invalid")
            self._assistant_messages[user_message.message_id] = self._copy(user_message)
            self._assistant_jobs[job.assistant_job_id] = self._copy(job)
            self._outbox[outbox.outbox_id] = self._copy(outbox)
            self._audits[audit.audit_id] = self._copy(audit)
            return self._copy(job), self._copy(user_message), True

    def get_assistant_job(self, org_id: str, assistant_job_id: str) -> AssistantJobRecord | None:
        with self._lock:
            value = self._assistant_jobs.get(assistant_job_id)
            return self._copy(value) if value and value.org_id == org_id else None

    def get_assistant_job_by_idempotency_key(
        self, org_id: str, idempotency_key: str
    ) -> AssistantJobRecord | None:
        with self._lock:
            value = next(
                (
                    item
                    for item in self._assistant_jobs.values()
                    if item.org_id == org_id and item.idempotency_key == idempotency_key
                ),
                None,
            )
            return self._copy(value) if value else None

    def list_assistant_jobs(self, org_id: str, conversation_id: str) -> list[AssistantJobRecord]:
        with self._lock:
            values = [
                item
                for item in self._assistant_jobs.values()
                if item.org_id == org_id and item.conversation_id == conversation_id
            ]
            return self._copy(
                sorted(values, key=lambda item: (item.created_at, item.assistant_job_id))
            )

    def claim_assistant_job(
        self,
        *,
        org_id: str,
        assistant_job_id: str,
        lease_id: str,
        lease_expires_at: datetime,
        event: AssistantStageEvent,
    ) -> AssistantJobRecord:
        with self._lock:
            value = self._assistant_jobs.get(assistant_job_id)
            if value is None or value.org_id != org_id:
                raise ConflictError("assistant job does not exist")
            if value.state != AssistantJobState.queued:
                raise ConflictError("assistant job is not runnable")
            now = utc_now()
            if lease_expires_at <= now:
                raise ConflictError("assistant job lease must expire in the future")
            _validate_assistant_event_append(value, event)
            started = _updated_assistant_job(
                value,
                state=AssistantJobState.running,
                call_started_at=now,
                lease_id=lease_id,
                lease_expires_at=lease_expires_at,
                events=[*value.events, event],
                updated_at=now,
            )
            self._assistant_jobs[assistant_job_id] = self._copy(started)
            return self._copy(started)

    def append_assistant_event(
        self,
        *,
        org_id: str,
        assistant_job_id: str,
        expected_lease_id: str,
        event: AssistantStageEvent,
    ) -> AssistantJobRecord:
        with self._lock:
            value = self._assistant_jobs.get(assistant_job_id)
            if (
                value is None
                or value.org_id != org_id
                or value.state != AssistantJobState.running
                or value.lease_id != expected_lease_id
            ):
                raise ConflictError("assistant job is not running")
            _validate_assistant_event_append(value, event)
            updated = _updated_assistant_job(
                value,
                events=[*value.events, event],
                updated_at=utc_now(),
            )
            self._assistant_jobs[assistant_job_id] = self._copy(updated)
            return self._copy(updated)

    def complete_assistant_job(
        self,
        *,
        job: AssistantJobRecord,
        answer_message: AssistantMessageRecord,
        events: tuple[AssistantStageEvent, AssistantStageEvent],
        audit: AuditRecord,
        expected_lease_id: str,
        expected_case_revision: int,
    ) -> AssistantJobRecord:
        with self._lock:
            current = self._assistant_jobs.get(job.assistant_job_id)
            if current is None or current.org_id != job.org_id:
                raise ConflictError("assistant job does not exist")
            if current.state != AssistantJobState.running or current.lease_id != expected_lease_id:
                raise ConflictError("assistant job is not running")
            case = self._cases.get(current.case_id)
            run = self._runs.get(current.review_run_id)
            conversation = self._assistant_conversations.get(current.conversation_id)
            if (
                case is None
                or run is None
                or conversation is None
                or case.revision != expected_case_revision
                or run.case_revision != expected_case_revision
                or run.provider_result_sha256 != conversation.bound_result_sha256
                or run.result_digest != conversation.bound_effective_result_digest
            ):
                raise ConflictError("assistant Case or review result changed during the call")
            if (
                answer_message.role != "assistant"
                or answer_message.conversation_id != current.conversation_id
                or answer_message.parent_message_id != current.user_message_id
                or job.answer_message_id != answer_message.message_id
                or job.state != AssistantJobState.succeeded
            ):
                raise ConflictError("assistant result is not bound to its job")
            completed_events = _assistant_completion_events(current, events)
            completed = _updated_assistant_job(
                job,
                events=completed_events,
                lease_id=None,
                lease_expires_at=None,
                updated_at=utc_now(),
            )
            self._assistant_messages[answer_message.message_id] = self._copy(answer_message)
            self._assistant_jobs[job.assistant_job_id] = self._copy(completed)
            self._audits[audit.audit_id] = self._copy(audit)
            return self._copy(completed)

    def mark_assistant_job_terminal(
        self,
        *,
        org_id: str,
        assistant_job_id: str,
        state: AssistantJobState,
        error_code: str,
        event: AssistantStageEvent,
        audit: AuditRecord,
        expected_lease_id: str,
    ) -> AssistantJobRecord:
        with self._lock:
            value = self._assistant_jobs.get(assistant_job_id)
            if value is None or value.org_id != org_id:
                raise ConflictError("assistant job does not exist")
            if (
                value.state != AssistantJobState.running
                or value.lease_id != expected_lease_id
                or state
                in {
                    AssistantJobState.queued,
                    AssistantJobState.running,
                    AssistantJobState.succeeded,
                }
            ):
                raise ConflictError("assistant job terminal transition is stale")
            _validate_assistant_event_append(value, event)
            terminal = _updated_assistant_job(
                value,
                state=state,
                error_code=error_code,
                lease_id=None,
                lease_expires_at=None,
                events=[*value.events, event],
                updated_at=utc_now(),
            )
            self._assistant_jobs[assistant_job_id] = self._copy(terminal)
            self._audits[audit.audit_id] = self._copy(audit)
            return self._copy(terminal)

    def list_assistant_messages(
        self, org_id: str, conversation_id: str
    ) -> list[AssistantMessageRecord]:
        with self._lock:
            values = [
                item
                for item in self._assistant_messages.values()
                if item.org_id == org_id and item.conversation_id == conversation_id
            ]
            return self._copy(sorted(values, key=lambda item: (item.created_at, item.message_id)))

    def list_audit(self, org_id: str, case_id: str) -> list[AuditRecord]:
        with self._lock:
            values = [
                item
                for item in self._audits.values()
                if item.org_id == org_id and item.case_id == case_id
            ]
            return self._copy(sorted(values, key=lambda item: item.created_at))

    def list_pending_outbox(self, limit: int = 100) -> list[OutboxRecord]:
        with self._lock:
            values = [item for item in self._outbox.values() if item.published_at is None]
            return self._copy(sorted(values, key=lambda item: item.created_at)[:limit])

    def claim_pending_outbox(
        self, *, lease_id: str, lease_expires_at: Any, limit: int = 100
    ) -> list[OutboxRecord]:
        with self._lock:
            now = utc_now()
            values = [
                item
                for item in self._outbox.values()
                if item.published_at is None
                and (item.lease_expires_at is None or item.lease_expires_at <= now)
            ]
            claimed: list[OutboxRecord] = []
            for item in sorted(values, key=lambda value: value.created_at)[:limit]:
                updated = item.model_copy(
                    update={
                        "lease_id": lease_id,
                        "lease_expires_at": lease_expires_at,
                        "attempt_count": item.attempt_count + 1,
                    }
                )
                self._outbox[item.outbox_id] = updated
                claimed.append(updated)
            return self._copy(claimed)

    def mark_outbox_published(self, outbox_id: str, *, lease_id: str) -> None:
        with self._lock:
            record = self._outbox.get(outbox_id)
            if record is None:
                raise ConflictError("outbox record does not exist")
            if record.lease_id != lease_id:
                raise ConflictError("outbox lease is stale")
            if record.published_at is None:
                self._outbox[outbox_id] = record.model_copy(
                    update={
                        "published_at": utc_now(),
                        "lease_id": None,
                        "lease_expires_at": None,
                    }
                )

    def release_outbox_claim(self, outbox_id: str, *, lease_id: str) -> None:
        with self._lock:
            record = self._outbox.get(outbox_id)
            if record is None or record.lease_id != lease_id:
                return
            self._outbox[outbox_id] = record.model_copy(
                update={"lease_id": None, "lease_expires_at": None}
            )


class Base(DeclarativeBase):
    pass


class _CaseRow(Base):
    __tablename__ = "product_cases"
    __table_args__ = {"schema": "operations"}
    case_id: Mapped[str] = mapped_column(String, primary_key=True)
    org_id: Mapped[str] = mapped_column(String, index=True)
    revision: Mapped[int] = mapped_column(Integer)
    record_json: Mapped[dict[str, Any]] = mapped_column(JSON)


class _DocumentRow(Base):
    __tablename__ = "product_documents"
    __table_args__ = {"schema": "operations"}
    document_id: Mapped[str] = mapped_column(String, primary_key=True)
    case_id: Mapped[str] = mapped_column(ForeignKey("operations.product_cases.case_id"), index=True)
    org_id: Mapped[str] = mapped_column(String, index=True)
    record_json: Mapped[dict[str, Any]] = mapped_column(JSON)


class _SupportingDocumentRow(Base):
    __tablename__ = "supporting_documents"
    __table_args__ = (UniqueConstraint("case_id", "document_id"), {"schema": "operations"})
    supporting_document_id: Mapped[str] = mapped_column(String, primary_key=True)
    document_id: Mapped[str] = mapped_column(String, index=True)
    case_id: Mapped[str] = mapped_column(ForeignKey("operations.product_cases.case_id"), index=True)
    org_id: Mapped[str] = mapped_column(String, index=True)
    record_json: Mapped[dict[str, Any]] = mapped_column(JSON)


class _JobRow(Base):
    __tablename__ = "product_jobs"
    __table_args__ = (UniqueConstraint("org_id", "idempotency_key"), {"schema": "operations"})
    job_id: Mapped[str] = mapped_column(String, primary_key=True)
    case_id: Mapped[str] = mapped_column(String, index=True)
    org_id: Mapped[str] = mapped_column(String, index=True)
    idempotency_key: Mapped[str] = mapped_column(String)
    request_digest: Mapped[str] = mapped_column(String)
    case_revision: Mapped[int] = mapped_column(Integer)
    logical_request_sha256: Mapped[str] = mapped_column(String)
    status: Mapped[str] = mapped_column(String)
    kind: Mapped[str] = mapped_column(String)
    lease_id: Mapped[str | None] = mapped_column(String, nullable=True)
    lease_expires_at: Mapped[Any | None] = mapped_column(DateTime(timezone=True), nullable=True)
    attempt_count: Mapped[int] = mapped_column(Integer, default=0)
    agent_call_state: Mapped[str] = mapped_column(String, default="not_started")
    agent_call_endpoint: Mapped[str | None] = mapped_column(String, nullable=True)
    agent_call_request_sha256: Mapped[str | None] = mapped_column(String, nullable=True)
    agent_call_started_at: Mapped[Any | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    record_json: Mapped[dict[str, Any]] = mapped_column(JSON)


class _RunRow(Base):
    __tablename__ = "product_review_runs"
    __table_args__ = {"schema": "operations"}
    review_run_id: Mapped[str] = mapped_column(String, primary_key=True)
    case_id: Mapped[str] = mapped_column(String, index=True)
    org_id: Mapped[str] = mapped_column(String, index=True)
    parent_run_id: Mapped[str | None] = mapped_column(String, nullable=True, index=True)
    case_revision: Mapped[int] = mapped_column(Integer)
    result_digest: Mapped[str] = mapped_column(String)
    execution_status: Mapped[str] = mapped_column(String)
    assessment: Mapped[str] = mapped_column(String)
    review_disposition: Mapped[str] = mapped_column(String)
    assurance_level: Mapped[str] = mapped_column(String)
    provider_result_json: Mapped[dict[str, Any]] = mapped_column(JSON)
    record_json: Mapped[dict[str, Any]] = mapped_column(JSON)


class _ReviewPresentationRow(Base):
    __tablename__ = "review_presentations"
    __table_args__ = (UniqueConstraint("org_id", "review_run_id"), {"schema": "operations"})
    presentation_id: Mapped[str] = mapped_column(String, primary_key=True)
    review_run_id: Mapped[str] = mapped_column(
        ForeignKey("operations.product_review_runs.review_run_id"), index=True
    )
    case_id: Mapped[str] = mapped_column(String, index=True)
    org_id: Mapped[str] = mapped_column(String, index=True)
    bound_provider_result_sha256: Mapped[str] = mapped_column(String)
    bound_effective_result_digest: Mapped[str] = mapped_column(String)
    created_at: Mapped[Any] = mapped_column(DateTime(timezone=True), index=True)
    record_json: Mapped[dict[str, Any]] = mapped_column(JSON)


class _AssistantConversationRow(Base):
    __tablename__ = "assistant_conversations"
    __table_args__ = {"schema": "operations"}
    conversation_id: Mapped[str] = mapped_column(String, primary_key=True)
    review_run_id: Mapped[str] = mapped_column(
        ForeignKey("operations.product_review_runs.review_run_id"), index=True
    )
    case_id: Mapped[str] = mapped_column(String, index=True)
    org_id: Mapped[str] = mapped_column(String, index=True)
    bound_result_sha256: Mapped[str] = mapped_column(String)
    bound_effective_result_digest: Mapped[str] = mapped_column(String)
    created_at: Mapped[Any] = mapped_column(DateTime(timezone=True), index=True)
    record_json: Mapped[dict[str, Any]] = mapped_column(JSON)


class _AssistantJobRow(Base):
    __tablename__ = "assistant_jobs"
    __table_args__ = (
        UniqueConstraint("org_id", "idempotency_key"),
        {"schema": "operations"},
    )
    assistant_job_id: Mapped[str] = mapped_column(String, primary_key=True)
    conversation_id: Mapped[str] = mapped_column(
        ForeignKey("operations.assistant_conversations.conversation_id"), index=True
    )
    review_run_id: Mapped[str] = mapped_column(String, index=True)
    case_id: Mapped[str] = mapped_column(String, index=True)
    org_id: Mapped[str] = mapped_column(String, index=True)
    idempotency_key: Mapped[str] = mapped_column(String)
    idempotency_request_digest: Mapped[str] = mapped_column(String)
    created_by: Mapped[str] = mapped_column(String, index=True)
    state: Mapped[str] = mapped_column(String, index=True)
    created_at: Mapped[Any] = mapped_column(DateTime(timezone=True), index=True)
    record_json: Mapped[dict[str, Any]] = mapped_column(JSON)


class _AssistantMessageRow(Base):
    __tablename__ = "assistant_messages"
    __table_args__ = {"schema": "operations"}
    message_id: Mapped[str] = mapped_column(String, primary_key=True)
    conversation_id: Mapped[str] = mapped_column(
        ForeignKey("operations.assistant_conversations.conversation_id"), index=True
    )
    review_run_id: Mapped[str] = mapped_column(String, index=True)
    org_id: Mapped[str] = mapped_column(String, index=True)
    role: Mapped[str] = mapped_column(String)
    created_at: Mapped[Any] = mapped_column(DateTime(timezone=True), index=True)
    record_json: Mapped[dict[str, Any]] = mapped_column(JSON)


class _DecisionRow(Base):
    __tablename__ = "human_decisions"
    __table_args__ = (
        UniqueConstraint("org_id", "decision_scope"),
        {"schema": "operations"},
    )
    decision_id: Mapped[str] = mapped_column(String, primary_key=True)
    review_run_id: Mapped[str] = mapped_column(String, index=True)
    org_id: Mapped[str] = mapped_column(String, index=True)
    decision_scope: Mapped[str] = mapped_column(String)
    idempotency_key: Mapped[str | None] = mapped_column(String, nullable=True)
    idempotency_request_digest: Mapped[str | None] = mapped_column(String, nullable=True)
    decision: Mapped[str] = mapped_column(String)
    record_json: Mapped[dict[str, Any]] = mapped_column(JSON)


class _CorrectionProposalRow(Base):
    __tablename__ = "correction_proposals"
    __table_args__ = (
        UniqueConstraint("org_id", "idempotency_key"),
        {"schema": "operations"},
    )
    proposal_id: Mapped[str] = mapped_column(String, primary_key=True)
    review_run_id: Mapped[str] = mapped_column(String, index=True)
    case_id: Mapped[str] = mapped_column(String, index=True)
    org_id: Mapped[str] = mapped_column(String, index=True)
    idempotency_key: Mapped[str] = mapped_column(String)
    idempotency_request_digest: Mapped[str] = mapped_column(String)
    status: Mapped[str] = mapped_column(String)
    record_json: Mapped[dict[str, Any]] = mapped_column(JSON)


class _ChatRow(Base):
    __tablename__ = "case_chat_messages"
    __table_args__ = {"schema": "operations"}
    message_id: Mapped[str] = mapped_column(String, primary_key=True)
    review_run_id: Mapped[str] = mapped_column(String, index=True)
    org_id: Mapped[str] = mapped_column(String, index=True)
    bound_result_digest: Mapped[str] = mapped_column(String)
    idempotency_key: Mapped[str | None] = mapped_column(String, nullable=True)
    idempotency_request_digest: Mapped[str | None] = mapped_column(String, nullable=True)
    record_json: Mapped[dict[str, Any]] = mapped_column(JSON)


class _ChatInteractionRow(Base):
    __tablename__ = "case_chat_interactions"
    __table_args__ = (
        UniqueConstraint("org_id", "idempotency_key"),
        {"schema": "operations"},
    )
    interaction_id: Mapped[str] = mapped_column(String, primary_key=True)
    review_run_id: Mapped[str] = mapped_column(String, index=True)
    case_id: Mapped[str] = mapped_column(String, index=True)
    org_id: Mapped[str] = mapped_column(String, index=True)
    idempotency_key: Mapped[str] = mapped_column(String)
    idempotency_request_digest: Mapped[str] = mapped_column(String)
    agent_request_sha256: Mapped[str] = mapped_column(String)
    state: Mapped[str] = mapped_column(String)
    message_id: Mapped[str | None] = mapped_column(String, nullable=True)
    created_by: Mapped[str] = mapped_column(String, index=True)
    created_at: Mapped[Any] = mapped_column(DateTime(timezone=True), index=True)
    record_json: Mapped[dict[str, Any]] = mapped_column(JSON)


class _AuditRow(Base):
    __tablename__ = "product_audit_log"
    __table_args__ = {"schema": "operations"}
    audit_id: Mapped[str] = mapped_column(String, primary_key=True)
    org_id: Mapped[str] = mapped_column(String, index=True)
    case_id: Mapped[str | None] = mapped_column(String, nullable=True, index=True)
    created_at: Mapped[Any] = mapped_column(DateTime(timezone=True), index=True)
    record_json: Mapped[dict[str, Any]] = mapped_column(JSON)


class _OutboxRow(Base):
    __tablename__ = "product_outbox"
    __table_args__ = {"schema": "operations"}
    outbox_id: Mapped[str] = mapped_column(String, primary_key=True)
    org_id: Mapped[str] = mapped_column(String, index=True)
    aggregate_id: Mapped[str] = mapped_column(String, index=True)
    topic: Mapped[str] = mapped_column(String)
    published_at: Mapped[Any | None] = mapped_column(DateTime(timezone=True), nullable=True)
    lease_id: Mapped[str | None] = mapped_column(String, nullable=True)
    lease_expires_at: Mapped[Any | None] = mapped_column(DateTime(timezone=True), nullable=True)
    attempt_count: Mapped[int] = mapped_column(Integer, default=0)
    record_json: Mapped[dict[str, Any]] = mapped_column(JSON)


class _StageEventRow(Base):
    __tablename__ = "review_stage_events"
    __table_args__ = (UniqueConstraint("job_id", "sequence"), {"schema": "operations"})
    event_id: Mapped[str] = mapped_column(String, primary_key=True)
    job_id: Mapped[str] = mapped_column(String, index=True)
    org_id: Mapped[str] = mapped_column(String, index=True)
    sequence: Mapped[int] = mapped_column(Integer)
    record_json: Mapped[dict[str, Any]] = mapped_column(JSON)


class PostgresWorkbenchRepository:
    """SQLAlchemy 2 repository. Alembic owns schema creation in production."""

    def __init__(self, database_url: str) -> None:
        self._engine = create_engine(database_url, pool_pre_ping=True)

    def health(self) -> bool:
        with self._engine.connect() as connection:
            return connection.scalar(text("SELECT 1")) == 1

    @staticmethod
    def _dump(record: Any) -> dict[str, Any]:
        return record.model_dump(mode="json")

    @staticmethod
    def _audit(session: Session, record: AuditRecord) -> None:
        session.add(
            _AuditRow(
                audit_id=record.audit_id,
                org_id=record.org_id,
                case_id=record.case_id,
                created_at=record.created_at,
                record_json=PostgresWorkbenchRepository._dump(record),
            )
        )

    def create_case(self, record: CaseRecord, audit: AuditRecord) -> CaseRecord:
        with Session(self._engine) as session, session.begin():
            session.add(
                _CaseRow(
                    case_id=record.case_id,
                    org_id=record.org_id,
                    revision=record.revision,
                    record_json=self._dump(record),
                )
            )
            self._audit(session, audit)
        return record

    def get_case(self, org_id: str, case_id: str) -> CaseRecord | None:
        with Session(self._engine) as session:
            row = session.scalar(
                select(_CaseRow).where(_CaseRow.case_id == case_id, _CaseRow.org_id == org_id)
            )
            return CaseRecord.model_validate(row.record_json) if row else None

    def list_cases(self, org_id: str) -> list[CaseRecord]:
        with Session(self._engine) as session:
            rows = session.scalars(select(_CaseRow).where(_CaseRow.org_id == org_id)).all()
            return [CaseRecord.model_validate(row.record_json) for row in rows]

    def add_document(
        self,
        record: DocumentRecord,
        *,
        expected_revision: int,
        new_manifest_digest: str,
        audit: AuditRecord,
    ) -> CaseRecord:
        with Session(self._engine) as session, session.begin():
            row = session.scalar(
                select(_CaseRow)
                .where(_CaseRow.case_id == record.case_id, _CaseRow.org_id == record.org_id)
                .with_for_update()
            )
            if row is None or row.revision != expected_revision:
                raise ConflictError("case revision is stale")
            existing_document_rows = session.scalars(
                select(_DocumentRow).where(
                    _DocumentRow.org_id == record.org_id,
                    _DocumentRow.case_id == record.case_id,
                )
            ).all()
            existing_documents = [
                DocumentRecord.model_validate(item.record_json) for item in existing_document_rows
            ]
            if record.document_type == "SUPPORTING":
                supporting_source_count = sum(
                    1 for item in existing_documents if item.document_type == "SUPPORTING"
                )
                if supporting_source_count >= MAX_SUPPORTING_DOCUMENTS_PER_CASE:
                    raise ConflictError("Case supporting-document source limit of 100 is exhausted")
            case = CaseRecord.model_validate(row.record_json)
            updated = case.model_copy(
                update={
                    "revision": case.revision + 1,
                    "document_manifest_digest": new_manifest_digest,
                    "updated_at": record.created_at,
                }
            )
            row.revision = updated.revision
            row.record_json = self._dump(updated)
            session.add(
                _DocumentRow(
                    document_id=record.document_id,
                    case_id=record.case_id,
                    org_id=record.org_id,
                    record_json=self._dump(record),
                )
            )
            self._audit(session, audit)
        return updated

    def list_documents(self, org_id: str, case_id: str) -> list[DocumentRecord]:
        with Session(self._engine) as session:
            rows = session.scalars(
                select(_DocumentRow).where(
                    _DocumentRow.org_id == org_id, _DocumentRow.case_id == case_id
                )
            ).all()
            return [DocumentRecord.model_validate(row.record_json) for row in rows]

    def save_supporting_document(
        self,
        record: SupportingDocumentRecord,
        audit: AuditRecord,
        *,
        expected_revision: int,
    ) -> SupportingDocumentRecord:
        with Session(self._engine) as session, session.begin():
            case_row = session.scalar(
                select(_CaseRow)
                .where(_CaseRow.org_id == record.org_id, _CaseRow.case_id == record.case_id)
                .with_for_update()
            )
            if case_row is None or case_row.revision != expected_revision:
                raise ConflictError("case revision is stale")
            source = session.scalar(
                select(_DocumentRow).where(
                    _DocumentRow.org_id == record.org_id,
                    _DocumentRow.case_id == record.case_id,
                    _DocumentRow.document_id == record.document_id,
                )
            )
            if source is None:
                raise ConflictError("supporting document source does not exist")
            source_record = DocumentRecord.model_validate(source.record_json)
            if (
                source_record.document_type != "SUPPORTING"
                or source_record.content_sha256 != record.source_sha256
            ):
                raise ConflictError("supporting document source hash is stale")
            supporting_count = session.scalar(
                select(func.count())
                .select_from(_SupportingDocumentRow)
                .where(
                    _SupportingDocumentRow.org_id == record.org_id,
                    _SupportingDocumentRow.case_id == record.case_id,
                )
            )
            if int(supporting_count or 0) >= MAX_SUPPORTING_DOCUMENTS_PER_CASE:
                raise ConflictError("Case supporting-document limit of 100 is exhausted")
            session.add(
                _SupportingDocumentRow(
                    supporting_document_id=f"{record.case_id}:{record.document_id}",
                    document_id=record.document_id,
                    case_id=record.case_id,
                    org_id=record.org_id,
                    record_json=self._dump(record),
                )
            )
            case = CaseRecord.model_validate(case_row.record_json)
            updated_case = case.model_copy(
                update={"revision": case.revision + 1, "updated_at": record.created_at}
            )
            case_row.revision = updated_case.revision
            case_row.record_json = self._dump(updated_case)
            self._audit(session, audit)
        return record

    def get_supporting_document(
        self, org_id: str, case_id: str, document_id: str
    ) -> SupportingDocumentRecord | None:
        with Session(self._engine) as session:
            row = session.scalar(
                select(_SupportingDocumentRow).where(
                    _SupportingDocumentRow.org_id == org_id,
                    _SupportingDocumentRow.case_id == case_id,
                    _SupportingDocumentRow.document_id == document_id,
                )
            )
            return SupportingDocumentRecord.model_validate(row.record_json) if row else None

    def list_supporting_documents(
        self, org_id: str, case_id: str
    ) -> list[SupportingDocumentRecord]:
        with Session(self._engine) as session:
            rows = session.scalars(
                select(_SupportingDocumentRow).where(
                    _SupportingDocumentRow.org_id == org_id,
                    _SupportingDocumentRow.case_id == case_id,
                )
            ).all()
            return [SupportingDocumentRecord.model_validate(row.record_json) for row in rows]

    def add_verification_assertion(
        self,
        *,
        org_id: str,
        case_id: str,
        document_id: str,
        assertion: VerificationAssertionRecord,
        audit: AuditRecord,
        expected_revision: int,
    ) -> SupportingDocumentRecord:
        with Session(self._engine) as session, session.begin():
            case_row = session.scalar(
                select(_CaseRow)
                .where(_CaseRow.org_id == org_id, _CaseRow.case_id == case_id)
                .with_for_update()
            )
            if case_row is None or case_row.revision != expected_revision:
                raise ConflictError("case revision is stale")
            row = session.scalar(
                select(_SupportingDocumentRow)
                .where(
                    _SupportingDocumentRow.org_id == org_id,
                    _SupportingDocumentRow.case_id == case_id,
                    _SupportingDocumentRow.document_id == document_id,
                )
                .with_for_update()
            )
            if row is None:
                raise ConflictError("supporting document does not exist")
            record = SupportingDocumentRecord.model_validate(row.record_json)
            source_row = session.scalar(
                select(_DocumentRow)
                .where(
                    _DocumentRow.org_id == org_id,
                    _DocumentRow.case_id == case_id,
                    _DocumentRow.document_id == document_id,
                )
                .with_for_update()
            )
            if source_row is None:
                raise ConflictError("supporting source document does not exist")
            source = DocumentRecord.model_validate(source_row.record_json)
            if assertion.method == "human_approver" and assertion.asserted_by in {
                source.created_by,
                record.created_by,
            }:
                raise ConflictError("supporting verification requires an independent approver")
            if any(
                item.assertion_id == assertion.assertion_id
                for item in record.verification_assertions
            ):
                return record
            updated = record.model_copy(
                update={"verification_assertions": [*record.verification_assertions, assertion]}
            )
            row.record_json = self._dump(updated)
            case = CaseRecord.model_validate(case_row.record_json)
            updated_case = case.model_copy(
                update={"revision": case.revision + 1, "updated_at": assertion.asserted_at}
            )
            case_row.revision = updated_case.revision
            case_row.record_json = self._dump(updated_case)
            self._audit(session, audit)
        return updated

    def enqueue_job(
        self,
        job: JobRecord,
        event: StageEvent,
        outbox: OutboxRecord,
        audit: AuditRecord,
    ) -> tuple[JobRecord, bool]:
        with Session(self._engine) as session, session.begin():
            # Serialize the check/insert for one organization-scoped idempotency
            # key.  The unique constraint remains the final integrity guard.
            session.execute(
                text("SELECT pg_advisory_xact_lock(hashtextextended(:key, 0))"),
                {"key": f"{job.org_id}:{job.idempotency_key}"},
            )
            existing = session.scalar(
                select(_JobRow).where(
                    _JobRow.org_id == job.org_id,
                    _JobRow.idempotency_key == job.idempotency_key,
                )
            )
            if existing:
                if existing.request_digest != job.request_digest:
                    raise ConflictError("idempotency key was reused with a different request")
                return JobRecord.model_validate(existing.record_json), False
            case_row = session.scalar(
                select(_CaseRow)
                .where(_CaseRow.org_id == job.org_id, _CaseRow.case_id == job.case_id)
                .with_for_update()
            )
            if case_row is None:
                raise ConflictError("case does not exist")
            session.execute(
                text("SELECT pg_advisory_xact_lock(hashtextextended(:key, 0))"),
                {
                    "key": (
                        f"{job.org_id}:{job.case_id}:{job.case_revision}:"
                        f"{job.logical_request_sha256}"
                    )
                },
            )
            active_rows = session.scalars(
                select(_JobRow).where(
                    _JobRow.org_id == job.org_id,
                    _JobRow.case_id == job.case_id,
                    _JobRow.case_revision == job.case_revision,
                    _JobRow.status.in_(_ACTIVE_JOB_STATUS_VALUES),
                )
            ).all()
            matching = next(
                (
                    row
                    for row in active_rows
                    if row.logical_request_sha256 == job.logical_request_sha256
                ),
                None,
            )
            if matching is not None:
                record = JobRecord.model_validate(matching.record_json)
                if record.created_by != job.created_by:
                    raise ConflictError("an active logical request belongs to another actor")
                return record, False
            if any(
                row.status in {"delivery_uncertain", "response_persistence_uncertain"}
                for row in active_rows
            ):
                raise ConflictError("delivery uncertainty must be reconciled before a new call")
            case = CaseRecord.model_validate(case_row.record_json)
            if job.kind == "review" and case.status == CaseStatus.closed:
                raise ConflictError("a new review cannot target a closed case")
            session.add(
                _JobRow(
                    job_id=job.job_id,
                    case_id=job.case_id,
                    org_id=job.org_id,
                    idempotency_key=job.idempotency_key,
                    request_digest=job.request_digest,
                    case_revision=job.case_revision,
                    logical_request_sha256=job.logical_request_sha256,
                    status=job.status.value,
                    kind=job.kind,
                    lease_id=job.lease_id,
                    lease_expires_at=job.lease_expires_at,
                    attempt_count=job.attempt_count,
                    agent_call_state=job.agent_call_state.value,
                    agent_call_endpoint=job.agent_call_endpoint,
                    agent_call_request_sha256=job.agent_call_request_sha256,
                    agent_call_started_at=job.agent_call_started_at,
                    record_json=self._dump(job),
                )
            )
            session.add(
                _StageEventRow(
                    event_id=event.event_id,
                    job_id=event.job_id,
                    org_id=job.org_id,
                    sequence=event.sequence,
                    record_json=self._dump(event),
                )
            )
            session.add(
                _OutboxRow(
                    outbox_id=outbox.outbox_id,
                    org_id=outbox.org_id,
                    aggregate_id=outbox.aggregate_id,
                    topic=outbox.topic,
                    published_at=outbox.published_at,
                    lease_id=outbox.lease_id,
                    lease_expires_at=outbox.lease_expires_at,
                    attempt_count=outbox.attempt_count,
                    record_json=self._dump(outbox),
                )
            )
            self._audit(session, audit)
            updated_case = case.model_copy(
                update={"status": CaseStatus.in_review, "updated_at": job.created_at}
            )
            case_row.record_json = self._dump(updated_case)
        return job, True

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
    ) -> tuple[JobRecord, bool]:
        with Session(self._engine) as session, session.begin():
            session.execute(
                text("SELECT pg_advisory_xact_lock(hashtextextended(:key, 0))"),
                {"key": f"{org_id}:{idempotency_key}"},
            )
            existing = session.scalar(
                select(_JobRow).where(
                    _JobRow.org_id == org_id,
                    _JobRow.idempotency_key == idempotency_key,
                )
            )
            if existing is not None:
                record = JobRecord.model_validate(existing.record_json)
                if record.idempotency_request_digest != idempotency_request_digest:
                    raise ConflictError("idempotency key was reused with a different request")
                return record, False
            case_row = session.scalar(
                select(_CaseRow)
                .where(_CaseRow.org_id == org_id, _CaseRow.case_id == case_id)
                .with_for_update()
            )
            if case_row is None:
                raise ConflictError("case does not exist")
            document_rows = session.scalars(
                select(_DocumentRow)
                .where(_DocumentRow.org_id == org_id, _DocumentRow.case_id == case_id)
                .order_by(_DocumentRow.document_id)
            ).all()
            supporting_rows = session.scalars(
                select(_SupportingDocumentRow)
                .where(
                    _SupportingDocumentRow.org_id == org_id,
                    _SupportingDocumentRow.case_id == case_id,
                )
                .order_by(_SupportingDocumentRow.document_id)
            ).all()
            documents = sorted(
                (DocumentRecord.model_validate(row.record_json) for row in document_rows),
                key=lambda item: (item.created_at, item.document_id),
            )
            supporting = sorted(
                (
                    SupportingDocumentRecord.model_validate(row.record_json)
                    for row in supporting_rows
                ),
                key=lambda item: (item.created_at, item.document_id),
            )
            job, event, outbox, audit = build(
                CaseRecord.model_validate(case_row.record_json),
                documents,
                supporting,
            )
            if job.idempotency_request_digest != idempotency_request_digest:
                raise ConflictError("snapshot builder changed the idempotency request digest")
            session.execute(
                text("SELECT pg_advisory_xact_lock(hashtextextended(:key, 0))"),
                {
                    "key": (
                        f"{job.org_id}:{job.case_id}:{job.case_revision}:"
                        f"{job.logical_request_sha256}"
                    )
                },
            )
            active_rows = session.scalars(
                select(_JobRow).where(
                    _JobRow.org_id == job.org_id,
                    _JobRow.case_id == job.case_id,
                    _JobRow.case_revision == job.case_revision,
                    _JobRow.status.in_(_ACTIVE_JOB_STATUS_VALUES),
                )
            ).all()
            matching = next(
                (
                    row
                    for row in active_rows
                    if row.logical_request_sha256 == job.logical_request_sha256
                ),
                None,
            )
            if matching is not None:
                record = JobRecord.model_validate(matching.record_json)
                if record.created_by != job.created_by:
                    raise ConflictError("an active logical request belongs to another actor")
                return record, False
            if any(
                row.status in {"delivery_uncertain", "response_persistence_uncertain"}
                for row in active_rows
            ):
                raise ConflictError("delivery uncertainty must be reconciled before a new call")
            current_case = CaseRecord.model_validate(case_row.record_json)
            if current_case.status == CaseStatus.closed:
                raise ConflictError("a new review cannot target a closed case")
            session.add(
                _JobRow(
                    job_id=job.job_id,
                    case_id=job.case_id,
                    org_id=job.org_id,
                    idempotency_key=job.idempotency_key,
                    request_digest=job.request_digest,
                    case_revision=job.case_revision,
                    logical_request_sha256=job.logical_request_sha256,
                    status=job.status.value,
                    kind=job.kind,
                    lease_id=job.lease_id,
                    lease_expires_at=job.lease_expires_at,
                    attempt_count=job.attempt_count,
                    agent_call_state=job.agent_call_state.value,
                    agent_call_endpoint=job.agent_call_endpoint,
                    agent_call_request_sha256=job.agent_call_request_sha256,
                    agent_call_started_at=job.agent_call_started_at,
                    record_json=self._dump(job),
                )
            )
            session.add(
                _StageEventRow(
                    event_id=event.event_id,
                    job_id=event.job_id,
                    org_id=job.org_id,
                    sequence=event.sequence,
                    record_json=self._dump(event),
                )
            )
            session.add(
                _OutboxRow(
                    outbox_id=outbox.outbox_id,
                    org_id=outbox.org_id,
                    aggregate_id=outbox.aggregate_id,
                    topic=outbox.topic,
                    published_at=outbox.published_at,
                    lease_id=outbox.lease_id,
                    lease_expires_at=outbox.lease_expires_at,
                    attempt_count=outbox.attempt_count,
                    record_json=self._dump(outbox),
                )
            )
            self._audit(session, audit)
            updated_case = current_case.model_copy(
                update={"status": CaseStatus.in_review, "updated_at": job.created_at}
            )
            case_row.record_json = self._dump(updated_case)
        return job, True

    def get_job(self, org_id: str, job_id: str) -> JobRecord | None:
        with Session(self._engine) as session:
            row = session.scalar(
                select(_JobRow).where(_JobRow.org_id == org_id, _JobRow.job_id == job_id)
            )
            return JobRecord.model_validate(row.record_json) if row else None

    def get_job_by_idempotency_key(self, org_id: str, idempotency_key: str) -> JobRecord | None:
        with Session(self._engine) as session:
            row = session.scalar(
                select(_JobRow).where(
                    _JobRow.org_id == org_id,
                    _JobRow.idempotency_key == idempotency_key,
                )
            )
            return JobRecord.model_validate(row.record_json) if row else None

    def claim_job(
        self,
        *,
        org_id: str,
        job_id: str,
        lease_id: str,
        lease_expires_at: Any,
        event: StageEvent,
        audit: AuditRecord,
    ) -> JobRecord | None:
        with Session(self._engine) as session, session.begin():
            row = session.scalar(
                select(_JobRow)
                .where(_JobRow.org_id == org_id, _JobRow.job_id == job_id)
                .with_for_update()
            )
            if row is None:
                return None
            record = JobRecord.model_validate(row.record_json)
            active_lease = (
                record.status.value == "running"
                and record.lease_id is not None
                and record.lease_expires_at is not None
                and record.lease_expires_at > utc_now()
            )
            if (
                record.status.value not in {"queued", "running"}
                or record.agent_call_state != AgentCallState.not_started
                or active_lease
            ):
                return None
            claimed = record.model_copy(
                update={
                    "status": JobStatus.running,
                    "error_code": None,
                    "lease_id": lease_id,
                    "lease_expires_at": lease_expires_at,
                    "attempt_count": record.attempt_count + 1,
                    "updated_at": utc_now(),
                }
            )
            row.status = "running"
            row.lease_id = lease_id
            row.lease_expires_at = lease_expires_at
            row.attempt_count = claimed.attempt_count
            row.record_json = self._dump(claimed)
            session.add(
                _StageEventRow(
                    event_id=event.event_id,
                    job_id=event.job_id,
                    org_id=org_id,
                    sequence=event.sequence,
                    record_json=self._dump(event),
                )
            )
            self._audit(session, audit)
        return claimed

    def start_agent_call_once(
        self,
        *,
        org_id: str,
        job_id: str,
        expected_lease_id: str,
        endpoint: str,
        request_sha256: str,
        audit: AuditRecord,
    ) -> JobRecord:
        with Session(self._engine) as session, session.begin():
            row = session.scalar(
                select(_JobRow)
                .where(_JobRow.org_id == org_id, _JobRow.job_id == job_id)
                .with_for_update()
            )
            if row is None:
                raise ConflictError("job does not exist")
            record = JobRecord.model_validate(row.record_json)
            if (
                record.status != JobStatus.running
                or record.lease_id != expected_lease_id
                or record.agent_call_state != AgentCallState.not_started
            ):
                raise ConflictError("Agent call-once barrier is already closed")
            now = utc_now()
            started = record.model_copy(
                update={
                    "agent_call_state": AgentCallState.started,
                    "agent_call_endpoint": endpoint,
                    "agent_call_request_sha256": request_sha256,
                    "agent_call_started_at": now,
                    "updated_at": now,
                }
            )
            row.agent_call_state = started.agent_call_state.value
            row.agent_call_endpoint = endpoint
            row.agent_call_request_sha256 = request_sha256
            row.agent_call_started_at = now
            row.record_json = self._dump(started)
            self._audit(session, audit)
        return started

    def mark_agent_delivery_uncertain(
        self,
        *,
        org_id: str,
        job_id: str,
        expected_lease_id: str | None,
        error_code: str,
        event: StageEvent,
        audit: AuditRecord,
    ) -> JobRecord:
        with Session(self._engine) as session, session.begin():
            row = session.scalar(
                select(_JobRow)
                .where(_JobRow.org_id == org_id, _JobRow.job_id == job_id)
                .with_for_update()
            )
            if row is None:
                raise ConflictError("job does not exist")
            record = JobRecord.model_validate(row.record_json)
            if expected_lease_id is not None and record.lease_id != expected_lease_id:
                raise ConflictError("job lease is stale")
            if record.agent_call_state == AgentCallState.not_started:
                raise ConflictError("Agent delivery was never attempted")
            if record.status == JobStatus.succeeded:
                return record
            uncertain = record.model_copy(
                update={
                    "status": JobStatus.delivery_uncertain,
                    "agent_call_state": AgentCallState.delivery_uncertain,
                    "error_code": error_code,
                    "lease_id": None,
                    "lease_expires_at": None,
                    "updated_at": utc_now(),
                }
            )
            row.status = uncertain.status.value
            row.lease_id = None
            row.lease_expires_at = None
            row.agent_call_state = uncertain.agent_call_state.value
            row.record_json = self._dump(uncertain)
            session.add(
                _StageEventRow(
                    event_id=event.event_id,
                    job_id=event.job_id,
                    org_id=org_id,
                    sequence=event.sequence,
                    record_json=self._dump(event),
                )
            )
            self._audit(session, audit)
        return uncertain

    def mark_agent_response_rejected(
        self,
        *,
        org_id: str,
        job_id: str,
        expected_lease_id: str,
        error_code: str,
        event: StageEvent,
        audit: AuditRecord,
    ) -> JobRecord:
        with Session(self._engine) as session, session.begin():
            row = session.scalar(
                select(_JobRow)
                .where(_JobRow.org_id == org_id, _JobRow.job_id == job_id)
                .with_for_update()
            )
            if row is None:
                raise ConflictError("job does not exist")
            record = JobRecord.model_validate(row.record_json)
            if (
                record.status != JobStatus.running
                or record.lease_id != expected_lease_id
                or record.agent_call_state != AgentCallState.started
            ):
                raise ConflictError("job lease is stale")
            rejected = record.model_copy(
                update={
                    "status": JobStatus.response_received_rejected,
                    "agent_call_state": AgentCallState.response_received_rejected,
                    "error_code": error_code,
                    "lease_id": None,
                    "lease_expires_at": None,
                    "updated_at": utc_now(),
                }
            )
            row.status = rejected.status.value
            row.lease_id = None
            row.lease_expires_at = None
            row.agent_call_state = rejected.agent_call_state.value
            row.record_json = self._dump(rejected)
            session.add(
                _StageEventRow(
                    event_id=event.event_id,
                    job_id=event.job_id,
                    org_id=org_id,
                    sequence=event.sequence,
                    record_json=self._dump(event),
                )
            )
            self._audit(session, audit)
        return rejected

    def mark_agent_pre_execution_retryable(
        self,
        *,
        org_id: str,
        job_id: str,
        expected_lease_id: str,
        expected_endpoint: str,
        expected_request_sha256: str,
        error_code: str,
        terminal: bool,
        event: StageEvent,
        audit: AuditRecord,
    ) -> JobRecord:
        with Session(self._engine) as session, session.begin():
            row = session.scalar(
                select(_JobRow)
                .where(_JobRow.org_id == org_id, _JobRow.job_id == job_id)
                .with_for_update()
            )
            if row is None:
                raise ConflictError("job does not exist")
            record = JobRecord.model_validate(row.record_json)
            if (
                record.status != JobStatus.running
                or record.agent_call_state != AgentCallState.started
                or record.lease_id != expected_lease_id
                or record.agent_call_endpoint != expected_endpoint
                or record.agent_call_request_sha256 != expected_request_sha256
            ):
                raise ConflictError("Agent pre-execution retry binding is stale")
            retry_state = record.model_copy(
                update={
                    "status": JobStatus.failed if terminal else JobStatus.queued,
                    "agent_call_state": AgentCallState.not_started,
                    "agent_call_endpoint": None,
                    "agent_call_request_sha256": None,
                    "agent_call_started_at": None,
                    "error_code": error_code,
                    "lease_id": None,
                    "lease_expires_at": None,
                    "updated_at": utc_now(),
                }
            )
            row.status = retry_state.status.value
            row.lease_id = None
            row.lease_expires_at = None
            row.agent_call_state = retry_state.agent_call_state.value
            row.agent_call_endpoint = None
            row.agent_call_request_sha256 = None
            row.agent_call_started_at = None
            row.attempt_count = retry_state.attempt_count
            row.record_json = self._dump(retry_state)
            session.add(
                _StageEventRow(
                    event_id=event.event_id,
                    job_id=event.job_id,
                    org_id=org_id,
                    sequence=event.sequence,
                    record_json=self._dump(event),
                )
            )
            self._audit(session, audit)
        return retry_state

    def mark_agent_response_persistence_uncertain(
        self,
        *,
        org_id: str,
        job_id: str,
        expected_lease_id: str,
        error_code: str,
        event: StageEvent,
        audit: AuditRecord,
    ) -> JobRecord:
        with Session(self._engine) as session, session.begin():
            row = session.scalar(
                select(_JobRow)
                .where(_JobRow.org_id == org_id, _JobRow.job_id == job_id)
                .with_for_update()
            )
            if row is None:
                raise ConflictError("job does not exist")
            record = JobRecord.model_validate(row.record_json)
            if (
                record.status != JobStatus.running
                or record.lease_id != expected_lease_id
                or record.agent_call_state != AgentCallState.started
            ):
                raise ConflictError("job lease is stale")
            uncertain = record.model_copy(
                update={
                    "status": JobStatus.response_persistence_uncertain,
                    "agent_call_state": AgentCallState.response_persistence_uncertain,
                    "error_code": error_code,
                    "lease_id": None,
                    "lease_expires_at": None,
                    "updated_at": utc_now(),
                }
            )
            row.status = uncertain.status.value
            row.lease_id = None
            row.lease_expires_at = None
            row.agent_call_state = uncertain.agent_call_state.value
            row.record_json = self._dump(uncertain)
            session.add(
                _StageEventRow(
                    event_id=event.event_id,
                    job_id=event.job_id,
                    org_id=org_id,
                    sequence=event.sequence,
                    record_json=self._dump(event),
                )
            )
            self._audit(session, audit)
        return uncertain

    def reconcile_delivery_uncertain(
        self,
        *,
        org_id: str,
        job_id: str,
        reconciled_by: str,
        evidence_reference_sha256: str,
        audit: AuditRecord,
    ) -> JobRecord:
        with Session(self._engine) as session, session.begin():
            row = session.scalar(
                select(_JobRow)
                .where(_JobRow.org_id == org_id, _JobRow.job_id == job_id)
                .with_for_update()
            )
            if row is None:
                raise ConflictError("job does not exist")
            record = JobRecord.model_validate(row.record_json)
            if (
                record.status != JobStatus.delivery_uncertain
                or record.agent_call_state != AgentCallState.delivery_uncertain
            ):
                raise ConflictError("only a delivery-uncertain Job can be reconciled")
            case_row = session.scalar(
                select(_CaseRow)
                .where(_CaseRow.org_id == org_id, _CaseRow.case_id == record.case_id)
                .with_for_update()
            )
            if case_row is None:
                raise ConflictError("job Case does not exist")
            now = utc_now()
            reconciled = record.model_copy(
                update={
                    "status": JobStatus.failed,
                    "agent_call_state": AgentCallState.delivery_reconciled,
                    "error_code": "delivery_confirmed_not_processed",
                    "delivery_reconciled_by": reconciled_by,
                    "delivery_reconciled_at": now,
                    "delivery_reconciliation_evidence_sha256": evidence_reference_sha256,
                    "updated_at": now,
                }
            )
            row.status = reconciled.status.value
            row.agent_call_state = reconciled.agent_call_state.value
            row.record_json = self._dump(reconciled)
            active_other = session.scalar(
                select(_JobRow.job_id).where(
                    _JobRow.org_id == org_id,
                    _JobRow.case_id == record.case_id,
                    _JobRow.case_revision == record.case_revision,
                    _JobRow.job_id != job_id,
                    _JobRow.status.in_(_ACTIVE_JOB_STATUS_VALUES),
                )
            )
            if active_other is None:
                case = CaseRecord.model_validate(case_row.record_json)
                reopened = case.model_copy(update={"status": CaseStatus.open, "updated_at": now})
                case_row.record_json = self._dump(reopened)
            self._audit(session, audit)
        return reconciled

    def update_job(
        self,
        job: JobRecord,
        event: StageEvent,
        audit: AuditRecord,
        *,
        expected_lease_id: str | None = None,
    ) -> JobRecord:
        with Session(self._engine) as session, session.begin():
            row = session.scalar(
                select(_JobRow)
                .where(_JobRow.org_id == job.org_id, _JobRow.job_id == job.job_id)
                .with_for_update()
            )
            if row is None:
                raise ConflictError("job does not exist")
            if expected_lease_id is not None and row.lease_id != expected_lease_id:
                raise ConflictError("job lease is stale")
            existing = JobRecord.model_validate(row.record_json)
            if existing.agent_call_state != job.agent_call_state:
                raise ConflictError("generic Job update cannot change Agent call-once state")
            row.record_json = self._dump(job)
            row.status = job.status.value
            row.lease_id = job.lease_id
            row.lease_expires_at = job.lease_expires_at
            row.attempt_count = job.attempt_count
            row.agent_call_state = job.agent_call_state.value
            row.agent_call_endpoint = job.agent_call_endpoint
            row.agent_call_request_sha256 = job.agent_call_request_sha256
            row.agent_call_started_at = job.agent_call_started_at
            session.add(
                _StageEventRow(
                    event_id=event.event_id,
                    job_id=event.job_id,
                    org_id=job.org_id,
                    sequence=event.sequence,
                    record_json=self._dump(event),
                )
            )
            self._audit(session, audit)
        return job

    def list_stage_events(self, org_id: str, job_id: str) -> list[StageEvent]:
        with Session(self._engine) as session:
            rows = session.scalars(
                select(_StageEventRow)
                .where(_StageEventRow.org_id == org_id, _StageEventRow.job_id == job_id)
                .order_by(_StageEventRow.sequence)
            ).all()
            return [StageEvent.model_validate(row.record_json) for row in rows]

    def recent_review_durations_ms(
        self,
        *,
        org_id: str,
        evidence_profile: str,
        document_count: int,
        limit: int = 30,
    ) -> list[int]:
        bounded_limit = max(0, min(limit, 30))
        if bounded_limit == 0:
            return []
        with Session(self._engine) as session:
            jobs = session.scalars(
                select(_JobRow)
                .where(
                    _JobRow.org_id == org_id,
                    _JobRow.status == JobStatus.succeeded.value,
                    _JobRow.record_json["request"]["experience_api_version"].as_string() == "3.0",
                    _JobRow.record_json["request"]["evidence_profile"].as_string()
                    == evidence_profile,
                    _JobRow.record_json["request"]["document_count"].as_integer() == document_count,
                )
                .order_by(_JobRow.agent_call_started_at.desc(), _JobRow.job_id.desc())
                .limit(bounded_limit)
            ).all()
            if not jobs:
                return []
            job_ids = [row.job_id for row in jobs]
            event_rows = session.scalars(
                select(_StageEventRow)
                .where(
                    _StageEventRow.org_id == org_id,
                    _StageEventRow.job_id.in_(job_ids),
                )
                .order_by(_StageEventRow.job_id, _StageEventRow.sequence)
            ).all()
            events_by_job: dict[str, list[StageEvent]] = {job_id: [] for job_id in job_ids}
            for row in event_rows:
                events_by_job[row.job_id].append(StageEvent.model_validate(row.record_json))
            values: list[int] = []
            for job_id in job_ids:
                duration = _review_total_duration_ms(events_by_job[job_id])
                if duration is not None:
                    values.append(duration)
            return values

    def recent_review_stage_durations_ms(
        self,
        *,
        org_id: str,
        evidence_profile: str,
        document_count: int,
        limit: int = 30,
    ) -> list[dict[str, int]]:
        bounded_limit = max(0, min(limit, 30))
        if bounded_limit == 0:
            return []
        with Session(self._engine) as session:
            jobs = session.scalars(
                select(_JobRow)
                .where(
                    _JobRow.org_id == org_id,
                    _JobRow.status == JobStatus.succeeded.value,
                    _JobRow.record_json["request"]["experience_api_version"].as_string() == "3.0",
                    _JobRow.record_json["request"]["evidence_profile"].as_string()
                    == evidence_profile,
                    _JobRow.record_json["request"]["document_count"].as_integer() == document_count,
                )
                .order_by(_JobRow.agent_call_started_at.desc(), _JobRow.job_id.desc())
                .limit(bounded_limit)
            ).all()
            if not jobs:
                return []
            job_ids = [row.job_id for row in jobs]
            event_rows = session.scalars(
                select(_StageEventRow)
                .where(
                    _StageEventRow.org_id == org_id,
                    _StageEventRow.job_id.in_(job_ids),
                )
                .order_by(_StageEventRow.job_id, _StageEventRow.sequence)
            ).all()
            events_by_job: dict[str, list[StageEvent]] = {job_id: [] for job_id in job_ids}
            for row in event_rows:
                events_by_job[row.job_id].append(StageEvent.model_validate(row.record_json))
            return [_review_stage_duration_sample(events_by_job[job_id]) for job_id in job_ids]

    def save_run(self, run: ReviewRunRecord, audit: AuditRecord) -> ReviewRunRecord:
        with Session(self._engine) as session, session.begin():
            existing = session.get(_RunRow, run.review_run_id)
            if existing:
                if existing.result_digest != run.result_digest:
                    raise ConflictError("immutable review run already contains another result")
                return ReviewRunRecord.model_validate(existing.record_json)
            if run.parent_run_id and session.get(_RunRow, run.parent_run_id) is None:
                raise ConflictError("parent review run does not exist")
            session.add(
                _RunRow(
                    review_run_id=run.review_run_id,
                    case_id=run.case_id,
                    org_id=run.org_id,
                    parent_run_id=run.parent_run_id,
                    case_revision=run.case_revision,
                    result_digest=run.result_digest,
                    execution_status=run.execution_status.value,
                    assessment=run.assessment.value,
                    review_disposition=run.review_disposition.value,
                    assurance_level=run.assurance_level.value,
                    provider_result_json={},
                    record_json=self._dump(run),
                )
            )
            self._audit(session, audit)
        return run

    def get_provider_result(self, org_id: str, review_run_id: str) -> dict[str, Any] | None:
        with Session(self._engine) as session:
            row = session.scalar(
                select(_RunRow).where(
                    _RunRow.org_id == org_id, _RunRow.review_run_id == review_run_id
                )
            )
            return dict(row.provider_result_json) if row else None

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
        presentation: ReviewPresentationRecord | None = None,
    ) -> ReviewRunRecord:
        with Session(self._engine) as session, session.begin():
            job_row = session.scalar(
                select(_JobRow)
                .where(_JobRow.org_id == job.org_id, _JobRow.job_id == job.job_id)
                .with_for_update()
            )
            if (
                job_row is None
                or job_row.status != "running"
                or job_row.lease_id != expected_lease_id
                or job_row.agent_call_state != AgentCallState.started.value
                or job.agent_call_state != AgentCallState.completed
                or job_row.agent_call_endpoint != job.agent_call_endpoint
                or job_row.agent_call_request_sha256 != job.agent_call_request_sha256
            ):
                raise ConflictError("job lease is stale")
            case_row = session.scalar(
                select(_CaseRow)
                .where(_CaseRow.org_id == job.org_id, _CaseRow.case_id == job.case_id)
                .with_for_update()
            )
            if case_row is None:
                raise ConflictError("job Case does not exist")
            selected_run = case_drift_run if case_row.revision != run.case_revision else run
            if (
                selected_run.review_run_id != run.review_run_id
                or selected_run.provider_result_sha256 != run.provider_result_sha256
            ):
                raise ConflictError("case-drift Run is not bound to the provider result")
            existing = session.get(_RunRow, selected_run.review_run_id)
            if existing is not None:
                if existing.result_digest != selected_run.result_digest:
                    raise ConflictError("immutable review run already contains another result")
                return ReviewRunRecord.model_validate(existing.record_json)
            if selected_run.parent_run_id:
                parent = session.scalar(
                    select(_RunRow).where(
                        _RunRow.review_run_id == selected_run.parent_run_id,
                        _RunRow.org_id == selected_run.org_id,
                        _RunRow.case_id == selected_run.case_id,
                    )
                )
                if parent is None:
                    raise ConflictError("parent review run does not exist in the case")
            session.add(
                _RunRow(
                    review_run_id=selected_run.review_run_id,
                    case_id=selected_run.case_id,
                    org_id=selected_run.org_id,
                    parent_run_id=selected_run.parent_run_id,
                    case_revision=selected_run.case_revision,
                    result_digest=selected_run.result_digest,
                    execution_status=selected_run.execution_status.value,
                    assessment=selected_run.assessment.value,
                    review_disposition=selected_run.review_disposition.value,
                    assurance_level=selected_run.assurance_level.value,
                    provider_result_json=dict(provider_result),
                    record_json=self._dump(selected_run),
                )
            )
            if presentation is not None:
                # These mapped rows deliberately have no ORM relationship; the
                # database foreign key is authoritative. Flush the immutable Run
                # first so SQLAlchemy cannot choose the child insert before it.
                session.flush()
                if (
                    presentation.review_run_id != run.review_run_id
                    or presentation.org_id != run.org_id
                    or presentation.case_id != run.case_id
                    or presentation.bound_provider_result_sha256 != run.provider_result_sha256
                    or presentation.bound_effective_result_digest != run.result_digest
                ):
                    raise ConflictError("experience presentation is not bound to its effective Run")
                if presentation.bound_effective_result_digest == selected_run.result_digest:
                    session.add(
                        _ReviewPresentationRow(
                            presentation_id=presentation.presentation_id,
                            review_run_id=presentation.review_run_id,
                            case_id=presentation.case_id,
                            org_id=presentation.org_id,
                            bound_provider_result_sha256=(
                                presentation.bound_provider_result_sha256
                            ),
                            bound_effective_result_digest=(
                                presentation.bound_effective_result_digest
                            ),
                            created_at=presentation.created_at,
                            record_json=self._dump(presentation),
                        )
                    )
            job_row.status = job.status.value
            job_row.lease_id = job.lease_id
            job_row.lease_expires_at = job.lease_expires_at
            job_row.attempt_count = job.attempt_count
            job_row.agent_call_state = job.agent_call_state.value
            job_row.agent_call_endpoint = job.agent_call_endpoint
            job_row.agent_call_request_sha256 = job.agent_call_request_sha256
            job_row.agent_call_started_at = job.agent_call_started_at
            job_row.record_json = self._dump(job)
            session.add(
                _StageEventRow(
                    event_id=event.event_id,
                    job_id=event.job_id,
                    org_id=job.org_id,
                    sequence=event.sequence,
                    record_json=self._dump(event),
                )
            )
            self._audit(session, run_audit)
            self._audit(session, job_audit)
        return selected_run

    def get_run(self, org_id: str, review_run_id: str) -> ReviewRunRecord | None:
        with Session(self._engine) as session:
            row = session.scalar(
                select(_RunRow).where(
                    _RunRow.org_id == org_id, _RunRow.review_run_id == review_run_id
                )
            )
            return ReviewRunRecord.model_validate(row.record_json) if row else None

    def list_runs(self, org_id: str, case_id: str) -> list[ReviewRunRecord]:
        with Session(self._engine) as session:
            rows = session.scalars(
                select(_RunRow).where(_RunRow.org_id == org_id, _RunRow.case_id == case_id)
            ).all()
            values = [ReviewRunRecord.model_validate(row.record_json) for row in rows]
            return sorted(
                values,
                key=lambda item: (item.created_at, item.review_run_id),
                reverse=True,
            )

    def save_run_decision(
        self,
        decision: HumanDecisionRecord,
        audit: AuditRecord,
        *,
        expected_case_revision: int,
    ) -> HumanDecisionRecord:
        with Session(self._engine) as session, session.begin():
            # The advisory lock and database unique constraint jointly serialize
            # all terminal decisions for one immutable Run.
            decision_scope = f"run:{decision.review_run_id}"
            session.execute(
                text("SELECT pg_advisory_xact_lock(hashtextextended(:key, 0))"),
                {"key": f"{decision.org_id}:{decision_scope}"},
            )
            case_row = session.scalar(
                select(_CaseRow)
                .where(
                    _CaseRow.org_id == decision.org_id,
                    _CaseRow.case_id == decision.case_id,
                )
                .with_for_update()
            )
            run_row = session.scalar(
                select(_RunRow).where(
                    _RunRow.org_id == decision.org_id,
                    _RunRow.review_run_id == decision.review_run_id,
                )
            )
            if (
                run_row is None
                or case_row is None
                or run_row.case_revision != expected_case_revision
                or case_row.revision != expected_case_revision
            ):
                raise ConflictError("review decision is stale because the Case changed")
            if decision.target_revision_id is not None:
                target = session.scalar(
                    select(_RunRow).where(
                        _RunRow.org_id == decision.org_id,
                        _RunRow.review_run_id == decision.target_revision_id,
                        _RunRow.parent_run_id == decision.review_run_id,
                        _RunRow.case_revision == expected_case_revision,
                    )
                )
                if target is None:
                    raise ConflictError("target revision is stale or unrelated")
            if session.scalar(
                select(_DecisionRow).where(
                    _DecisionRow.org_id == decision.org_id,
                    _DecisionRow.decision_scope == decision_scope,
                )
            ):
                raise ConflictError("review run already has a human decision")
            session.add(
                _DecisionRow(
                    decision_id=decision.decision_id,
                    review_run_id=decision.review_run_id,
                    org_id=decision.org_id,
                    decision_scope=decision_scope,
                    decision=decision.decision,
                    record_json=self._dump(decision),
                )
            )
            self._audit(session, audit)
        return decision

    def list_decisions(self, org_id: str, review_run_id: str) -> list[HumanDecisionRecord]:
        with Session(self._engine) as session:
            rows = session.scalars(
                select(_DecisionRow).where(
                    _DecisionRow.org_id == org_id,
                    _DecisionRow.review_run_id == review_run_id,
                )
            ).all()
            return [HumanDecisionRecord.model_validate(row.record_json) for row in rows]

    def get_proposal_decision(self, org_id: str, proposal_id: str) -> HumanDecisionRecord | None:
        with Session(self._engine) as session:
            row = session.scalar(
                select(_DecisionRow).where(
                    _DecisionRow.org_id == org_id,
                    _DecisionRow.decision_scope == f"proposal:{proposal_id}",
                )
            )
            return HumanDecisionRecord.model_validate(row.record_json) if row else None

    def get_decision_by_idempotency_key(
        self, org_id: str, idempotency_key: str
    ) -> HumanDecisionRecord | None:
        with Session(self._engine) as session:
            row = session.scalar(
                select(_DecisionRow).where(
                    _DecisionRow.org_id == org_id,
                    _DecisionRow.idempotency_key == idempotency_key,
                )
            )
            return HumanDecisionRecord.model_validate(row.record_json) if row else None

    def create_correction_proposal(
        self, proposal: CorrectionProposalRecord, audit: AuditRecord
    ) -> CorrectionProposalRecord:
        with Session(self._engine) as session, session.begin():
            session.execute(
                text("SELECT pg_advisory_xact_lock(hashtextextended(:key, 0))"),
                {"key": f"proposal:{proposal.org_id}:{proposal.idempotency_key}"},
            )
            existing = session.scalar(
                select(_CorrectionProposalRow).where(
                    _CorrectionProposalRow.org_id == proposal.org_id,
                    _CorrectionProposalRow.idempotency_key == proposal.idempotency_key,
                )
            )
            if existing is not None:
                record = CorrectionProposalRecord.model_validate(existing.record_json)
                if record.idempotency_request_digest != proposal.idempotency_request_digest:
                    raise ConflictError("proposal idempotency key was reused with another request")
                return record
            session.add(
                _CorrectionProposalRow(
                    proposal_id=proposal.proposal_id,
                    review_run_id=proposal.review_run_id,
                    case_id=proposal.case_id,
                    org_id=proposal.org_id,
                    idempotency_key=proposal.idempotency_key,
                    idempotency_request_digest=proposal.idempotency_request_digest,
                    status=proposal.status.value,
                    record_json=self._dump(proposal),
                )
            )
            self._audit(session, audit)
        return proposal

    def get_correction_proposal(
        self, org_id: str, proposal_id: str
    ) -> CorrectionProposalRecord | None:
        with Session(self._engine) as session:
            row = session.scalar(
                select(_CorrectionProposalRow).where(
                    _CorrectionProposalRow.org_id == org_id,
                    _CorrectionProposalRow.proposal_id == proposal_id,
                )
            )
            return CorrectionProposalRecord.model_validate(row.record_json) if row else None

    def get_correction_proposal_by_idempotency_key(
        self, org_id: str, idempotency_key: str
    ) -> CorrectionProposalRecord | None:
        with Session(self._engine) as session:
            row = session.scalar(
                select(_CorrectionProposalRow).where(
                    _CorrectionProposalRow.org_id == org_id,
                    _CorrectionProposalRow.idempotency_key == idempotency_key,
                )
            )
            return CorrectionProposalRecord.model_validate(row.record_json) if row else None

    def list_correction_proposals(
        self, org_id: str, review_run_id: str
    ) -> list[CorrectionProposalRecord]:
        with Session(self._engine) as session:
            rows = session.scalars(
                select(_CorrectionProposalRow).where(
                    _CorrectionProposalRow.org_id == org_id,
                    _CorrectionProposalRow.review_run_id == review_run_id,
                )
            ).all()
            return [CorrectionProposalRecord.model_validate(row.record_json) for row in rows]

    def decide_correction_proposal(
        self,
        *,
        proposal: CorrectionProposalRecord,
        decision: HumanDecisionRecord,
        audit: AuditRecord,
        job: JobRecord | None = None,
        event: StageEvent | None = None,
        outbox: OutboxRecord | None = None,
    ) -> CorrectionProposalRecord:
        with Session(self._engine) as session, session.begin():
            if decision.idempotency_key is None:
                raise ConflictError("proposal decision omitted its idempotency binding")
            session.execute(
                text("SELECT pg_advisory_xact_lock(hashtextextended(:key, 0))"),
                {"key": f"decision:{decision.org_id}:{decision.idempotency_key}"},
            )
            if session.scalar(
                select(_DecisionRow).where(
                    _DecisionRow.org_id == decision.org_id,
                    _DecisionRow.idempotency_key == decision.idempotency_key,
                )
            ):
                raise ConflictError("decision idempotency key is already committed")
            case_row = session.scalar(
                select(_CaseRow)
                .where(
                    _CaseRow.org_id == proposal.org_id,
                    _CaseRow.case_id == proposal.case_id,
                )
                .with_for_update()
            )
            run_row = session.scalar(
                select(_RunRow).where(
                    _RunRow.org_id == proposal.org_id,
                    _RunRow.review_run_id == proposal.review_run_id,
                )
            )
            row = session.scalar(
                select(_CorrectionProposalRow)
                .where(
                    _CorrectionProposalRow.org_id == proposal.org_id,
                    _CorrectionProposalRow.proposal_id == proposal.proposal_id,
                )
                .with_for_update()
            )
            if row is None:
                raise ConflictError("correction proposal does not exist")
            current = CorrectionProposalRecord.model_validate(row.record_json)
            if current.status != CorrectionProposalStatus.pending:
                raise ConflictError("correction proposal was already decided")
            if (
                case_row is None
                or run_row is None
                or current.case_id != proposal.case_id
                or current.review_run_id != proposal.review_run_id
                or case_row.revision != current.case_revision
                or run_row.result_digest != current.parent_result_digest
            ):
                raise ConflictError("correction proposal decision is stale")
            if current.created_by == decision.decided_by:
                raise ConflictError("correction proposal requires an independent decision maker")
            if proposal.status == CorrectionProposalStatus.approved:
                if job is None or event is None or outbox is None:
                    raise ConflictError("approved correction proposal requires a child job")
                session.execute(
                    text("SELECT pg_advisory_xact_lock(hashtextextended(:key, 0))"),
                    {"key": f"{job.org_id}:{job.idempotency_key}"},
                )
                if session.scalar(
                    select(_JobRow).where(
                        _JobRow.org_id == job.org_id,
                        _JobRow.idempotency_key == job.idempotency_key,
                    )
                ):
                    raise ConflictError("idempotency key already belongs to another decision")
                session.execute(
                    text("SELECT pg_advisory_xact_lock(hashtextextended(:key, 0))"),
                    {
                        "key": (
                            f"{job.org_id}:{job.case_id}:{job.case_revision}:"
                            f"{job.logical_request_sha256}"
                        )
                    },
                )
                active_rows = session.scalars(
                    select(_JobRow).where(
                        _JobRow.org_id == job.org_id,
                        _JobRow.case_id == job.case_id,
                        _JobRow.case_revision == job.case_revision,
                        _JobRow.status.in_(_ACTIVE_JOB_STATUS_VALUES),
                    )
                ).all()
                if any(
                    row.status in {"delivery_uncertain", "response_persistence_uncertain"}
                    for row in active_rows
                ):
                    raise ConflictError("delivery uncertainty must be reconciled before a new call")
                if any(
                    row.logical_request_sha256 == job.logical_request_sha256 for row in active_rows
                ):
                    raise ConflictError("logical revision request is already active")
                session.add(
                    _JobRow(
                        job_id=job.job_id,
                        case_id=job.case_id,
                        org_id=job.org_id,
                        idempotency_key=job.idempotency_key,
                        request_digest=job.request_digest,
                        case_revision=job.case_revision,
                        logical_request_sha256=job.logical_request_sha256,
                        status=job.status.value,
                        kind=job.kind,
                        lease_id=None,
                        lease_expires_at=None,
                        attempt_count=0,
                        agent_call_state=job.agent_call_state.value,
                        agent_call_endpoint=job.agent_call_endpoint,
                        agent_call_request_sha256=job.agent_call_request_sha256,
                        agent_call_started_at=job.agent_call_started_at,
                        record_json=self._dump(job),
                    )
                )
                session.add(
                    _StageEventRow(
                        event_id=event.event_id,
                        job_id=event.job_id,
                        org_id=job.org_id,
                        sequence=event.sequence,
                        record_json=self._dump(event),
                    )
                )
                session.add(
                    _OutboxRow(
                        outbox_id=outbox.outbox_id,
                        org_id=outbox.org_id,
                        aggregate_id=outbox.aggregate_id,
                        topic=outbox.topic,
                        published_at=None,
                        lease_id=None,
                        lease_expires_at=None,
                        attempt_count=0,
                        record_json=self._dump(outbox),
                    )
                )
            row.status = proposal.status.value
            row.record_json = self._dump(proposal)
            session.add(
                _DecisionRow(
                    decision_id=decision.decision_id,
                    review_run_id=decision.review_run_id,
                    org_id=decision.org_id,
                    decision_scope=f"proposal:{proposal.proposal_id}",
                    idempotency_key=decision.idempotency_key,
                    idempotency_request_digest=decision.idempotency_request_digest,
                    decision=decision.decision,
                    record_json=self._dump(decision),
                )
            )
            self._audit(session, audit)
        return proposal

    def reserve_chat_interaction(
        self,
        interaction: ChatInteractionRecord,
        *,
        actor_window_started_at: datetime,
        actor_window_limit: int,
        review_run_limit: int,
    ) -> tuple[ChatInteractionRecord, bool]:
        with Session(self._engine) as session, session.begin():
            # The run-wide lock makes the 100-interaction cap authoritative even
            # across actors. The actor lock binds the rolling-window check and the
            # insert into one transaction across every Backend process.
            for lock_key in (
                f"chat-quota-run:{interaction.org_id}:{interaction.review_run_id}",
                (
                    "chat-quota-actor:"
                    f"{interaction.org_id}:{interaction.review_run_id}:{interaction.created_by}"
                ),
                f"chat:{interaction.org_id}:{interaction.idempotency_key}",
            ):
                session.execute(
                    text("SELECT pg_advisory_xact_lock(hashtextextended(:key, 0))"),
                    {"key": lock_key},
                )
            existing = session.scalar(
                select(_ChatInteractionRow).where(
                    _ChatInteractionRow.org_id == interaction.org_id,
                    _ChatInteractionRow.idempotency_key == interaction.idempotency_key,
                )
            )
            if existing is not None:
                record = ChatInteractionRecord.model_validate(existing.record_json)
                if record.idempotency_request_digest != interaction.idempotency_request_digest:
                    raise ConflictError("chat idempotency key was reused with another request")
                return record, False
            quota_filters = (
                _ChatInteractionRow.org_id == interaction.org_id,
                _ChatInteractionRow.review_run_id == interaction.review_run_id,
                _ChatInteractionRow.state.in_(_CHAT_QUOTA_STATE_VALUES),
            )
            run_count = session.scalar(
                select(func.count()).select_from(_ChatInteractionRow).where(*quota_filters)
            )
            if int(run_count or 0) >= review_run_limit:
                raise QuotaExceededError("chat quota for this review run is exhausted")
            actor_window_count = session.scalar(
                select(func.count())
                .select_from(_ChatInteractionRow)
                .where(
                    *quota_filters,
                    _ChatInteractionRow.created_by == interaction.created_by,
                    _ChatInteractionRow.created_at >= actor_window_started_at,
                )
            )
            if int(actor_window_count or 0) >= actor_window_limit:
                raise QuotaExceededError("chat rate limit exceeded; retry after one minute")
            session.add(
                _ChatInteractionRow(
                    interaction_id=interaction.interaction_id,
                    review_run_id=interaction.review_run_id,
                    case_id=interaction.case_id,
                    org_id=interaction.org_id,
                    idempotency_key=interaction.idempotency_key,
                    idempotency_request_digest=interaction.idempotency_request_digest,
                    agent_request_sha256=interaction.agent_request_sha256,
                    state=interaction.state.value,
                    message_id=None,
                    created_by=interaction.created_by,
                    created_at=interaction.created_at,
                    record_json=self._dump(interaction),
                )
            )
        return interaction, True

    def start_chat_call_once(self, *, org_id: str, interaction_id: str) -> ChatInteractionRecord:
        with Session(self._engine) as session, session.begin():
            row = session.scalar(
                select(_ChatInteractionRow)
                .where(
                    _ChatInteractionRow.org_id == org_id,
                    _ChatInteractionRow.interaction_id == interaction_id,
                )
                .with_for_update()
            )
            if row is None:
                raise ConflictError("chat interaction does not exist")
            record = ChatInteractionRecord.model_validate(row.record_json)
            if record.state != ChatInteractionState.reserved:
                raise ConflictError("chat Agent call is already started or terminal")
            now = utc_now()
            started = record.model_copy(
                update={
                    "state": ChatInteractionState.call_started,
                    "call_started_at": now,
                    "updated_at": now,
                }
            )
            row.state = started.state.value
            row.record_json = self._dump(started)
        return started

    def complete_chat_interaction(
        self,
        *,
        interaction: ChatInteractionRecord,
        message: ChatRecord,
        audit: AuditRecord,
    ) -> ChatRecord:
        with Session(self._engine) as session, session.begin():
            row = session.scalar(
                select(_ChatInteractionRow)
                .where(
                    _ChatInteractionRow.org_id == interaction.org_id,
                    _ChatInteractionRow.interaction_id == interaction.interaction_id,
                )
                .with_for_update()
            )
            if row is None:
                raise ConflictError("chat interaction does not exist")
            current = ChatInteractionRecord.model_validate(row.record_json)
            if (
                current.state != ChatInteractionState.call_started
                or current.idempotency_request_digest != interaction.idempotency_request_digest
            ):
                raise ConflictError("chat interaction call state is stale")
            completed = current.model_copy(
                update={
                    "state": ChatInteractionState.completed,
                    "message_id": message.message_id,
                    "error_code": None,
                    "updated_at": utc_now(),
                }
            )
            message_row = _ChatRow(
                message_id=message.message_id,
                review_run_id=message.review_run_id,
                org_id=message.org_id,
                bound_result_digest=message.bound_result_digest,
                idempotency_key=message.idempotency_key,
                idempotency_request_digest=message.idempotency_request_digest,
                record_json=self._dump(message),
            )
            session.add(message_row)
            # These ORM models deliberately expose no object relationship. Flush
            # the immutable message first so PostgreSQL can validate the
            # interaction.message_id FK when the same transaction closes the
            # call-once barrier.
            session.flush([message_row])
            row.state = completed.state.value
            row.message_id = message.message_id
            row.record_json = self._dump(completed)
            self._audit(session, audit)
        return message

    def mark_chat_interaction_terminal(
        self,
        *,
        org_id: str,
        interaction_id: str,
        state: ChatInteractionState,
        error_code: str,
        audit: AuditRecord,
    ) -> ChatInteractionRecord:
        if state not in {
            ChatInteractionState.delivery_uncertain,
            ChatInteractionState.response_received_rejected,
            ChatInteractionState.response_persistence_uncertain,
        }:
            raise ValueError("chat terminal state is not an allowed failure state")
        with Session(self._engine) as session, session.begin():
            row = session.scalar(
                select(_ChatInteractionRow)
                .where(
                    _ChatInteractionRow.org_id == org_id,
                    _ChatInteractionRow.interaction_id == interaction_id,
                )
                .with_for_update()
            )
            if row is None:
                raise ConflictError("chat interaction does not exist")
            current = ChatInteractionRecord.model_validate(row.record_json)
            if current.state != ChatInteractionState.call_started:
                raise ConflictError("chat interaction call state is stale")
            terminal = current.model_copy(
                update={"state": state, "error_code": error_code, "updated_at": utc_now()}
            )
            row.state = terminal.state.value
            row.record_json = self._dump(terminal)
            self._audit(session, audit)
        return terminal

    def get_chat_interaction_by_idempotency_key(
        self, org_id: str, idempotency_key: str
    ) -> ChatInteractionRecord | None:
        with Session(self._engine) as session:
            row = session.scalar(
                select(_ChatInteractionRow).where(
                    _ChatInteractionRow.org_id == org_id,
                    _ChatInteractionRow.idempotency_key == idempotency_key,
                )
            )
            return ChatInteractionRecord.model_validate(row.record_json) if row else None

    def get_chat_by_idempotency_key(self, org_id: str, idempotency_key: str) -> ChatRecord | None:
        with Session(self._engine) as session:
            row = session.scalar(
                select(_ChatRow).where(
                    _ChatRow.org_id == org_id,
                    _ChatRow.idempotency_key == idempotency_key,
                )
            )
            return ChatRecord.model_validate(row.record_json) if row else None

    def list_chat(self, org_id: str, review_run_id: str) -> list[ChatRecord]:
        with Session(self._engine) as session:
            rows = session.scalars(
                select(_ChatRow).where(
                    _ChatRow.org_id == org_id, _ChatRow.review_run_id == review_run_id
                )
            ).all()
            return [ChatRecord.model_validate(row.record_json) for row in rows]

    def get_review_presentation(
        self, org_id: str, review_run_id: str
    ) -> ReviewPresentationRecord | None:
        with Session(self._engine) as session:
            row = session.scalar(
                select(_ReviewPresentationRow).where(
                    _ReviewPresentationRow.org_id == org_id,
                    _ReviewPresentationRow.review_run_id == review_run_id,
                )
            )
            return ReviewPresentationRecord.model_validate(row.record_json) if row else None

    def create_assistant_conversation(
        self, record: AssistantConversationRecord, audit: AuditRecord
    ) -> AssistantConversationRecord:
        with Session(self._engine) as session, session.begin():
            run = session.scalar(
                select(_RunRow).where(
                    _RunRow.org_id == record.org_id,
                    _RunRow.review_run_id == record.review_run_id,
                )
            )
            if run is None:
                raise ConflictError("assistant conversation Run does not exist")
            stored_run = ReviewRunRecord.model_validate(run.record_json)
            if (
                stored_run.case_id != record.case_id
                or stored_run.provider_result_sha256 != record.bound_result_sha256
                or stored_run.result_digest != record.bound_effective_result_digest
            ):
                raise ConflictError("assistant conversation is not bound to the current Run")
            existing = session.get(_AssistantConversationRow, record.conversation_id)
            if existing is not None:
                previous = AssistantConversationRecord.model_validate(existing.record_json)
                if previous != record:
                    raise ConflictError("conversation ID already binds another record")
                return previous
            session.add(
                _AssistantConversationRow(
                    conversation_id=record.conversation_id,
                    review_run_id=record.review_run_id,
                    case_id=record.case_id,
                    org_id=record.org_id,
                    bound_result_sha256=record.bound_result_sha256,
                    bound_effective_result_digest=record.bound_effective_result_digest,
                    created_at=record.created_at,
                    record_json=self._dump(record),
                )
            )
            self._audit(session, audit)
        return record

    def get_assistant_conversation(
        self, org_id: str, conversation_id: str
    ) -> AssistantConversationRecord | None:
        with Session(self._engine) as session:
            row = session.scalar(
                select(_AssistantConversationRow).where(
                    _AssistantConversationRow.org_id == org_id,
                    _AssistantConversationRow.conversation_id == conversation_id,
                )
            )
            return AssistantConversationRecord.model_validate(row.record_json) if row else None

    def list_assistant_conversations(
        self, org_id: str, review_run_id: str
    ) -> list[AssistantConversationRecord]:
        with Session(self._engine) as session:
            rows = session.scalars(
                select(_AssistantConversationRow)
                .where(
                    _AssistantConversationRow.org_id == org_id,
                    _AssistantConversationRow.review_run_id == review_run_id,
                )
                .order_by(_AssistantConversationRow.created_at)
            ).all()
            return [AssistantConversationRecord.model_validate(row.record_json) for row in rows]

    def enqueue_assistant_message(
        self,
        *,
        conversation: AssistantConversationRecord,
        user_message: AssistantMessageRecord,
        job: AssistantJobRecord,
        outbox: OutboxRecord,
        audit: AuditRecord,
        expected_case_revision: int,
        actor_window_started_at: datetime,
        actor_window_limit: int,
        review_run_limit: int,
    ) -> tuple[AssistantJobRecord, AssistantMessageRecord, bool]:
        with Session(self._engine) as session, session.begin():
            # Conversations are append-only, so the runtime role intentionally
            # has no UPDATE privilege and therefore cannot use SELECT FOR UPDATE.
            # Serialize one conversation's parent check and active-job insert
            # with a transaction-scoped advisory lock instead.
            for lock_key in (
                f"assistant-quota-run:{job.org_id}:{job.review_run_id}",
                f"assistant-quota-actor:{job.org_id}:{job.review_run_id}:{job.created_by}",
                f"assistant-conversation:{conversation.org_id}:{conversation.conversation_id}",
                f"assistant-idempotency:{job.org_id}:{job.idempotency_key}",
            ):
                session.execute(
                    text("SELECT pg_advisory_xact_lock(hashtextextended(:key, 0))"),
                    {"key": lock_key},
                )
            case_row = session.scalar(
                select(_CaseRow)
                .where(
                    _CaseRow.org_id == conversation.org_id,
                    _CaseRow.case_id == conversation.case_id,
                )
                .with_for_update()
            )
            run_row = session.scalar(
                select(_RunRow).where(
                    _RunRow.org_id == conversation.org_id,
                    _RunRow.review_run_id == conversation.review_run_id,
                    _RunRow.case_id == conversation.case_id,
                )
            )
            conversation_row = session.scalar(
                select(_AssistantConversationRow).where(
                    _AssistantConversationRow.org_id == conversation.org_id,
                    _AssistantConversationRow.conversation_id == conversation.conversation_id,
                )
            )
            if conversation_row is None or (
                AssistantConversationRecord.model_validate(conversation_row.record_json)
                != conversation
            ):
                raise ConflictError("assistant conversation binding is stale")
            if case_row is None or run_row is None:
                raise ConflictError("assistant Case or review result is unavailable")
            stored_run = ReviewRunRecord.model_validate(run_row.record_json)
            if (
                case_row.revision != expected_case_revision
                or stored_run.case_revision != expected_case_revision
                or stored_run.provider_result_sha256 != conversation.bound_result_sha256
                or stored_run.result_digest != conversation.bound_effective_result_digest
            ):
                raise ConflictError("assistant Case or review result binding is stale")
            existing = session.scalar(
                select(_AssistantJobRow).where(
                    _AssistantJobRow.org_id == job.org_id,
                    _AssistantJobRow.idempotency_key == job.idempotency_key,
                )
            )
            if existing is not None:
                previous = AssistantJobRecord.model_validate(existing.record_json)
                if previous.idempotency_request_digest != job.idempotency_request_digest:
                    raise ConflictError("assistant idempotency key was reused")
                message_row = session.get(_AssistantMessageRow, previous.user_message_id)
                if message_row is None:
                    raise ConflictError("assistant job references a missing user message")
                return (
                    previous,
                    AssistantMessageRecord.model_validate(message_row.record_json),
                    False,
                )
            quota_filters = (
                _AssistantJobRow.org_id == job.org_id,
                _AssistantJobRow.review_run_id == job.review_run_id,
            )
            run_count = session.scalar(
                select(func.count()).select_from(_AssistantJobRow).where(*quota_filters)
            )
            if int(run_count or 0) >= review_run_limit:
                raise QuotaExceededError("assistant quota for this review run is exhausted")
            actor_window_count = session.scalar(
                select(func.count())
                .select_from(_AssistantJobRow)
                .where(
                    *quota_filters,
                    _AssistantJobRow.created_by == job.created_by,
                    _AssistantJobRow.created_at >= actor_window_started_at,
                )
            )
            if int(actor_window_count or 0) >= actor_window_limit:
                raise QuotaExceededError("assistant rate limit exceeded; retry after one minute")
            active = session.scalar(
                select(_AssistantJobRow).where(
                    _AssistantJobRow.conversation_id == conversation.conversation_id,
                    _AssistantJobRow.state.in_(
                        [AssistantJobState.queued.value, AssistantJobState.running.value]
                    ),
                )
            )
            if active is not None:
                raise ConflictError("assistant conversation already has an active job")
            latest = session.scalar(
                select(_AssistantMessageRow)
                .where(
                    _AssistantMessageRow.org_id == job.org_id,
                    _AssistantMessageRow.conversation_id == conversation.conversation_id,
                )
                .order_by(
                    _AssistantMessageRow.created_at.desc(),
                    _AssistantMessageRow.message_id.desc(),
                )
                .limit(1)
            )
            expected_parent = latest.message_id if latest else None
            if user_message.parent_message_id != expected_parent:
                raise ConflictError("assistant message parent is stale")
            if (
                user_message.conversation_id != conversation.conversation_id
                or user_message.review_run_id != conversation.review_run_id
                or user_message.case_id != conversation.case_id
                or user_message.org_id != conversation.org_id
                or user_message.bound_result_sha256 != conversation.bound_result_sha256
                or job.user_message_id != user_message.message_id
                or job.conversation_id != conversation.conversation_id
            ):
                raise ConflictError("assistant message or job binding is invalid")
            session.add(
                _AssistantMessageRow(
                    message_id=user_message.message_id,
                    conversation_id=user_message.conversation_id,
                    review_run_id=user_message.review_run_id,
                    org_id=user_message.org_id,
                    role=user_message.role,
                    created_at=user_message.created_at,
                    record_json=self._dump(user_message),
                )
            )
            session.add(
                _AssistantJobRow(
                    assistant_job_id=job.assistant_job_id,
                    conversation_id=job.conversation_id,
                    review_run_id=job.review_run_id,
                    case_id=job.case_id,
                    org_id=job.org_id,
                    idempotency_key=job.idempotency_key,
                    idempotency_request_digest=job.idempotency_request_digest,
                    created_by=job.created_by,
                    state=job.state.value,
                    created_at=job.created_at,
                    record_json=self._dump(job),
                )
            )
            session.add(
                _OutboxRow(
                    outbox_id=outbox.outbox_id,
                    org_id=outbox.org_id,
                    aggregate_id=outbox.aggregate_id,
                    topic=outbox.topic,
                    published_at=outbox.published_at,
                    lease_id=outbox.lease_id,
                    lease_expires_at=outbox.lease_expires_at,
                    attempt_count=outbox.attempt_count,
                    record_json=self._dump(outbox),
                )
            )
            self._audit(session, audit)
        return job, user_message, True

    def get_assistant_job(self, org_id: str, assistant_job_id: str) -> AssistantJobRecord | None:
        with Session(self._engine) as session:
            row = session.scalar(
                select(_AssistantJobRow).where(
                    _AssistantJobRow.org_id == org_id,
                    _AssistantJobRow.assistant_job_id == assistant_job_id,
                )
            )
            return AssistantJobRecord.model_validate(row.record_json) if row else None

    def get_assistant_job_by_idempotency_key(
        self, org_id: str, idempotency_key: str
    ) -> AssistantJobRecord | None:
        with Session(self._engine) as session:
            row = session.scalar(
                select(_AssistantJobRow).where(
                    _AssistantJobRow.org_id == org_id,
                    _AssistantJobRow.idempotency_key == idempotency_key,
                )
            )
            return AssistantJobRecord.model_validate(row.record_json) if row else None

    def list_assistant_jobs(self, org_id: str, conversation_id: str) -> list[AssistantJobRecord]:
        with Session(self._engine) as session:
            rows = session.scalars(
                select(_AssistantJobRow)
                .where(
                    _AssistantJobRow.org_id == org_id,
                    _AssistantJobRow.conversation_id == conversation_id,
                )
                .order_by(
                    _AssistantJobRow.created_at,
                    _AssistantJobRow.assistant_job_id,
                )
            ).all()
            return [AssistantJobRecord.model_validate(row.record_json) for row in rows]

    def claim_assistant_job(
        self,
        *,
        org_id: str,
        assistant_job_id: str,
        lease_id: str,
        lease_expires_at: datetime,
        event: AssistantStageEvent,
    ) -> AssistantJobRecord:
        with Session(self._engine) as session, session.begin():
            row = session.scalar(
                select(_AssistantJobRow)
                .where(
                    _AssistantJobRow.org_id == org_id,
                    _AssistantJobRow.assistant_job_id == assistant_job_id,
                )
                .with_for_update()
            )
            if row is None:
                raise ConflictError("assistant job does not exist")
            value = AssistantJobRecord.model_validate(row.record_json)
            if value.state != AssistantJobState.queued:
                raise ConflictError("assistant job is not runnable")
            now = utc_now()
            if lease_expires_at <= now:
                raise ConflictError("assistant job lease must expire in the future")
            _validate_assistant_event_append(value, event)
            started = _updated_assistant_job(
                value,
                state=AssistantJobState.running,
                call_started_at=now,
                lease_id=lease_id,
                lease_expires_at=lease_expires_at,
                events=[*value.events, event],
                updated_at=now,
            )
            row.state = started.state.value
            row.record_json = self._dump(started)
        return started

    def append_assistant_event(
        self,
        *,
        org_id: str,
        assistant_job_id: str,
        expected_lease_id: str,
        event: AssistantStageEvent,
    ) -> AssistantJobRecord:
        with Session(self._engine) as session, session.begin():
            row = session.scalar(
                select(_AssistantJobRow)
                .where(
                    _AssistantJobRow.org_id == org_id,
                    _AssistantJobRow.assistant_job_id == assistant_job_id,
                )
                .with_for_update()
            )
            if row is None:
                raise ConflictError("assistant job does not exist")
            value = AssistantJobRecord.model_validate(row.record_json)
            if value.state != AssistantJobState.running or value.lease_id != expected_lease_id:
                raise ConflictError("assistant job is not running")
            _validate_assistant_event_append(value, event)
            updated = _updated_assistant_job(
                value,
                events=[*value.events, event],
                updated_at=utc_now(),
            )
            row.record_json = self._dump(updated)
        return updated

    def complete_assistant_job(
        self,
        *,
        job: AssistantJobRecord,
        answer_message: AssistantMessageRecord,
        events: tuple[AssistantStageEvent, AssistantStageEvent],
        audit: AuditRecord,
        expected_lease_id: str,
        expected_case_revision: int,
    ) -> AssistantJobRecord:
        with Session(self._engine) as session, session.begin():
            row = session.scalar(
                select(_AssistantJobRow)
                .where(
                    _AssistantJobRow.org_id == job.org_id,
                    _AssistantJobRow.assistant_job_id == job.assistant_job_id,
                )
                .with_for_update()
            )
            if row is None:
                raise ConflictError("assistant job does not exist")
            current = AssistantJobRecord.model_validate(row.record_json)
            if current.state != AssistantJobState.running or current.lease_id != expected_lease_id:
                raise ConflictError("assistant job is not running")
            case_row = session.scalar(
                select(_CaseRow)
                .where(
                    _CaseRow.org_id == current.org_id,
                    _CaseRow.case_id == current.case_id,
                )
                .with_for_update()
            )
            run_row = session.scalar(
                select(_RunRow).where(
                    _RunRow.org_id == current.org_id,
                    _RunRow.review_run_id == current.review_run_id,
                    _RunRow.case_id == current.case_id,
                )
            )
            conversation_row = session.scalar(
                select(_AssistantConversationRow).where(
                    _AssistantConversationRow.org_id == current.org_id,
                    _AssistantConversationRow.conversation_id == current.conversation_id,
                )
            )
            if case_row is None or run_row is None or conversation_row is None:
                raise ConflictError("assistant Case or review result is unavailable")
            stored_run = ReviewRunRecord.model_validate(run_row.record_json)
            conversation = AssistantConversationRecord.model_validate(conversation_row.record_json)
            if (
                case_row.revision != expected_case_revision
                or stored_run.case_revision != expected_case_revision
                or stored_run.provider_result_sha256 != conversation.bound_result_sha256
                or stored_run.result_digest != conversation.bound_effective_result_digest
            ):
                raise ConflictError("assistant Case or review result changed during the call")
            if (
                answer_message.role != "assistant"
                or answer_message.conversation_id != current.conversation_id
                or answer_message.parent_message_id != current.user_message_id
                or job.answer_message_id != answer_message.message_id
                or job.state != AssistantJobState.succeeded
            ):
                raise ConflictError("assistant result is not bound to its job")
            completed_events = _assistant_completion_events(current, events)
            completed = _updated_assistant_job(
                job,
                events=completed_events,
                lease_id=None,
                lease_expires_at=None,
                updated_at=utc_now(),
            )
            session.add(
                _AssistantMessageRow(
                    message_id=answer_message.message_id,
                    conversation_id=answer_message.conversation_id,
                    review_run_id=answer_message.review_run_id,
                    org_id=answer_message.org_id,
                    role=answer_message.role,
                    created_at=answer_message.created_at,
                    record_json=self._dump(answer_message),
                )
            )
            row.state = completed.state.value
            row.record_json = self._dump(completed)
            self._audit(session, audit)
        return completed

    def mark_assistant_job_terminal(
        self,
        *,
        org_id: str,
        assistant_job_id: str,
        state: AssistantJobState,
        error_code: str,
        event: AssistantStageEvent,
        audit: AuditRecord,
        expected_lease_id: str,
    ) -> AssistantJobRecord:
        with Session(self._engine) as session, session.begin():
            row = session.scalar(
                select(_AssistantJobRow)
                .where(
                    _AssistantJobRow.org_id == org_id,
                    _AssistantJobRow.assistant_job_id == assistant_job_id,
                )
                .with_for_update()
            )
            if row is None:
                raise ConflictError("assistant job does not exist")
            value = AssistantJobRecord.model_validate(row.record_json)
            if (
                value.state != AssistantJobState.running
                or value.lease_id != expected_lease_id
                or state
                in {
                    AssistantJobState.queued,
                    AssistantJobState.running,
                    AssistantJobState.succeeded,
                }
            ):
                raise ConflictError("assistant job terminal transition is stale")
            _validate_assistant_event_append(value, event)
            terminal = _updated_assistant_job(
                value,
                state=state,
                error_code=error_code,
                lease_id=None,
                lease_expires_at=None,
                events=[*value.events, event],
                updated_at=utc_now(),
            )
            row.state = terminal.state.value
            row.record_json = self._dump(terminal)
            self._audit(session, audit)
        return terminal

    def list_assistant_messages(
        self, org_id: str, conversation_id: str
    ) -> list[AssistantMessageRecord]:
        with Session(self._engine) as session:
            rows = session.scalars(
                select(_AssistantMessageRow)
                .where(
                    _AssistantMessageRow.org_id == org_id,
                    _AssistantMessageRow.conversation_id == conversation_id,
                )
                .order_by(
                    _AssistantMessageRow.created_at,
                    _AssistantMessageRow.message_id,
                )
            ).all()
            return [AssistantMessageRecord.model_validate(row.record_json) for row in rows]

    def list_audit(self, org_id: str, case_id: str) -> list[AuditRecord]:
        with Session(self._engine) as session:
            rows = session.scalars(
                select(_AuditRow)
                .where(_AuditRow.org_id == org_id, _AuditRow.case_id == case_id)
                .order_by(_AuditRow.created_at)
            ).all()
            return [AuditRecord.model_validate(row.record_json) for row in rows]

    def list_pending_outbox(self, limit: int = 100) -> list[OutboxRecord]:
        with Session(self._engine) as session:
            rows = session.scalars(
                select(_OutboxRow).where(_OutboxRow.published_at.is_(None)).limit(limit)
            ).all()
            return [OutboxRecord.model_validate(row.record_json) for row in rows]

    def claim_pending_outbox(
        self, *, lease_id: str, lease_expires_at: Any, limit: int = 100
    ) -> list[OutboxRecord]:
        with Session(self._engine) as session, session.begin():
            now = utc_now()
            rows = session.scalars(
                select(_OutboxRow)
                .where(
                    _OutboxRow.published_at.is_(None),
                    (_OutboxRow.lease_expires_at.is_(None) | (_OutboxRow.lease_expires_at <= now)),
                )
                .order_by(_OutboxRow.outbox_id)
                .limit(limit)
                .with_for_update(skip_locked=True)
            ).all()
            claimed: list[OutboxRecord] = []
            for row in rows:
                record = OutboxRecord.model_validate(row.record_json)
                updated = record.model_copy(
                    update={
                        "lease_id": lease_id,
                        "lease_expires_at": lease_expires_at,
                        "attempt_count": record.attempt_count + 1,
                    }
                )
                row.lease_id = lease_id
                row.lease_expires_at = lease_expires_at
                row.attempt_count = updated.attempt_count
                row.record_json = self._dump(updated)
                claimed.append(updated)
        return claimed

    def mark_outbox_published(self, outbox_id: str, *, lease_id: str) -> None:
        with Session(self._engine) as session, session.begin():
            row = session.scalar(
                select(_OutboxRow).where(_OutboxRow.outbox_id == outbox_id).with_for_update()
            )
            if row is None:
                raise ConflictError("outbox record does not exist")
            if row.lease_id != lease_id:
                raise ConflictError("outbox lease is stale")
            if row.published_at is None:
                timestamp = utc_now()
                row.published_at = timestamp
                record = OutboxRecord.model_validate(row.record_json)
                row.lease_id = None
                row.lease_expires_at = None
                row.record_json = self._dump(
                    record.model_copy(
                        update={
                            "published_at": timestamp,
                            "lease_id": None,
                            "lease_expires_at": None,
                        }
                    )
                )

    def release_outbox_claim(self, outbox_id: str, *, lease_id: str) -> None:
        with Session(self._engine) as session, session.begin():
            row = session.scalar(
                select(_OutboxRow).where(_OutboxRow.outbox_id == outbox_id).with_for_update()
            )
            if row is None or row.lease_id != lease_id:
                return
            record = OutboxRecord.model_validate(row.record_json)
            row.lease_id = None
            row.lease_expires_at = None
            row.record_json = self._dump(
                record.model_copy(update={"lease_id": None, "lease_expires_at": None})
            )
