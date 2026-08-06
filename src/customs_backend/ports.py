"""Small port set for the direct local review flow."""

from __future__ import annotations

from typing import Any, Protocol

from customs_backend.domain import (
    CaseRecord,
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
    def update_job(self, job: JobRecord) -> JobRecord: ...
    def complete_review(self, job: JobRecord, run: ReviewRunRecord) -> ReviewRunRecord: ...
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
    def get_job(self, org_id: str, job_id: str) -> JobRecord | None: ...
    def get_run(self, org_id: str, review_run_id: str) -> ReviewRunRecord | None: ...
    def list_runs(self, org_id: str, case_id: str) -> list[ReviewRunRecord]: ...


class ObjectStore(Protocol):
    def health(self) -> bool: ...
    def put_text(self, *, key: str, content: str, content_sha256: str) -> None: ...
    def get_text(self, *, key: str, expected_sha256: str) -> str: ...


class AgentClient(Protocol):
    def review(self, request: Any) -> dict[str, Any]: ...
    def metadata(self) -> dict[str, Any]: ...


class TokenVerifier(Protocol):
    def verify(self, token: str) -> Any: ...
