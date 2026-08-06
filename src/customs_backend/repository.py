"""Default in-memory persistence for `local-development`."""

from __future__ import annotations

from threading import RLock
from typing import Any

from customs_backend.domain import (
    CaseRecord,
    CaseStatus,
    DocumentRecord,
    FindingDecisionHistoryEntry,
    FindingDecisionRecord,
    FindingDecisionStatus,
    JobRecord,
    ReviewRunRecord,
    utc_now,
)
from customs_backend.ports import ConflictError


class InMemoryWorkbenchRepository:
    """Thread-safe storage for cases, text documents, jobs, and results."""

    def __init__(self) -> None:
        self._lock = RLock()
        self._cases: dict[str, CaseRecord] = {}
        self._documents: dict[str, DocumentRecord] = {}
        self._jobs: dict[str, JobRecord] = {}
        self._idempotency: dict[tuple[str, str], str] = {}
        self._runs: dict[str, ReviewRunRecord] = {}

    def health(self) -> bool:
        return True

    @staticmethod
    def _copy(value: Any) -> Any:
        if isinstance(value, list):
            return [item.model_copy(deep=True) for item in value]
        return value.model_copy(deep=True)

    def create_case(self, record: CaseRecord) -> CaseRecord:
        with self._lock:
            if record.case_id in self._cases:
                raise ConflictError("case_id already exists")
            self._cases[record.case_id] = self._copy(record)
            return self._copy(record)

    def get_case(self, org_id: str, case_id: str) -> CaseRecord | None:
        with self._lock:
            record = self._cases.get(case_id)
            return self._copy(record) if record is not None and record.org_id == org_id else None

    def list_cases(self, org_id: str) -> list[CaseRecord]:
        with self._lock:
            records = [record for record in self._cases.values() if record.org_id == org_id]
            return self._copy(sorted(records, key=lambda record: record.created_at, reverse=True))

    def add_document(
        self,
        record: DocumentRecord,
        *,
        expected_revision: int,
        new_manifest_digest: str,
    ) -> CaseRecord:
        with self._lock:
            case = self._cases.get(record.case_id)
            if case is None or case.org_id != record.org_id:
                raise ConflictError("case does not exist")
            if case.revision != expected_revision:
                raise ConflictError("case revision is stale")
            if record.document_id in self._documents:
                raise ConflictError("document_id already exists")
            replaced: DocumentRecord | None = None
            if record.replaces_document_id is not None:
                replaced = self._documents.get(record.replaces_document_id)
                if (
                    replaced is None
                    or replaced.org_id != record.org_id
                    or replaced.case_id != record.case_id
                ):
                    raise ConflictError("replacement document does not belong to this case")
                if replaced.document_type != record.document_type:
                    raise ConflictError("a replacement must use the same document type")
                if replaced.superseded_by_document_id is not None:
                    raise ConflictError("replacement document is already superseded")
            updated = case.model_copy(
                update={
                    "revision": case.revision + 1,
                    "document_manifest_digest": new_manifest_digest,
                    "updated_at": record.created_at,
                }
            )
            self._documents[record.document_id] = self._copy(record)
            if replaced is not None:
                self._documents[replaced.document_id] = self._copy(
                    replaced.model_copy(
                        update={
                            "superseded_by_document_id": record.document_id,
                            "superseded_at": record.created_at,
                        }
                    )
                )
            self._cases[record.case_id] = self._copy(updated)
            return self._copy(updated)

    def list_documents(self, org_id: str, case_id: str) -> list[DocumentRecord]:
        with self._lock:
            records = [
                record
                for record in self._documents.values()
                if record.org_id == org_id and record.case_id == case_id
            ]
            return self._copy(
                sorted(records, key=lambda record: (record.created_at, record.document_id))
            )

    def create_review_job(
        self, job: JobRecord, *, expected_case_revision: int
    ) -> tuple[JobRecord, bool]:
        with self._lock:
            existing_id = self._idempotency.get((job.org_id, job.idempotency_key))
            if existing_id is not None:
                existing = self._jobs[existing_id]
                if existing.idempotency_request_digest != job.idempotency_request_digest:
                    raise ConflictError("idempotency key was reused with another request")
                return self._copy(existing), False
            case = self._cases.get(job.case_id)
            if case is None or case.org_id != job.org_id:
                raise ConflictError("case does not exist")
            if case.revision != expected_case_revision:
                raise ConflictError("case revision is stale")
            if job.job_id in self._jobs:
                raise ConflictError("job_id already exists")
            self._jobs[job.job_id] = self._copy(job)
            self._idempotency[(job.org_id, job.idempotency_key)] = job.job_id
            self._cases[job.case_id] = self._copy(
                case.model_copy(update={"status": CaseStatus.in_review, "updated_at": utc_now()})
            )
            return self._copy(job), True

    def update_job(self, job: JobRecord) -> JobRecord:
        with self._lock:
            existing = self._jobs.get(job.job_id)
            if existing is None or existing.org_id != job.org_id:
                raise ConflictError("job does not exist")
            self._jobs[job.job_id] = self._copy(job)
            return self._copy(job)

    def complete_review(self, job: JobRecord, run: ReviewRunRecord) -> ReviewRunRecord:
        with self._lock:
            current = self._jobs.get(job.job_id)
            case = self._cases.get(job.case_id)
            if (
                current is None
                or current.org_id != job.org_id
                or case is None
                or case.revision != job.case_revision
            ):
                raise ConflictError("job does not exist")
            existing = self._runs.get(run.review_run_id)
            if existing is not None and existing.result_digest != run.result_digest:
                raise ConflictError("review run already contains another result")
            self._jobs[job.job_id] = self._copy(job)
            self._runs[run.review_run_id] = self._copy(run)
            return self._copy(run)

    def record_finding_decision(
        self,
        *,
        org_id: str,
        review_run_id: str,
        finding_id: str,
        status: FindingDecisionStatus,
        reason: str,
        decided_by: str,
    ) -> FindingDecisionRecord:
        """Atomically replace a finding's current reviewer decision.

        Agent output is immutable.  This only appends reviewer-owned history
        and deliberately does not recompute the Agent result digest.
        """

        with self._lock:
            run = self._runs.get(review_run_id)
            if run is None or run.org_id != org_id:
                raise ConflictError("review run does not exist")
            now = utc_now()
            entry = FindingDecisionHistoryEntry(
                status=status,
                reason=reason,
                decided_by=decided_by,
                decided_at=now,
            )
            existing = next(
                (item for item in run.reviewer_decisions if item.finding_id == finding_id), None
            )
            if existing is None:
                decision = FindingDecisionRecord(
                    finding_id=finding_id,
                    status=status,
                    reason=reason,
                    decided_by=decided_by,
                    decided_at=now,
                    history=[entry],
                )
                decisions = [*run.reviewer_decisions, decision]
            else:
                decision = existing.model_copy(
                    update={
                        "status": status,
                        "reason": reason,
                        "decided_by": decided_by,
                        "decided_at": now,
                        "history": [*existing.history, entry],
                    }
                )
                decisions = [
                    decision if item.finding_id == finding_id else item
                    for item in run.reviewer_decisions
                ]
            self._runs[review_run_id] = self._copy(
                run.model_copy(update={"reviewer_decisions": decisions})
            )
            return self._copy(decision)

    def get_job(self, org_id: str, job_id: str) -> JobRecord | None:
        with self._lock:
            job = self._jobs.get(job_id)
            return self._copy(job) if job is not None and job.org_id == org_id else None

    def get_run(self, org_id: str, review_run_id: str) -> ReviewRunRecord | None:
        with self._lock:
            run = self._runs.get(review_run_id)
            return self._copy(run) if run is not None and run.org_id == org_id else None

    def list_runs(self, org_id: str, case_id: str) -> list[ReviewRunRecord]:
        with self._lock:
            runs = [
                run
                for run in self._runs.values()
                if run.org_id == org_id and run.case_id == case_id
            ]
            return self._copy(
                sorted(runs, key=lambda run: (run.created_at, run.review_run_id), reverse=True)
            )


def __getattr__(name: str) -> Any:
    if name == "PostgresWorkbenchRepository":
        from customs_backend.functional_repository import PostgresWorkbenchRepository

        return PostgresWorkbenchRepository
    raise AttributeError(name)
