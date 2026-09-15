"""Default in-memory persistence for `local-development`."""

from __future__ import annotations

import uuid
from datetime import datetime
from threading import RLock
from typing import Any

from customs_backend.domain import (
    CaseClosureRecord,
    CaseRecord,
    ConversationMessageExchange,
    ConversationMessageRecord,
    ConversationRecord,
    CorrectionProposalDecisionRecord,
    CorrectionProposalHistoryEntry,
    CorrectionProposalRecord,
    CorrectionProposalStatus,
    CaseStatus,
    DocumentAssetRecord,
    DocumentRecord,
    ExtractionJobRecord,
    FindingDecisionHistoryEntry,
    FindingDecisionRecord,
    FindingDecisionReasonCode,
    FindingDecisionStatus,
    GuidanceRegistryVersion,
    HsSelectionHistoryEntry,
    HsSelectionRecord,
    JobRecord,
    KnowledgeConversationRecord,
    ReviewRunRecord,
    RasterDocumentExtractionResult,
    RasterExtractionJobRecord,
    RasterTextDocumentExtractionResult,
    RasterTextExtractionJobRecord,
    utc_now,
)
from customs_backend.ports import ConflictError


class InMemoryWorkbenchRepository:
    """Thread-safe storage for cases, text documents, jobs, and results."""

    def __init__(self) -> None:
        self._lock = RLock()
        self._cases: dict[str, CaseRecord] = {}
        self._documents: dict[str, DocumentRecord] = {}
        self._document_assets: dict[str, DocumentAssetRecord] = {}
        self._extraction_jobs: dict[str, ExtractionJobRecord] = {}
        self._raster_extraction_jobs: dict[str, RasterExtractionJobRecord] = {}
        self._raster_extraction_by_asset_profile: dict[tuple[str, str, str, str], str] = {}
        self._raster_text_extraction_jobs: dict[str, RasterTextExtractionJobRecord] = {}
        self._raster_text_extraction_by_asset_profile: dict[tuple[str, str, str, str], str] = {}
        self._jobs: dict[str, JobRecord] = {}
        self._idempotency: dict[tuple[str, str], str] = {}
        self._runs: dict[str, ReviewRunRecord] = {}
        self._conversations: dict[str, ConversationRecord] = {}
        self._messages: dict[str, ConversationMessageRecord] = {}
        self._chat_idempotency: dict[tuple[str, str, str], tuple[str, str, str]] = {}
        # Standalone knowledge conversations deliberately do not share the
        # Case/Review Run maps.  That prevents a global guidance thread from
        # ever being resolved as a case-grounded conversation by accident.
        self._knowledge_conversations: dict[str, KnowledgeConversationRecord] = {}
        self._knowledge_messages: dict[str, ConversationMessageRecord] = {}
        self._knowledge_chat_idempotency: dict[tuple[str, str, str], tuple[str, str, str]] = {}
        self._correction_proposals: dict[str, CorrectionProposalRecord] = {}

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

    def close_case(
        self, *, org_id: str, case_id: str, closure: CaseClosureRecord
    ) -> CaseRecord:
        with self._lock:
            record = self._cases.get(case_id)
            if record is None or record.org_id != org_id:
                raise ConflictError("case does not exist")
            if record.status == CaseStatus.closed:
                raise ConflictError("the case is already closed")
            updated = record.model_copy(
                update={
                    "status": CaseStatus.closed,
                    "closure": closure,
                    "updated_at": utc_now(),
                }
            )
            self._cases[case_id] = self._copy(updated)
            return self._copy(updated)

    def record_case_hs_selection(
        self,
        *,
        org_id: str,
        case_id: str,
        code: str,
        reason: str,
        selected_by: str,
    ) -> HsSelectionRecord:
        """Atomically replace the case's reviewer HS code selection.

        Reviewer-owned history only: the case revision (part of every review
        input digest) is left unchanged, so an already queued review keeps
        its idempotency identity.
        """

        with self._lock:
            record = self._cases.get(case_id)
            if record is None or record.org_id != org_id:
                raise ConflictError("case does not exist")
            now = utc_now()
            entry = HsSelectionHistoryEntry(
                code=code,
                reason=reason,
                selected_by=selected_by,
                selected_at=now,
            )
            history = [*record.hs_selection.history, entry] if record.hs_selection else [entry]
            selection = HsSelectionRecord(
                code=code,
                reason=reason,
                selected_by=selected_by,
                selected_at=now,
                history=history[-100:],
            )
            updated = record.model_copy(update={"hs_selection": selection, "updated_at": now})
            self._cases[case_id] = self._copy(updated)
            return self._copy(selection)

    def delete_case(self, *, org_id: str, case_id: str) -> None:
        """Remove a case and every record attached to it (local admin path).

        Object-store blobs are left behind; both local modes tolerate orphans.
        """

        with self._lock:
            record = self._cases.get(case_id)
            if record is None or record.org_id != org_id:
                raise ConflictError("case does not exist")
            removed_runs = {
                k for k, v in self._runs.items() if v.case_id == case_id
            }
            removed_jobs = {k for k, v in self._jobs.items() if v.case_id == case_id}
            removed_convs = {
                k for k, v in self._conversations.items() if v.review_run_id in removed_runs
            }
            self._cases.pop(case_id, None)
            self._documents = {
                k: v for k, v in self._documents.items() if v.case_id != case_id
            }
            self._document_assets = {
                k: v for k, v in self._document_assets.items() if v.case_id != case_id
            }
            self._extraction_jobs = {
                k: v for k, v in self._extraction_jobs.items() if v.case_id != case_id
            }
            self._jobs = {k: v for k, v in self._jobs.items() if k not in removed_jobs}
            self._runs = {k: v for k, v in self._runs.items() if k not in removed_runs}
            self._conversations = {
                k: v for k, v in self._conversations.items() if k not in removed_convs
            }
            self._messages = {
                k: v for k, v in self._messages.items() if v.conversation_id not in removed_convs
            }
            self._correction_proposals = {
                k: v
                for k, v in self._correction_proposals.items()
                if v.review_run_id not in removed_runs
            }
            self._idempotency = {
                k: v for k, v in self._idempotency.items() if v not in removed_jobs
            }
            self._chat_idempotency = {
                k: v for k, v in self._chat_idempotency.items() if k[1] not in removed_convs
            }

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

    def add_document_asset(self, record: DocumentAssetRecord) -> DocumentAssetRecord:
        with self._lock:
            case = self._cases.get(record.case_id)
            if case is None or case.org_id != record.org_id:
                raise ConflictError("case does not exist")
            if record.asset_id in self._document_assets:
                raise ConflictError("document asset already exists")
            self._document_assets[record.asset_id] = self._copy(record)
            return self._copy(record)

    def list_document_assets(self, org_id: str, case_id: str) -> list[DocumentAssetRecord]:
        with self._lock:
            records = [
                record
                for record in self._document_assets.values()
                if record.org_id == org_id and record.case_id == case_id
            ]
            return self._copy(
                sorted(records, key=lambda record: (record.created_at, record.asset_id))
            )

    def get_document_asset(
        self, org_id: str, case_id: str, asset_id: str
    ) -> DocumentAssetRecord | None:
        with self._lock:
            record = self._document_assets.get(asset_id)
            if record is None or record.org_id != org_id or record.case_id != case_id:
                return None
            return self._copy(record)

    def create_extraction_job(self, job: ExtractionJobRecord) -> ExtractionJobRecord:
        with self._lock:
            asset = self._document_assets.get(job.asset_id)
            if (
                asset is None
                or asset.org_id != job.org_id
                or asset.case_id != job.case_id
                or job.extraction_job_id in self._extraction_jobs
            ):
                raise ConflictError("document extraction job cannot be created")
            self._extraction_jobs[job.extraction_job_id] = self._copy(job)
            return self._copy(job)

    def update_extraction_job(self, job: ExtractionJobRecord) -> ExtractionJobRecord:
        with self._lock:
            existing = self._extraction_jobs.get(job.extraction_job_id)
            if (
                existing is None
                or existing.org_id != job.org_id
                or existing.case_id != job.case_id
                or existing.asset_id != job.asset_id
            ):
                raise ConflictError("document extraction job does not exist")
            self._extraction_jobs[job.extraction_job_id] = self._copy(job)
            return self._copy(job)

    def get_extraction_job(
        self, org_id: str, case_id: str, extraction_job_id: str
    ) -> ExtractionJobRecord | None:
        with self._lock:
            record = self._extraction_jobs.get(extraction_job_id)
            if record is None or record.org_id != org_id or record.case_id != case_id:
                return None
            return self._copy(record)

    def create_raster_extraction_job(
        self, job: RasterExtractionJobRecord
    ) -> tuple[RasterExtractionJobRecord, bool]:
        """Persist one asset/profile queue entry or safely reuse the prior one.

        Binary assets are immutable, so this key also binds the source hash.
        Reusing a terminal record is intentional: retries must not silently
        create a different OCR claim for the same approved profile.
        """

        with self._lock:
            asset = self._document_assets.get(job.asset_id)
            if (
                asset is None
                or asset.org_id != job.org_id
                or asset.case_id != job.case_id
                or asset.source_sha256 != job.source_sha256
            ):
                raise ConflictError("raster extraction job cannot be created")
            key = (job.org_id, job.case_id, job.asset_id, job.profile_revision)
            existing_id = self._raster_extraction_by_asset_profile.get(key)
            if existing_id is not None:
                existing = self._raster_extraction_jobs.get(existing_id)
                if existing is None:
                    raise ConflictError("raster extraction idempotency state is invalid")
                return self._copy(existing), False
            if job.raster_extraction_job_id in self._raster_extraction_jobs:
                raise ConflictError("raster extraction job identifier already exists")
            self._raster_extraction_jobs[job.raster_extraction_job_id] = self._copy(job)
            self._raster_extraction_by_asset_profile[key] = job.raster_extraction_job_id
            return self._copy(job), True

    def get_raster_extraction_job(
        self, org_id: str, case_id: str, raster_extraction_job_id: str
    ) -> RasterExtractionJobRecord | None:
        with self._lock:
            record = self._raster_extraction_jobs.get(raster_extraction_job_id)
            if record is None or record.org_id != org_id or record.case_id != case_id:
                return None
            return self._copy(record)

    def claim_next_raster_extraction_job(
        self, *, now: datetime, lease_expires_at: datetime
    ) -> RasterExtractionJobRecord | None:
        """Atomically claim the oldest queued/expired job with a fresh lease."""

        with self._lock:
            candidates = sorted(
                (
                    item
                    for item in self._raster_extraction_jobs.values()
                    if item.status == "queued"
                    or (
                        item.status == "running"
                        and item.lease_expires_at is not None
                        and item.lease_expires_at <= now
                    )
                ),
                key=lambda item: (item.created_at, item.raster_extraction_job_id),
            )
            for record in candidates:
                if record.attempt_count >= 3:
                    self._raster_extraction_jobs[record.raster_extraction_job_id] = self._copy(
                        record.model_copy(
                            update={
                                "status": "failed",
                                "lease_token": None,
                                "lease_expires_at": None,
                                "error_code": "raster_extraction_attempts_exhausted",
                                "updated_at": now,
                            }
                        )
                    )
                    continue
                claimed = record.model_copy(
                    update={
                        "status": "running",
                        "attempt_count": record.attempt_count + 1,
                        "lease_token": uuid.uuid4().hex,
                        "lease_expires_at": lease_expires_at,
                        "error_code": None,
                        "updated_at": now,
                    }
                )
                self._raster_extraction_jobs[claimed.raster_extraction_job_id] = self._copy(claimed)
                return self._copy(claimed)
            return None

    def complete_raster_extraction_job(
        self,
        *,
        raster_extraction_job_id: str,
        lease_token: str,
        result: RasterDocumentExtractionResult,
        completed_at: datetime,
    ) -> RasterExtractionJobRecord | None:
        with self._lock:
            record = self._raster_extraction_jobs.get(raster_extraction_job_id)
            if (
                record is None
                or record.status != "running"
                or record.lease_token != lease_token
                or result.extraction_request_id != record.raster_extraction_job_id
            ):
                return None
            completed = record.model_copy(
                update={
                    "status": result.status,
                    "result": result,
                    "lease_token": None,
                    "lease_expires_at": None,
                    "error_code": None,
                    "updated_at": completed_at,
                }
            )
            self._raster_extraction_jobs[raster_extraction_job_id] = self._copy(completed)
            return self._copy(completed)

    def mark_raster_extraction_job_unavailable(
        self,
        *,
        raster_extraction_job_id: str,
        expected_profile_revision: str,
        result: RasterDocumentExtractionResult,
        error_code: str,
        completed_at: datetime,
    ) -> RasterExtractionJobRecord | None:
        """Close a job when its immutable profile is no longer usable.

        This is not a retry.  A replacement profile must create a distinct
        asset/profile job so a reviewer never receives text from one profile
        with page geometry from another one.  It is intentionally safe for a
        worker/page-view race: either caller can close the old record and the
        late caller gets the already terminal copy.
        """

        with self._lock:
            record = self._raster_extraction_jobs.get(raster_extraction_job_id)
            if (
                record is None
                or record.profile_revision != expected_profile_revision
                or result.extraction_request_id != record.raster_extraction_job_id
                or result.raster_profile_revision != record.profile_revision
                or result.status != "unavailable"
            ):
                return None
            if record.status == "failed":
                return None
            if record.status == "unavailable":
                return self._copy(record)
            updated = record.model_copy(
                update={
                    "status": "unavailable",
                    "result": result,
                    "lease_token": None,
                    "lease_expires_at": None,
                    "error_code": error_code,
                    "updated_at": completed_at,
                }
            )
            self._raster_extraction_jobs[raster_extraction_job_id] = self._copy(updated)
            return self._copy(updated)

    def fail_raster_extraction_job(
        self,
        *,
        raster_extraction_job_id: str,
        lease_token: str,
        error_code: str,
        retryable: bool,
        completed_at: datetime,
    ) -> RasterExtractionJobRecord | None:
        """Release a matching lease, retrying at most three total attempts."""

        with self._lock:
            record = self._raster_extraction_jobs.get(raster_extraction_job_id)
            if (
                record is None
                or record.status != "running"
                or record.lease_token != lease_token
            ):
                return None
            should_retry = retryable and record.attempt_count < 3
            updated = record.model_copy(
                update={
                    "status": "queued" if should_retry else "failed",
                    "lease_token": None,
                    "lease_expires_at": None,
                    # Do not expose a provider error while a retry may later
                    # succeed.  The public failed code remains generic.
                    "error_code": None if should_retry else error_code,
                    "updated_at": completed_at,
                }
            )
            self._raster_extraction_jobs[raster_extraction_job_id] = self._copy(updated)
            return self._copy(updated)

    def create_raster_text_extraction_job(
        self, job: RasterTextExtractionJobRecord
    ) -> tuple[RasterTextExtractionJobRecord, bool]:
        """Persist one independent text-only asset/profile queue entry.

        The idempotency map is intentionally separate from structured raster
        extraction.  The two paths may use the same source asset and even a
        similarly named profile without granting each other approval.
        """

        with self._lock:
            asset = self._document_assets.get(job.asset_id)
            if (
                asset is None
                or asset.org_id != job.org_id
                or asset.case_id != job.case_id
                or asset.source_sha256 != job.source_sha256
            ):
                raise ConflictError("raster text extraction job cannot be created")
            key = (job.org_id, job.case_id, job.asset_id, job.profile_revision)
            existing_id = self._raster_text_extraction_by_asset_profile.get(key)
            if existing_id is not None:
                existing = self._raster_text_extraction_jobs.get(existing_id)
                if existing is None:
                    raise ConflictError("raster text extraction idempotency state is invalid")
                return self._copy(existing), False
            if job.raster_text_extraction_job_id in self._raster_text_extraction_jobs:
                raise ConflictError("raster text extraction job identifier already exists")
            self._raster_text_extraction_jobs[job.raster_text_extraction_job_id] = self._copy(job)
            self._raster_text_extraction_by_asset_profile[key] = job.raster_text_extraction_job_id
            return self._copy(job), True

    def get_raster_text_extraction_job(
        self, org_id: str, case_id: str, raster_text_extraction_job_id: str
    ) -> RasterTextExtractionJobRecord | None:
        with self._lock:
            record = self._raster_text_extraction_jobs.get(raster_text_extraction_job_id)
            if record is None or record.org_id != org_id or record.case_id != case_id:
                return None
            return self._copy(record)

    def claim_next_raster_text_extraction_job(
        self, *, now: datetime, lease_expires_at: datetime
    ) -> RasterTextExtractionJobRecord | None:
        """Atomically claim the oldest queued/expired text-only OCR job."""

        with self._lock:
            candidates = sorted(
                (
                    item
                    for item in self._raster_text_extraction_jobs.values()
                    if item.status == "queued"
                    or (
                        item.status == "running"
                        and item.lease_expires_at is not None
                        and item.lease_expires_at <= now
                    )
                ),
                key=lambda item: (item.created_at, item.raster_text_extraction_job_id),
            )
            for record in candidates:
                if record.attempt_count >= 3:
                    self._raster_text_extraction_jobs[record.raster_text_extraction_job_id] = self._copy(
                        record.model_copy(
                            update={
                                "status": "failed",
                                "lease_token": None,
                                "lease_expires_at": None,
                                "error_code": "raster_text_extraction_attempts_exhausted",
                                "updated_at": now,
                            }
                        )
                    )
                    continue
                claimed = record.model_copy(
                    update={
                        "status": "running",
                        "attempt_count": record.attempt_count + 1,
                        "lease_token": uuid.uuid4().hex,
                        "lease_expires_at": lease_expires_at,
                        "error_code": None,
                        "updated_at": now,
                    }
                )
                self._raster_text_extraction_jobs[claimed.raster_text_extraction_job_id] = self._copy(
                    claimed
                )
                return self._copy(claimed)
            return None

    def complete_raster_text_extraction_job(
        self,
        *,
        raster_text_extraction_job_id: str,
        lease_token: str,
        result: RasterTextDocumentExtractionResult,
        completed_at: datetime,
    ) -> RasterTextExtractionJobRecord | None:
        with self._lock:
            record = self._raster_text_extraction_jobs.get(raster_text_extraction_job_id)
            if (
                record is None
                or record.status != "running"
                or record.lease_token != lease_token
                or result.extraction_request_id != record.raster_text_extraction_job_id
            ):
                return None
            completed = record.model_copy(
                update={
                    "status": result.status,
                    "result": result,
                    "lease_token": None,
                    "lease_expires_at": None,
                    "error_code": None,
                    "updated_at": completed_at,
                }
            )
            self._raster_text_extraction_jobs[raster_text_extraction_job_id] = self._copy(completed)
            return self._copy(completed)

    def mark_raster_text_extraction_job_unavailable(
        self,
        *,
        raster_text_extraction_job_id: str,
        expected_profile_revision: str,
        result: RasterTextDocumentExtractionResult,
        error_code: str,
        completed_at: datetime,
    ) -> RasterTextExtractionJobRecord | None:
        """Close a stale text-only job without reusing a replacement profile."""

        with self._lock:
            record = self._raster_text_extraction_jobs.get(raster_text_extraction_job_id)
            if (
                record is None
                or record.profile_revision != expected_profile_revision
                or result.extraction_request_id != record.raster_text_extraction_job_id
                or result.raster_text_profile_revision != record.profile_revision
                or result.status != "unavailable"
            ):
                return None
            if record.status == "failed":
                return None
            if record.status == "unavailable":
                return self._copy(record)
            updated = record.model_copy(
                update={
                    "status": "unavailable",
                    "result": result,
                    "lease_token": None,
                    "lease_expires_at": None,
                    "error_code": error_code,
                    "updated_at": completed_at,
                }
            )
            self._raster_text_extraction_jobs[raster_text_extraction_job_id] = self._copy(updated)
            return self._copy(updated)

    def fail_raster_text_extraction_job(
        self,
        *,
        raster_text_extraction_job_id: str,
        lease_token: str,
        error_code: str,
        retryable: bool,
        completed_at: datetime,
    ) -> RasterTextExtractionJobRecord | None:
        """Release a matching text-only lease, retrying at most three times."""

        with self._lock:
            record = self._raster_text_extraction_jobs.get(raster_text_extraction_job_id)
            if (
                record is None
                or record.status != "running"
                or record.lease_token != lease_token
            ):
                return None
            should_retry = retryable and record.attempt_count < 3
            updated = record.model_copy(
                update={
                    "status": "queued" if should_retry else "failed",
                    "lease_token": None,
                    "lease_expires_at": None,
                    "error_code": None if should_retry else error_code,
                    "updated_at": completed_at,
                }
            )
            self._raster_text_extraction_jobs[raster_text_extraction_job_id] = self._copy(updated)
            return self._copy(updated)

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

    def get_review_job_by_idempotency(self, org_id: str, idempotency_key: str) -> JobRecord | None:
        """Return the persisted job behind an idempotency key, if any.

        A revision has to perform this lookup before inspecting the current
        correction-proposal state.  A successful child run marks its inputs as
        applied, but an HTTP retry with the original key must still replay that
        original job rather than being treated as a new revision attempt.
        """

        with self._lock:
            job_id = self._idempotency.get((org_id, idempotency_key))
            if job_id is None:
                return None
            job = self._jobs.get(job_id)
            return self._copy(job) if job is not None and job.org_id == org_id else None

    def update_job(self, job: JobRecord) -> JobRecord:
        with self._lock:
            existing = self._jobs.get(job.job_id)
            if existing is None or existing.org_id != job.org_id:
                raise ConflictError("job does not exist")
            self._jobs[job.job_id] = self._copy(job)
            return self._copy(job)

    def _complete_review_locked(self, job: JobRecord, run: ReviewRunRecord) -> ReviewRunRecord:
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

    def complete_review(self, job: JobRecord, run: ReviewRunRecord) -> ReviewRunRecord:
        with self._lock:
            return self._complete_review_locked(job, run)

    def complete_review_and_apply_correction_proposals(
        self,
        *,
        job: JobRecord,
        run: ReviewRunRecord,
        correction_proposal_ids: list[str],
        applied_by: str,
    ) -> ReviewRunRecord:
        """Atomically make a child result visible and bind its approved inputs.

        Applying first would falsely mark a correction if the Agent fails;
        completing first would let two concurrent revisions consume the same
        proposal.  The aggregate lock keeps those two state changes together.
        """

        with self._lock:
            if run.parent_review_run_id is None or set(run.applied_correction_proposal_ids) != set(
                correction_proposal_ids
            ):
                raise ConflictError("child review is not bound to approved corrections")
            records: list[CorrectionProposalRecord] = []
            for proposal_id in correction_proposal_ids:
                record = self._correction_proposals.get(proposal_id)
                if (
                    record is None
                    or record.org_id != job.org_id
                    or record.review_run_id != run.parent_review_run_id
                    or record.status != CorrectionProposalStatus.approved
                    or record.applied_review_run_id is not None
                ):
                    raise ConflictError("approved correction proposal is no longer available")
                records.append(record)
            self._complete_review_locked(job, run)
            now = utc_now()
            for record in records:
                updated = record.model_copy(
                    update={
                        "applied_review_run_id": run.review_run_id,
                        "applied_by": applied_by,
                        "applied_at": now,
                        "history": [
                            *record.history,
                            CorrectionProposalHistoryEntry(
                                event="applied",
                                reason="Approved correction was applied to a child review run.",
                                actor=applied_by,
                                occurred_at=now,
                            ),
                        ],
                    }
                )
                self._correction_proposals[record.correction_proposal_id] = self._copy(updated)
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
        reason_code: FindingDecisionReasonCode | None = None,
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
                reason_code=reason_code,
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
                    reason_code=reason_code,
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
                        "reason_code": reason_code,
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

    def record_hs_selection(
        self,
        *,
        org_id: str,
        review_run_id: str,
        code: str,
        reason: str,
        selected_by: str,
    ) -> HsSelectionRecord:
        """Atomically replace the run's reviewer HS code selection.

        Agent output is immutable.  This only appends reviewer-owned history
        and deliberately does not recompute the Agent result digest.
        """

        with self._lock:
            run = self._runs.get(review_run_id)
            if run is None or run.org_id != org_id:
                raise ConflictError("review run does not exist")
            now = utc_now()
            entry = HsSelectionHistoryEntry(
                code=code,
                reason=reason,
                selected_by=selected_by,
                selected_at=now,
            )
            history = [*run.hs_selection.history, entry] if run.hs_selection else [entry]
            selection = HsSelectionRecord(
                code=code,
                reason=reason,
                selected_by=selected_by,
                selected_at=now,
                history=history,
            )
            self._runs[review_run_id] = self._copy(
                run.model_copy(update={"hs_selection": selection})
            )
            return self._copy(selection)

    def create_conversation(self, record: ConversationRecord) -> ConversationRecord:
        with self._lock:
            if record.conversation_id in self._conversations:
                raise ConflictError("conversation_id already exists")
            run = self._runs.get(record.review_run_id)
            if run is None or run.org_id != record.org_id:
                raise ConflictError("review run does not exist")
            self._conversations[record.conversation_id] = self._copy(record)
            return self._copy(record)

    def get_conversation(self, org_id: str, conversation_id: str) -> ConversationRecord | None:
        with self._lock:
            record = self._conversations.get(conversation_id)
            return self._copy(record) if record is not None and record.org_id == org_id else None

    def list_conversations(self, org_id: str, review_run_id: str) -> list[ConversationRecord]:
        with self._lock:
            records = [
                value
                for value in self._conversations.values()
                if value.org_id == org_id and value.review_run_id == review_run_id
            ]
            return self._copy(
                sorted(
                    records,
                    key=lambda value: (value.updated_at, value.conversation_id),
                    reverse=True,
                )
            )

    def list_messages(self, org_id: str, conversation_id: str) -> list[ConversationMessageRecord]:
        with self._lock:
            conversation = self._conversations.get(conversation_id)
            if conversation is None or conversation.org_id != org_id:
                return []
            records = [
                value
                for value in self._messages.values()
                if value.conversation_id == conversation_id
            ]
            return self._copy(
                sorted(records, key=lambda value: (value.created_at, value.message_id))
            )

    def _exchange(
        self, user_message_id: str, assistant_message_id: str
    ) -> ConversationMessageExchange:
        try:
            return ConversationMessageExchange(
                user_message=self._copy(self._messages[user_message_id]),
                assistant_message=self._copy(self._messages[assistant_message_id]),
            )
        except KeyError as exc:
            raise ConflictError("conversation exchange is incomplete") from exc

    def get_message_exchange(
        self,
        *,
        org_id: str,
        conversation_id: str,
        idempotency_key: str,
        idempotency_request_digest: str,
    ) -> ConversationMessageExchange | None:
        with self._lock:
            stored = self._chat_idempotency.get((org_id, conversation_id, idempotency_key))
            if stored is None:
                return None
            request_digest, user_message_id, assistant_message_id = stored
            if request_digest != idempotency_request_digest:
                raise ConflictError("idempotency key was reused with another message")
            return self._exchange(user_message_id, assistant_message_id)

    def append_message_exchange(
        self,
        *,
        user_message: ConversationMessageRecord,
        assistant_message: ConversationMessageRecord,
        idempotency_key: str,
        idempotency_request_digest: str,
        conversation_snapshot_id: str | None = None,
        guidance_registry_version: GuidanceRegistryVersion | None = None,
    ) -> tuple[ConversationMessageExchange, bool]:
        with self._lock:
            conversation = self._conversations.get(user_message.conversation_id)
            if (
                conversation is None
                or conversation.conversation_id != assistant_message.conversation_id
            ):
                raise ConflictError("conversation does not exist")
            if (
                user_message.message_id in self._messages
                or assistant_message.message_id in self._messages
            ):
                raise ConflictError("message_id already exists")
            key = (conversation.org_id, conversation.conversation_id, idempotency_key)
            existing = self._chat_idempotency.get(key)
            if existing is not None:
                existing_digest, user_id, assistant_id = existing
                if existing_digest != idempotency_request_digest:
                    raise ConflictError("idempotency key was reused with another message")
                return self._exchange(user_id, assistant_id), False
            if user_message.role.value != "user" or assistant_message.role.value != "assistant":
                raise ConflictError("conversation exchange roles are invalid")
            if guidance_registry_version is not None and conversation_snapshot_id is None:
                raise ConflictError("conversation guidance snapshot binding is incomplete")
            if (
                conversation.snapshot_id is not None
                and conversation_snapshot_id is not None
                and conversation_snapshot_id != conversation.snapshot_id
            ):
                raise ConflictError("conversation cannot switch its resolved snapshot")
            if (
                conversation.guidance_registry_version is not None
                and guidance_registry_version is not None
                and guidance_registry_version != conversation.guidance_registry_version
            ):
                raise ConflictError("conversation cannot switch its guidance registry")
            self._messages[user_message.message_id] = self._copy(user_message)
            self._messages[assistant_message.message_id] = self._copy(assistant_message)
            self._chat_idempotency[key] = (
                idempotency_request_digest,
                user_message.message_id,
                assistant_message.message_id,
            )
            self._conversations[conversation.conversation_id] = self._copy(
                conversation.model_copy(
                    update={
                        "snapshot_id": conversation.snapshot_id or conversation_snapshot_id,
                        "guidance_registry_version": (
                            conversation.guidance_registry_version or guidance_registry_version
                        ),
                        "updated_at": assistant_message.created_at,
                    }
                )
            )
            return self._exchange(user_message.message_id, assistant_message.message_id), True

    def create_knowledge_conversation(
        self, record: KnowledgeConversationRecord
    ) -> KnowledgeConversationRecord:
        with self._lock:
            if record.conversation_id in self._knowledge_conversations:
                raise ConflictError("knowledge conversation_id already exists")
            self._knowledge_conversations[record.conversation_id] = self._copy(record)
            return self._copy(record)

    def get_knowledge_conversation(
        self, org_id: str, owner_subject: str, conversation_id: str
    ) -> KnowledgeConversationRecord | None:
        with self._lock:
            record = self._knowledge_conversations.get(conversation_id)
            return (
                self._copy(record)
                if (
                    record is not None
                    and record.org_id == org_id
                    and record.created_by == owner_subject
                )
                else None
            )

    def list_knowledge_conversations(
        self, org_id: str, owner_subject: str
    ) -> list[KnowledgeConversationRecord]:
        with self._lock:
            records = [
                value
                for value in self._knowledge_conversations.values()
                if value.org_id == org_id and value.created_by == owner_subject
            ]
            return self._copy(
                sorted(
                    records,
                    key=lambda value: (value.updated_at, value.conversation_id),
                    reverse=True,
                )
            )

    def list_knowledge_messages(
        self, org_id: str, owner_subject: str, conversation_id: str
    ) -> list[ConversationMessageRecord]:
        with self._lock:
            conversation = self._knowledge_conversations.get(conversation_id)
            if (
                conversation is None
                or conversation.org_id != org_id
                or conversation.created_by != owner_subject
            ):
                return []
            records = [
                value
                for value in self._knowledge_messages.values()
                if value.conversation_id == conversation_id
            ]
            return self._copy(
                sorted(records, key=lambda value: (value.created_at, value.message_id))
            )

    def _knowledge_exchange(
        self, user_message_id: str, assistant_message_id: str
    ) -> ConversationMessageExchange:
        try:
            return ConversationMessageExchange(
                user_message=self._copy(self._knowledge_messages[user_message_id]),
                assistant_message=self._copy(self._knowledge_messages[assistant_message_id]),
            )
        except KeyError as exc:
            raise ConflictError("knowledge conversation exchange is incomplete") from exc

    def get_knowledge_message_exchange(
        self,
        *,
        org_id: str,
        owner_subject: str,
        conversation_id: str,
        idempotency_key: str,
        idempotency_request_digest: str,
    ) -> ConversationMessageExchange | None:
        with self._lock:
            conversation = self._knowledge_conversations.get(conversation_id)
            if (
                conversation is None
                or conversation.org_id != org_id
                or conversation.created_by != owner_subject
            ):
                return None
            stored = self._knowledge_chat_idempotency.get(
                (org_id, conversation_id, idempotency_key)
            )
            if stored is None:
                return None
            request_digest, user_message_id, assistant_message_id = stored
            if request_digest != idempotency_request_digest:
                raise ConflictError("idempotency key was reused with another knowledge message")
            return self._knowledge_exchange(user_message_id, assistant_message_id)

    def append_knowledge_message_exchange(
        self,
        *,
        user_message: ConversationMessageRecord,
        assistant_message: ConversationMessageRecord,
        knowledge_snapshot_id: str | None,
        idempotency_key: str,
        idempotency_request_digest: str,
        guidance_registry_version: GuidanceRegistryVersion | None = None,
    ) -> tuple[ConversationMessageExchange, bool]:
        with self._lock:
            conversation = self._knowledge_conversations.get(user_message.conversation_id)
            if (
                conversation is None
                or conversation.conversation_id != assistant_message.conversation_id
            ):
                raise ConflictError("knowledge conversation does not exist")
            if user_message.created_by != conversation.created_by:
                raise ConflictError("knowledge message must be created by the conversation owner")
            if (
                user_message.message_id in self._knowledge_messages
                or assistant_message.message_id in self._knowledge_messages
            ):
                raise ConflictError("knowledge message_id already exists")
            key = (conversation.org_id, conversation.conversation_id, idempotency_key)
            existing = self._knowledge_chat_idempotency.get(key)
            if existing is not None:
                existing_digest, user_id, assistant_id = existing
                if existing_digest != idempotency_request_digest:
                    raise ConflictError("idempotency key was reused with another knowledge message")
                return self._knowledge_exchange(user_id, assistant_id), False
            if user_message.role.value != "user" or assistant_message.role.value != "assistant":
                raise ConflictError("knowledge conversation exchange roles are invalid")
            if guidance_registry_version is not None and knowledge_snapshot_id is None:
                raise ConflictError("knowledge guidance snapshot binding is incomplete")
            # A classified V2 no-coverage turn may have no Data snapshot at
            # all.  Preserve an existing conversation pin, while continuing
            # to reject a different non-null snapshot on a later covered
            # turn.
            if (
                conversation.snapshot_id is not None
                and knowledge_snapshot_id is not None
                and knowledge_snapshot_id != conversation.snapshot_id
            ):
                raise ConflictError("knowledge conversation cannot switch its resolved snapshot")
            if (
                conversation.guidance_registry_version is not None
                and guidance_registry_version is not None
                and guidance_registry_version != conversation.guidance_registry_version
            ):
                raise ConflictError("knowledge conversation cannot switch its guidance registry")
            self._knowledge_messages[user_message.message_id] = self._copy(user_message)
            self._knowledge_messages[assistant_message.message_id] = self._copy(assistant_message)
            self._knowledge_chat_idempotency[key] = (
                idempotency_request_digest,
                user_message.message_id,
                assistant_message.message_id,
            )
            self._knowledge_conversations[conversation.conversation_id] = self._copy(
                conversation.model_copy(
                    update={
                        "snapshot_id": conversation.snapshot_id or knowledge_snapshot_id,
                        "guidance_registry_version": (
                            conversation.guidance_registry_version or guidance_registry_version
                        ),
                        "updated_at": assistant_message.created_at,
                    }
                )
            )
            return self._knowledge_exchange(
                user_message.message_id, assistant_message.message_id
            ), True

    def create_correction_proposal(
        self, record: CorrectionProposalRecord
    ) -> CorrectionProposalRecord:
        with self._lock:
            if record.correction_proposal_id in self._correction_proposals:
                raise ConflictError("correction proposal already exists")
            run = self._runs.get(record.review_run_id)
            if (
                run is None
                or run.org_id != record.org_id
                or run.case_id != record.case_id
                or run.result_digest != record.parent_result_digest
                or run.agent_input_manifest_sha256 != record.parent_input_manifest_sha256
            ):
                raise ConflictError("correction proposal is not bound to its parent review")
            self._correction_proposals[record.correction_proposal_id] = self._copy(record)
            return self._copy(record)

    def get_correction_proposal(
        self, org_id: str, correction_proposal_id: str
    ) -> CorrectionProposalRecord | None:
        with self._lock:
            record = self._correction_proposals.get(correction_proposal_id)
            return self._copy(record) if record is not None and record.org_id == org_id else None

    def list_correction_proposals(
        self, org_id: str, review_run_id: str
    ) -> list[CorrectionProposalRecord]:
        with self._lock:
            records = [
                value
                for value in self._correction_proposals.values()
                if value.org_id == org_id and value.review_run_id == review_run_id
            ]
            return self._copy(
                sorted(records, key=lambda value: (value.created_at, value.correction_proposal_id))
            )

    def decide_correction_proposal(
        self,
        *,
        org_id: str,
        correction_proposal_id: str,
        decision: CorrectionProposalDecisionRecord,
    ) -> CorrectionProposalRecord:
        with self._lock:
            record = self._correction_proposals.get(correction_proposal_id)
            if record is None or record.org_id != org_id:
                raise ConflictError("correction proposal does not exist")
            if record.status != CorrectionProposalStatus.proposed or record.decision is not None:
                raise ConflictError("correction proposal has already been decided")
            updated = record.model_copy(
                update={
                    "status": CorrectionProposalStatus(decision.status),
                    "decision": decision,
                    "history": [
                        *record.history,
                        CorrectionProposalHistoryEntry(
                            event=decision.status,
                            reason=decision.reason,
                            actor=decision.decided_by,
                            occurred_at=decision.decided_at,
                        ),
                    ],
                }
            )
            self._correction_proposals[correction_proposal_id] = self._copy(updated)
            return self._copy(updated)

    def latest_review_job(self, org_id: str, case_id: str) -> JobRecord | None:
        with self._lock:
            jobs = [
                job
                for job in self._jobs.values()
                if job.org_id == org_id and job.case_id == case_id
            ]
            if not jobs:
                return None
            return self._copy(max(jobs, key=lambda job: job.created_at))

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
