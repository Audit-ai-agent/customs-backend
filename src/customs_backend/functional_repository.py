"""Optional PostgreSQL persistence for the functional local mode."""

from __future__ import annotations

import json
from typing import Any

from customs_backend.domain import (
    CaseRecord,
    ConversationMessageExchange,
    ConversationMessageRecord,
    ConversationRecord,
    CorrectionProposalDecisionRecord,
    CorrectionProposalRecord,
    DocumentAssetRecord,
    DocumentRecord,
    ExtractionJobRecord,
    FindingDecisionRecord,
    FindingDecisionStatus,
    GuidanceRegistryVersion,
    HsSelectionRecord,
    JobRecord,
    KnowledgeConversationRecord,
    ReviewRunRecord,
)
from customs_backend.repository import InMemoryWorkbenchRepository


def _message_persistence_dump(record: ConversationMessageRecord) -> dict[str, Any]:
    """Serialize a message for local PostgreSQL without widening its public DTO.

    ``registry_source_id`` is intentionally excluded from Pydantic's normal
    model dump so it cannot appear in Browser API responses or OpenAPI. It is
    nevertheless needed in Functional-UAT's local operational state to retain
    a V2 citation's already-validated registry binding across a restart.
    Restore only that fixed identifier here; never persist the Agent-only
    registry digest or change public serialization.
    """

    value = record.model_dump(mode="json")

    def dump_citations(citations: list[Any]) -> list[dict[str, Any]]:
        dumped: list[dict[str, Any]] = []
        for citation in citations:
            entry = citation.model_dump(mode="json")
            if citation.registry_source_id is not None:
                entry["registry_source_id"] = citation.registry_source_id
            dumped.append(entry)
        return dumped

    value["citations"] = dump_citations(record.citations)
    sections = value.get("answer_sections")
    if isinstance(sections, list):
        for dumped_section, section in zip(sections, record.answer_sections, strict=True):
            if isinstance(dumped_section, dict):
                dumped_section["citations"] = dump_citations(section.citations)
    return value


