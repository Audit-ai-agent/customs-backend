"""Direct case-to-Agent review flow for the two local modes."""

from __future__ import annotations

import hashlib
import uuid
from dataclasses import dataclass
from datetime import date
from typing import Any, Literal, cast

from pydantic import ValidationError

from customs_backend.agent_contract import AgentContractPayloadError, validate_agent_v3_payload
from customs_backend.domain import (
    AgentDocumentInput,
    AgentReviewRequest,
    Assessment,
    AssuranceLevel,
    CaseCreate,
    CaseRecord,
    CaseStatus,
    CompletionEligibility,
    DocumentRecord,
    ExecutionStatus,
    FindingDecisionRecord,
    FindingDecisionRequest,
    InputDocumentManifestEntry,
    JobRecord,
    JobStatus,
    MAX_TEXT_DOCUMENT_CHARACTERS,
    MAX_TEXT_DOCUMENT_UTF8_BYTES,
    Principal,
    RequiredAction,
    ReviewDisposition,
    ReviewEnqueueRequest,
    ReviewRunRecord,
    Role,
    TextDocumentCreate,
    canonical_digest,
    canonical_json_bytes,
    utc_now,
)
from customs_backend.ports import (
    AgentClient,
    ConflictError,
    NotFoundError,
    ObjectStore,
    WorkbenchRepository,
)


_DECLARED_TYPE_MAP: dict[
    str, Literal["invoice", "packing_list", "bill_of_lading", "certificate_of_origin", "unknown"]
] = {
    "CI": "invoice",
    "PL": "packing_list",
    "BL": "bill_of_lading",
    "CO": "certificate_of_origin",
    "UNKNOWN": "unknown",
}
_MAX_DOCUMENTS = 20
_MAX_AGENT_REQUEST_BYTES = 2 * 1024 * 1024


def _id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex}"


def _scoped_idempotency_key(principal: Principal, case_id: str, supplied_key: str) -> str:
    return "review_" + canonical_digest(
        {"subject": principal.subject, "case_id": case_id, "key": supplied_key}
    )


def _review_documents(documents: list[DocumentRecord]) -> list[DocumentRecord]:
    if not documents:
        raise ConflictError("at least one text document is required")
    if len(documents) > _MAX_DOCUMENTS:
        raise ConflictError("a review accepts at most twenty text documents")
    if any(
        item.character_count > MAX_TEXT_DOCUMENT_CHARACTERS
        or item.byte_size > MAX_TEXT_DOCUMENT_UTF8_BYTES
        for item in documents
    ):
        raise ConflictError("case contains a document outside the text review boundary")
    return sorted(documents, key=lambda item: item.document_id)


def _active_documents(documents: list[DocumentRecord]) -> list[DocumentRecord]:
    """Return the current case input without mutating historic documents.

    Replacement is an append-only correction: a superseded document remains
    available to historic review snapshots and the document history, but is
    never sent to a newly created Agent run.
    """

    return [item for item in documents if item.superseded_by_document_id is None]


def _public_manifest(documents: list[DocumentRecord]) -> list[InputDocumentManifestEntry]:
    return [
        InputDocumentManifestEntry(
            document_id=item.document_id,
            document_type=cast(Literal["CI", "PL", "BL", "CO", "UNKNOWN"], item.document_type),
            content_sha256=item.content_sha256,
        )
        for item in _review_documents(documents)
    ]


def _manifest_digest(
    *,
    case_id: str,
    case_revision: int,
    review_as_of: date,
    trade_type: str,
    documents: list[InputDocumentManifestEntry],
) -> str:
    return canonical_digest(
        {
            "case_id": case_id,
            "case_revision": case_revision,
            "review_as_of": review_as_of.isoformat(),
            "trade_type": trade_type,
            "documents": [item.model_dump(mode="json") for item in documents],
        }
    )


def _result_digest(
    *,
    provider_result_sha256: str,
    input_manifest_digest: str,
    agent_result: dict[str, Any],
) -> str:
    return canonical_digest(
        {
            "provider_result_sha256": provider_result_sha256,
            "input_manifest_digest": input_manifest_digest,
            # The complete schema-validated Agent output covers V3.1 workflow,
            # document facts, comparisons, stable references, and actions.
            # Reviewer-owned decisions are deliberately outside this digest.
            "agent_result": agent_result,
            "human_review_required": True,
        }
    )


def _agent_request_from_snapshot(
    *, object_store: ObjectStore, review_run_id: str, case_id: str, snapshot: dict[str, Any]
) -> AgentReviewRequest:
    try:
        review_as_of = date.fromisoformat(str(snapshot["review_as_of"]))
        trade_type = snapshot["trade_type"]
        if trade_type not in {"import", "export"}:
            raise ValueError("invalid trade type")
        documents = [DocumentRecord.model_validate(value) for value in snapshot["documents"]]
    except (KeyError, TypeError, ValueError, ValidationError) as exc:
        raise ConflictError("review job has an invalid immutable input snapshot") from exc
    return AgentReviewRequest(
        review_run_id=review_run_id,
        case_id=case_id,
        review_as_of=review_as_of,
        trade_type=trade_type,
        documents=[
            AgentDocumentInput(
                document_id=document.document_id,
                filename=document.filename,
                content_text=object_store.get_text(
                    key=document.object_key, expected_sha256=document.content_sha256
                ),
                declared_type=_DECLARED_TYPE_MAP[document.document_type],
            )
            for document in _review_documents(documents)
        ],
    )


@dataclass(frozen=True)
class DocumentView:
    record: DocumentRecord
    content_text: str