class PostgresWorkbenchRepository(InMemoryWorkbenchRepository):
    """Persist the compact aggregate in one local PostgreSQL row."""

    def __init__(self, database_url: str) -> None:
        super().__init__()
        try:
            import psycopg
        except ImportError as exc:  # pragma: no cover - optional install path
            raise RuntimeError("install the functional-uat extra to use PostgreSQL") from exc
        self._psycopg = psycopg
        self._dsn = database_url
        self._initialize()
        self._restore()

    def _connect(self):
        return self._psycopg.connect(self._dsn)

    def _initialize(self) -> None:
        with self._connect() as connection, connection.cursor() as cursor:
            cursor.execute("CREATE SCHEMA IF NOT EXISTS functional_uat")
            cursor.execute(
                """
                CREATE TABLE IF NOT EXISTS functional_uat.backend_state (
                    singleton boolean PRIMARY KEY DEFAULT true CHECK (singleton),
                    state_json jsonb NOT NULL,
                    updated_at timestamptz NOT NULL DEFAULT now()
                )
                """
            )
            cursor.execute(
                """
                INSERT INTO functional_uat.backend_state(singleton, state_json)
                VALUES (true, '{}'::jsonb)
                ON CONFLICT (singleton) DO NOTHING
                """
            )

    def _restore(self) -> None:
        with self._connect() as connection, connection.cursor() as cursor:
            cursor.execute(
                "SELECT state_json FROM functional_uat.backend_state WHERE singleton = true"
            )
            row = cursor.fetchone()
        if row is None:
            raise RuntimeError("functional Backend state row is missing")
        raw = row[0]
        state = json.loads(raw) if isinstance(raw, str) else raw
        if not isinstance(state, dict):
            raise RuntimeError("functional Backend state is malformed")
        try:
            self._cases = {
                key: CaseRecord.model_validate(value)
                for key, value in dict(state.get("cases", {})).items()
            }
            self._documents = {
                key: DocumentRecord.model_validate(value)
                for key, value in dict(state.get("documents", {})).items()
            }
            self._document_assets = {
                key: DocumentAssetRecord.model_validate(value)
                for key, value in dict(state.get("document_assets", {})).items()
            }
            self._extraction_jobs = {
                key: ExtractionJobRecord.model_validate(value)
                for key, value in dict(state.get("extraction_jobs", {})).items()
            }
            self._jobs = {
                key: JobRecord.model_validate(value)
                for key, value in dict(state.get("jobs", {})).items()
            }
            self._idempotency = {
                (str(item[0]), str(item[1])): str(item[2])
                for item in list(state.get("idempotency", []))
                if isinstance(item, list) and len(item) == 3
            }
            self._runs = {
                key: ReviewRunRecord.model_validate(value)
                for key, value in dict(state.get("runs", {})).items()
            }
            self._conversations = {
                key: ConversationRecord.model_validate(value)
                for key, value in dict(state.get("conversations", {})).items()
            }
            self._messages = {
                key: ConversationMessageRecord.model_validate(value)
                for key, value in dict(state.get("messages", {})).items()
            }
            self._chat_idempotency = {
                (str(item[0]), str(item[1]), str(item[2])): (
                    str(item[3]),
                    str(item[4]),
                    str(item[5]),
                )
                for item in list(state.get("chat_idempotency", []))
                if isinstance(item, list) and len(item) == 6
            }
            self._knowledge_conversations = {
                key: KnowledgeConversationRecord.model_validate(value)
                for key, value in dict(state.get("knowledge_conversations", {})).items()
            }
            self._knowledge_messages = {
                key: ConversationMessageRecord.model_validate(value)
                for key, value in dict(state.get("knowledge_messages", {})).items()
            }
            self._knowledge_chat_idempotency = {
                (str(item[0]), str(item[1]), str(item[2])): (
                    str(item[3]),
                    str(item[4]),
                    str(item[5]),
                )
                for item in list(state.get("knowledge_chat_idempotency", []))
                if isinstance(item, list) and len(item) == 6
            }
            self._correction_proposals = {
                key: CorrectionProposalRecord.model_validate(value)
                for key, value in dict(state.get("correction_proposals", {})).items()
            }
        except (TypeError, ValueError) as exc:
            raise RuntimeError("functional Backend state is invalid") from exc

    def _persist(self) -> None:
        state: dict[str, Any] = {
            "cases": {key: value.model_dump(mode="json") for key, value in self._cases.items()},
            "documents": {
                key: value.model_dump(mode="json") for key, value in self._documents.items()
            },
            "document_assets": {
                key: value.model_dump(mode="json")
                for key, value in self._document_assets.items()
            },
            "extraction_jobs": {
                key: value.model_dump(mode="json")
                for key, value in self._extraction_jobs.items()
            },
            "jobs": {key: value.model_dump(mode="json") for key, value in self._jobs.items()},
            "idempotency": [[org, key, job_id] for (org, key), job_id in self._idempotency.items()],
            "runs": {key: value.model_dump(mode="json") for key, value in self._runs.items()},
            "conversations": {
                key: value.model_dump(mode="json") for key, value in self._conversations.items()
            },
            "messages": {
                key: _message_persistence_dump(value) for key, value in self._messages.items()
            },
            "chat_idempotency": [
                [org, conversation_id, key, request_digest, user_message_id, assistant_message_id]
                for (org, conversation_id, key), (
                    request_digest,
                    user_message_id,
                    assistant_message_id,
                ) in self._chat_idempotency.items()
            ],
            "knowledge_conversations": {
                key: value.model_dump(mode="json")
                for key, value in self._knowledge_conversations.items()
            },
            "knowledge_messages": {
                key: _message_persistence_dump(value)
                for key, value in self._knowledge_messages.items()
            },
            "knowledge_chat_idempotency": [
                [org, conversation_id, key, request_digest, user_message_id, assistant_message_id]
                for (org, conversation_id, key), (
                    request_digest,
                    user_message_id,
                    assistant_message_id,
                ) in self._knowledge_chat_idempotency.items()
            ],
            "correction_proposals": {
                key: value.model_dump(mode="json")
                for key, value in self._correction_proposals.items()
            },
        }
        payload = json.dumps(state, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        with self._connect() as connection, connection.cursor() as cursor:
            cursor.execute(
                """
                UPDATE functional_uat.backend_state
                SET state_json = %s::jsonb, updated_at = now()
                WHERE singleton = true
                """,
                (payload,),
            )
            if cursor.rowcount != 1:
                raise RuntimeError("functional Backend state row could not be saved")

    def health(self) -> bool:
        try:
            with self._connect() as connection, connection.cursor() as cursor:
                cursor.execute("SELECT 1")
                return cursor.fetchone() == (1,)
        except Exception:
            return False

    def create_case(self, record: CaseRecord) -> CaseRecord:
        value = super().create_case(record)
        self._persist()
        return value

    def add_document(
        self, record: DocumentRecord, *, expected_revision: int, new_manifest_digest: str
    ) -> CaseRecord:
        value = super().add_document(
            record, expected_revision=expected_revision, new_manifest_digest=new_manifest_digest
        )
        self._persist()
        return value

    def add_document_asset(self, record: DocumentAssetRecord) -> DocumentAssetRecord:
        value = super().add_document_asset(record)
        self._persist()
        return value

    def create_extraction_job(self, job: ExtractionJobRecord) -> ExtractionJobRecord:
        value = super().create_extraction_job(job)
        self._persist()
        return value

    def update_extraction_job(self, job: ExtractionJobRecord) -> ExtractionJobRecord:
        value = super().update_extraction_job(job)
        self._persist()
        return value

    def create_review_job(
        self, job: JobRecord, *, expected_case_revision: int
    ) -> tuple[JobRecord, bool]:
        value = super().create_review_job(job, expected_case_revision=expected_case_revision)
        if value[1]:
            self._persist()
        return value

    def update_job(self, job: JobRecord) -> JobRecord:
        value = super().update_job(job)
        self._persist()
        return value

    def complete_review(self, job: JobRecord, run: ReviewRunRecord) -> ReviewRunRecord:
        value = super().complete_review(job, run)
        self._persist()
        return value

    def complete_review_and_apply_correction_proposals(
        self,
        *,
        job: JobRecord,
        run: ReviewRunRecord,
        correction_proposal_ids: list[str],
        applied_by: str,
    ) -> ReviewRunRecord:
        value = super().complete_review_and_apply_correction_proposals(
            job=job,
            run=run,
            correction_proposal_ids=correction_proposal_ids,
            applied_by=applied_by,
        )
        self._persist()
        return value

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
        value = super().record_finding_decision(
            org_id=org_id,
            review_run_id=review_run_id,
            finding_id=finding_id,
            status=status,
            reason=reason,
            decided_by=decided_by,
        )
        self._persist()
        return value

    def record_hs_selection(
        self,
        *,
        org_id: str,
        review_run_id: str,
        code: str,
        reason: str,
        selected_by: str,
    ) -> HsSelectionRecord:
        value = super().record_hs_selection(
            org_id=org_id,
            review_run_id=review_run_id,
            code=code,
            reason=reason,
            selected_by=selected_by,
        )
        self._persist()
        return value

    def create_conversation(self, record: ConversationRecord) -> ConversationRecord:
        value = super().create_conversation(record)
        self._persist()
        return value

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
        value = super().append_message_exchange(
            user_message=user_message,
            assistant_message=assistant_message,
            idempotency_key=idempotency_key,
            idempotency_request_digest=idempotency_request_digest,
            conversation_snapshot_id=conversation_snapshot_id,
            guidance_registry_version=guidance_registry_version,
        )
        if value[1]:
            self._persist()
        return value

    def create_knowledge_conversation(
        self, record: KnowledgeConversationRecord
    ) -> KnowledgeConversationRecord:
        value = super().create_knowledge_conversation(record)
        self._persist()
        return value

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
        value = super().append_knowledge_message_exchange(
            user_message=user_message,
            assistant_message=assistant_message,
            knowledge_snapshot_id=knowledge_snapshot_id,
            idempotency_key=idempotency_key,
            idempotency_request_digest=idempotency_request_digest,
            guidance_registry_version=guidance_registry_version,
        )
        if value[1]:
            self._persist()
        return value

    def create_correction_proposal(
        self, record: CorrectionProposalRecord
    ) -> CorrectionProposalRecord:
        value = super().create_correction_proposal(record)
        self._persist()
        return value

    def decide_correction_proposal(
        self,
        *,
        org_id: str,
        correction_proposal_id: str,
        decision: CorrectionProposalDecisionRecord,
    ) -> CorrectionProposalRecord:
        value = super().decide_correction_proposal(
            org_id=org_id,
            correction_proposal_id=correction_proposal_id,
            decision=decision,
        )
        self._persist()
        return value