class WorkbenchService:
    """Case, text document, and one synchronous human-review workflow."""

    def __init__(
        self, *, repository: WorkbenchRepository, object_store: ObjectStore, agent: AgentClient
    ) -> None:
        self.repository = repository
        self.object_store = object_store
        self.agent = agent

    @staticmethod
    def _require_case_access(principal: Principal, record: CaseRecord) -> None:
        if Role.reviewer in principal.roles:
            return
        raise NotFoundError("case not found")

    def create_case(self, principal: Principal, request: CaseCreate) -> CaseRecord:
        now = utc_now()
        return self.repository.create_case(
            CaseRecord(
                case_id=_id("case"),
                org_id=principal.org_id,
                title=request.title,
                external_reference=request.external_reference,
                revision=0,
                document_manifest_digest=canonical_digest([]),
                metadata=request.metadata,
                created_by=principal.subject,
                created_at=now,
                updated_at=now,
            )
        )

    def get_case(self, principal: Principal, case_id: str) -> CaseRecord:
        record = self.repository.get_case(principal.org_id, case_id)
        if record is None:
            raise NotFoundError("case not found")
        self._require_case_access(principal, record)
        return record

    def list_cases(self, principal: Principal) -> list[CaseRecord]:
        records = self.repository.list_cases(principal.org_id)
        return records

    def add_document(
        self, principal: Principal, case_id: str, request: TextDocumentCreate
    ) -> tuple[CaseRecord, DocumentRecord]:
        case = self.get_case(principal, case_id)
        if case.status == CaseStatus.closed:
            raise ConflictError("documents cannot be added to a closed case")
        payload = request.content_text.encode("utf-8")
        digest = hashlib.sha256(payload).hexdigest()
        document_id = _id("doc")
        object_key = f"{principal.org_id}/{case_id}/{document_id}/{digest}.txt"
        record = DocumentRecord(
            document_id=document_id,
            case_id=case_id,
            org_id=principal.org_id,
            document_type=request.document_type,
            filename=request.filename,
            media_type=request.media_type,
            content_sha256=digest,
            object_key=object_key,
            byte_size=len(payload),
            character_count=len(request.content_text),
            created_by=principal.subject,
            created_at=utc_now(),
            replaces_document_id=request.replaces_document_id,
        )
        existing = self.repository.list_documents(principal.org_id, case_id)
        replaced: DocumentRecord | None = None
        if request.replaces_document_id is not None:
            replaced = next(
                (item for item in existing if item.document_id == request.replaces_document_id),
                None,
            )
            if replaced is None:
                raise NotFoundError("replacement document not found")
            if replaced.document_type != request.document_type:
                raise ConflictError("a replacement must use the same document type")
            if replaced.superseded_by_document_id is not None:
                raise ConflictError("replacement document is already superseded")
        new_manifest_digest = canonical_digest(
            sorted(
                [
                    {"document_id": item.document_id, "content_sha256": item.content_sha256}
                    for item in [
                        *(
                            item
                            for item in _active_documents(existing)
                            if item.document_id != request.replaces_document_id
                        ),
                        record,
                    ]
                ],
                key=lambda item: item["document_id"],
            )
        )
        self.object_store.put_text(
            key=object_key, content=request.content_text, content_sha256=digest
        )
        return (
            self.repository.add_document(
                record,
                expected_revision=case.revision,
                new_manifest_digest=new_manifest_digest,
            ),
            record,
        )

    def list_document_records(self, principal: Principal, case_id: str) -> list[DocumentRecord]:
        self.get_case(principal, case_id)
        return self.repository.list_documents(principal.org_id, case_id)

    def get_document_view(
        self, principal: Principal, case_id: str, document_id: str
    ) -> DocumentView:
        record = next(
            (
                item
                for item in self.list_document_records(principal, case_id)
                if item.document_id == document_id
            ),
            None,
        )
        if record is None:
            raise NotFoundError("document not found")
        return DocumentView(
            record=record,
            content_text=self.object_store.get_text(
                key=record.object_key, expected_sha256=record.content_sha256
            ),
        )

    def review(
        self,
        principal: Principal,
        case_id: str,
        request: ReviewEnqueueRequest,
        idempotency_key: str,
    ) -> tuple[JobRecord, bool]:
        case = self.get_case(principal, case_id)
        if case.status == CaseStatus.closed:
            raise ConflictError("reviews cannot be started for a closed case")
        documents = _active_documents(self.repository.list_documents(principal.org_id, case_id))
        manifest = _public_manifest(documents)
        now = utc_now()
        review_as_of = request.review_as_of or now.date()
        input_manifest_digest = _manifest_digest(
            case_id=case_id,
            case_revision=case.revision,
            review_as_of=review_as_of,
            trade_type=request.trade_type,
            documents=manifest,
        )
        snapshot: dict[str, Any] = {
            "review_as_of": review_as_of.isoformat(),
            "trade_type": request.trade_type,
            "documents": [document.model_dump(mode="json") for document in documents],
            "input_document_manifest": [item.model_dump(mode="json") for item in manifest],
            "input_manifest_digest": input_manifest_digest,
        }
        supplied_digest = canonical_digest(
            {"case_id": case_id, "request": request.model_dump(mode="json")}
        )
        job = JobRecord(
            job_id=_id("job"),
            case_id=case_id,
            org_id=principal.org_id,
            idempotency_key=_scoped_idempotency_key(principal, case_id, idempotency_key),
            idempotency_request_digest=supplied_digest,
            request_digest=canonical_digest(snapshot),
            case_revision=case.revision,
            request=snapshot,
            status=JobStatus.queued,
            review_run_id=_id("run"),
            created_by=principal.subject,
            created_at=now,
            updated_at=now,
        )
        stored, created = self.repository.create_review_job(
            job, expected_case_revision=case.revision
        )
        if not created:
            return stored, False
        return self._run_review(principal, stored), True

    def _build_run(
        self,
        *,
        principal: Principal,
        job: JobRecord,
        case: CaseRecord,
        result: dict[str, Any],
        expected_input_sha256: str,
    ) -> ReviewRunRecord:
        try:
            validate_agent_v3_payload("review_result", result)
            review_as_of = date.fromisoformat(str(job.request["review_as_of"]))
            trade_type = job.request["trade_type"]
            if trade_type not in {"import", "export"}:
                raise ValueError("invalid trade type")
            manifest = [
                InputDocumentManifestEntry.model_validate(value)
                for value in job.request["input_document_manifest"]
            ]
            input_manifest_digest = str(job.request["input_manifest_digest"])
            execution_status = ExecutionStatus(str(result["execution_status"]))
            assessment = Assessment(str(result["assessment"]))
            provider_result_sha256 = str(result["result_sha256"])
            agent_schema_version = str(result["schema_version"])
            agent_status = str(result["status"])
            agent_engine = dict(result["engine"]) if result.get("engine") is not None else None
            agent_jurisdiction = result["jurisdiction"]
            report = dict(result["report"])
            evidence = list(result["evidence"])
            findings = list(result["findings"])
            actions = [RequiredAction.model_validate(value) for value in result["required_actions"]]
            workflow = dict(result["workflow"])
            document_analyses = list(result["document_analyses"])
            comparisons = list(result["comparisons"])
        except (AgentContractPayloadError, KeyError, TypeError, ValueError, ValidationError) as exc:
            raise ConflictError("Agent result does not satisfy the V3 review contract") from exc
        expected_provider_result_sha256 = canonical_digest(
            {key: value for key, value in result.items() if key != "result_sha256"}
        )
        if provider_result_sha256 != expected_provider_result_sha256:
            raise ConflictError("Agent result hash does not cover the complete V3.1 result")
        comparison_ids = [
            str(item.get("comparison_id"))
            for item in comparisons
            if isinstance(item, dict) and isinstance(item.get("comparison_id"), str)
        ]
        finding_ids = [
            str(item.get("finding_id"))
            for item in findings
            if isinstance(item, dict) and isinstance(item.get("finding_id"), str)
        ]
        known_comparison_ids = set(comparison_ids)
        known_finding_ids = set(finding_ids)
        if len(comparison_ids) != len(comparisons) or len(comparison_ids) != len(
            set(comparison_ids)
        ):
            raise ConflictError("Agent result has invalid comparison identifiers")
        if len(finding_ids) != len(findings) or len(finding_ids) != len(set(finding_ids)):
            raise ConflictError("Agent result has invalid finding identifiers")
        if any(
            item.get("comparison_id") is not None
            and item.get("comparison_id") not in known_comparison_ids
            for item in findings
            if isinstance(item, dict)
        ):
            raise ConflictError("Agent finding refers to an unknown comparison")
        if any(
            finding_id not in known_finding_ids
            for action in actions
            for finding_id in action.finding_ids
        ):
            raise ConflictError("Agent action refers to an unknown finding")
        if (
            result.get("case_id") != case.case_id
            or result.get("review_run_id") != job.review_run_id
            or result.get("review_as_of") != review_as_of.isoformat()
            or result.get("trade_type") != trade_type
            or result.get("input_manifest_sha256") != expected_input_sha256
            or result.get("requires_human_review") is not True
        ):
            raise ConflictError("Agent result is not bound to the immutable review input")
        human_action = RequiredAction(
            action_code="human_review_required",
            message="심사 결과는 담당자의 검토와 결정이 필요합니다.",
        )
        persisted_agent_result = result
        if not any(action.action_code == human_action.action_code for action in actions):
            actions.append(human_action)
            # The Backend-owned mandatory human-review action becomes part of
            # the persisted result projection and therefore its digest too.
            persisted_agent_result = {
                **result,
                "required_actions": [
                    *list(result["required_actions"]),
                    human_action.model_dump(mode="json"),
                ],
            }
        eligibility = CompletionEligibility(
            eligible=False, unmet_reason_codes=["human_review_required"]
        )
        return ReviewRunRecord(
            review_run_id=job.review_run_id,
            case_id=case.case_id,
            org_id=job.org_id,
            job_id=job.job_id,
            case_revision=job.case_revision,
            review_as_of=review_as_of,
            trade_type=trade_type,
            input_document_manifest=manifest,
            input_manifest_digest=input_manifest_digest,
            agent_input_manifest_sha256=expected_input_sha256,
            provider_result_sha256=provider_result_sha256,
            result_digest=_result_digest(
                provider_result_sha256=provider_result_sha256,
                input_manifest_digest=input_manifest_digest,
                agent_result=persisted_agent_result,
            ),
            execution_status=execution_status,
            assessment=assessment,
            agent_schema_version=agent_schema_version,
            agent_status=agent_status,
            agent_engine=agent_engine,
            agent_jurisdiction=agent_jurisdiction,
            agent_requires_human_review=True,
            review_disposition=ReviewDisposition.human_review_required,
            assurance_level=AssuranceLevel.diagnostic,
            completion_eligibility=eligibility,
            report=report,
            evidence=evidence,
            findings=findings,
            required_actions=actions,
            workflow=workflow,
            document_analyses=document_analyses,
            comparisons=comparisons,
            release_identity={"status": "non-release", "mode": "human-review-only"},
            created_by=principal.subject,
            created_at=utc_now(),
        )

    def _run_review(self, principal: Principal, job: JobRecord) -> JobRecord:
        running = self.repository.update_job(
            job.model_copy(update={"status": JobStatus.running, "updated_at": utc_now()})
        )
        try:
            case = self.repository.get_case(principal.org_id, running.case_id)
            if case is None:
                raise NotFoundError("case not found")
            agent_request = _agent_request_from_snapshot(
                object_store=self.object_store,
                review_run_id=running.review_run_id,
                case_id=running.case_id,
                snapshot=running.request,
            )
            wire = agent_request.to_agent_wire()
            validate_agent_v3_payload("review_request", wire)
            if len(canonical_json_bytes(wire)) > _MAX_AGENT_REQUEST_BYTES:
                raise ConflictError("Agent review request exceeds the 2 MiB transport limit")
            result = self.agent.review(agent_request)
            run = self._build_run(
                principal=principal,
                job=running,
                case=case,
                result=result,
                expected_input_sha256=agent_request.input_manifest_sha256,
            )
            completed = running.model_copy(
                update={"status": JobStatus.succeeded, "updated_at": utc_now(), "error_code": None}
            )
            self.repository.complete_review(completed, run)
            return completed
        except Exception as exc:
            return self.repository.update_job(
                running.model_copy(
                    update={
                        "status": JobStatus.failed,
                        "error_code": type(exc).__name__,
                        "updated_at": utc_now(),
                    }
                )
            )

    def get_job(self, principal: Principal, job_id: str) -> JobRecord:
        job = self.repository.get_job(principal.org_id, job_id)
        if job is None:
            raise NotFoundError("job not found")
        self.get_case(principal, job.case_id)
        return job

    def get_run(self, principal: Principal, review_run_id: str) -> ReviewRunRecord:
        run = self.repository.get_run(principal.org_id, review_run_id)
        if run is None:
            raise NotFoundError("review run not found")
        self.get_case(principal, run.case_id)
        return run

    def list_runs(self, principal: Principal, case_id: str) -> list[ReviewRunRecord]:
        self.get_case(principal, case_id)
        return self.repository.list_runs(principal.org_id, case_id)

    def record_finding_decision(
        self,
        principal: Principal,
        review_run_id: str,
        finding_id: str,
        request: FindingDecisionRequest,
    ) -> FindingDecisionRecord:
        run = self.get_run(principal, review_run_id)
        known_finding_ids = {
            str(item.get("finding_id"))
            for item in run.findings
            if isinstance(item, dict) and isinstance(item.get("finding_id"), str)
        }
        if finding_id not in known_finding_ids:
            raise NotFoundError("finding not found")
        return self.repository.record_finding_decision(
            org_id=principal.org_id,
            review_run_id=review_run_id,
            finding_id=finding_id,
            status=request.status,
            reason=request.reason.strip(),
            decided_by=principal.subject,
        )
