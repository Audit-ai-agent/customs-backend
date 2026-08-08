"""Public and service-level guards for standalone knowledge input only."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

import pytest
from fastapi.testclient import TestClient

from customs_backend.adapters import CallableAgentClient, InMemoryObjectStore
from customs_backend.api import Container, create_app
from customs_backend.auth import StaticTokenVerifier
from customs_backend.domain import (
    ConversationMessageRecord,
    ConversationMessageRole,
    AgentKnowledgeAnswerRequest,
    Principal,
    Role,
    utc_now,
)
from customs_backend.repository import InMemoryWorkbenchRepository
from customs_backend.service import WorkbenchService
from customs_backend.settings import Settings
from customs_backend.ports import KnowledgeInputRejectedError
from tests.product.agent_v3_fixture import build_agent_v3_result


@dataclass
class _Harness:
    client: TestClient
    repository: InMemoryWorkbenchRepository
    knowledge_calls: list[object] = field(default_factory=list)
    case_calls: list[object] = field(default_factory=list)


def _harness(
    *,
    knowledge_answer_handler: Callable[[AgentKnowledgeAnswerRequest], dict[str, Any]] | None = None,
) -> _Harness:
    settings = Settings(_env_file=None, environment="test")
    reviewer = Principal(subject="reviewer-1", org_id="local-dev", roles=frozenset({Role.reviewer}))
    approver = Principal(subject="approver-1", org_id="local-dev", roles=frozenset({Role.approver}))
    same_subject_other_org = Principal(
        subject="reviewer-1", org_id="other-org", roles=frozenset({Role.reviewer})
    )
    repository = InMemoryWorkbenchRepository()
    knowledge_calls: list[object] = []
    case_calls: list[object] = []

    def answer_case(request):
        case_calls.append(request)
        return {
            "schema_version": "1.0",
            "case_id": request.case_id,
            "review_run_id": request.review_run_id,
            "conversation_id": request.conversation_id,
            "answer_scope": "clarification_needed",
            "direct_answer": "현재 사건의 심사 근거를 확인해 주세요.",
            "citations": [],
            "answer_sections": [],
            "suggested_questions": [],
            "correction_draft": None,
            "engine": {"kind": "deterministic", "model_id": "ingress-fixture"},
        }

    def answer_knowledge(request):
        knowledge_calls.append(request)
        if knowledge_answer_handler is not None:
            return knowledge_answer_handler(request)
        return {
            "schema_version": "1.0",
            "conversation_id": request.conversation_id,
            "answer_scope": "clarification_needed",
            "direct_answer": "통관 단계나 품목을 알려 주세요.",
            "citations": [],
            "answer_sections": [],
            "suggested_questions": [],
            "coverage": "unavailable",
            "snapshot_id": None,
            "as_of": request.as_of.isoformat(),
            "engine": {"kind": "deterministic", "model_id": "ingress-fixture"},
        }

    agent = CallableAgentClient(
        build_agent_v3_result,
        answer=answer_case,
        knowledge_answer=answer_knowledge,
    )
    service = WorkbenchService(
        repository=repository,
        object_store=InMemoryObjectStore(),
        agent=agent,
    )
    app = create_app(
        settings=settings,
        container=Container(
            settings=settings,
            repository=repository,
            object_store=InMemoryObjectStore(),
            agent=agent,
            token_verifier=StaticTokenVerifier(
                {
                    "test-token": reviewer,
                    "approver-token": approver,
                    "other-org-same-subject-token": same_subject_other_org,
                }
            ),
            service=service,
        ),
    )
    return _Harness(
        client=TestClient(app),
        repository=repository,
        knowledge_calls=knowledge_calls,
        case_calls=case_calls,
    )


def _auth() -> dict[str, str]:
    return {"Authorization": "Bearer test-token"}


def _approver_auth() -> dict[str, str]:
    return {"Authorization": "Bearer approver-token"}


def _same_subject_other_org_auth() -> dict[str, str]:
    return {"Authorization": "Bearer other-org-same-subject-token"}


def _new_knowledge_conversation(harness: _Harness) -> str:
    created = harness.client.post(
        "/api/v1/knowledge/conversations",
        json={"title": "수출 통관 지식 대화", "trade_type": "export"},
        headers=_auth(),
    )
    assert created.status_code == 201, created.text
    conversation_id = created.json().get("conversation_id")
    assert isinstance(conversation_id, str)
    return conversation_id


def test_global_knowledge_rejects_structural_title_without_persistence_or_agent_call() -> None:
    harness = _harness()
    rejected = harness.client.post(
        "/api/v1/knowledge/conversations",
        json={"title": "sample-invoice.txt", "trade_type": "import"},
        headers=_auth(),
    )

    assert rejected.status_code == 422
    assert rejected.json()["code"] == "knowledge_input_not_allowed"
    assert "sample-invoice.txt" not in rejected.text
    assert harness.repository.list_knowledge_conversations("local-dev", "reviewer-1") == []
    assert harness.knowledge_calls == []


def test_global_knowledge_rejects_multiline_title_and_normalises_safe_title() -> None:
    harness = _harness()
    for title in ("일반 문의\n문서처럼 보이지 않는 둘째 줄", "일반 문의\u2028둘째 줄"):
        rejected = harness.client.post(
            "/api/v1/knowledge/conversations",
            json={"title": title, "trade_type": "import"},
            headers=_auth(),
        )

        assert rejected.status_code == 422
        assert rejected.json()["code"] == "knowledge_input_not_allowed"
        assert "둘째 줄" not in rejected.text
    assert harness.repository.list_knowledge_conversations("local-dev", "reviewer-1") == []

    created = harness.client.post(
        "/api/v1/knowledge/conversations",
        json={"title": "임의의 한 줄 제목", "trade_type": "import"},
        headers=_auth(),
    )

    assert created.status_code == 201
    assert created.json()["title"] == "수입 통관 지식 대화"


def test_global_knowledge_rejects_document_like_message_before_exchange_or_agent_call() -> None:
    harness = _harness()
    conversation_id = _new_knowledge_conversation(harness)
    rejected = harness.client.post(
        f"/api/v1/knowledge/conversations/{conversation_id}/messages",
        json={"content": "filename: sample-invoice.txt\nQuantity: 2"},
        headers={**_auth(), "Idempotency-Key": "knowledge-ingress-001"},
    )

    assert rejected.status_code == 422
    assert rejected.json()["code"] == "knowledge_input_not_allowed"
    assert "sample-invoice.txt" not in rejected.text
    assert harness.knowledge_calls == []
    assert (
        harness.repository.list_knowledge_messages("local-dev", "reviewer-1", conversation_id) == []
    )


def test_global_knowledge_rejects_unicode_line_separator_before_agent_call() -> None:
    harness = _harness()
    conversation_id = _new_knowledge_conversation(harness)
    rejected = harness.client.post(
        f"/api/v1/knowledge/conversations/{conversation_id}/messages",
        json={"content": "수출 신고 절차를 알려 주세요.\u2028추가 문장"},
        headers={**_auth(), "Idempotency-Key": "knowledge-ingress-unicode-line-001"},
    )

    assert rejected.status_code == 422
    assert rejected.json()["code"] == "knowledge_input_not_allowed"
    assert "추가 문장" not in rejected.text
    assert harness.knowledge_calls == []
    assert (
        harness.repository.list_knowledge_messages("local-dev", "reviewer-1", conversation_id) == []
    )


def test_global_knowledge_legacy_unsafe_history_is_preserved_but_never_dispatched() -> None:
    harness = _harness()
    conversation_id = _new_knowledge_conversation(harness)
    now = utc_now()
    harness.repository.append_knowledge_message_exchange(
        user_message=ConversationMessageRecord(
            message_id="legacy-user-message",
            conversation_id=conversation_id,
            role=ConversationMessageRole.user,
            content="legacy-invoice.txt",
            created_by="reviewer-1",
            created_at=now,
        ),
        assistant_message=ConversationMessageRecord(
            message_id="legacy-assistant-message",
            conversation_id=conversation_id,
            role=ConversationMessageRole.assistant,
            content="기존 응답",
            created_by="legacy-agent",
            created_at=now,
        ),
        knowledge_snapshot_id=None,
        idempotency_key="legacy-exchange-key",
        idempotency_request_digest="0" * 64,
    )

    rejected = harness.client.post(
        f"/api/v1/knowledge/conversations/{conversation_id}/messages",
        json={"content": "수출 신고 절차를 알려 주세요."},
        headers={**_auth(), "Idempotency-Key": "knowledge-ingress-002"},
    )

    assert rejected.status_code == 422
    assert rejected.json()["code"] == "knowledge_input_not_allowed"
    assert harness.knowledge_calls == []
    # Fail closed without deleting or rewriting a record created before this
    # boundary existed; no second user/assistant exchange is appended.
    assert (
        len(harness.repository.list_knowledge_messages("local-dev", "reviewer-1", conversation_id))
        == 2
    )


def test_global_knowledge_allows_normal_multiline_assistant_history() -> None:
    harness = _harness()
    conversation_id = _new_knowledge_conversation(harness)
    now = utc_now()
    assistant_content = "공식 자료를 기준으로 안내합니다.\n\n확인 절차\n1. 기준일을 확인합니다."
    harness.repository.append_knowledge_message_exchange(
        user_message=ConversationMessageRecord(
            message_id="safe-user-message",
            conversation_id=conversation_id,
            role=ConversationMessageRole.user,
            content="수출 신고 절차를 알려 주세요.",
            created_by="reviewer-1",
            created_at=now,
        ),
        assistant_message=ConversationMessageRecord(
            message_id="safe-assistant-message",
            conversation_id=conversation_id,
            role=ConversationMessageRole.assistant,
            content=assistant_content,
            created_by="agent",
            created_at=now,
        ),
        knowledge_snapshot_id=None,
        idempotency_key="safe-exchange-key",
        idempotency_request_digest="1" * 64,
    )

    answer = harness.client.post(
        f"/api/v1/knowledge/conversations/{conversation_id}/messages",
        json={"content": "원산지 확인 절차도 알려 주세요."},
        headers={**_auth(), "Idempotency-Key": "knowledge-history-safe-001"},
    )

    assert answer.status_code == 200, answer.text
    assert len(harness.knowledge_calls) == 1
    assert any(
        turn.role.value == "assistant" and turn.content == assistant_content
        for turn in harness.knowledge_calls[0].recent_turns
    )
    assert (
        len(harness.repository.list_knowledge_messages("local-dev", "reviewer-1", conversation_id))
        == 4
    )


def test_agent_knowledge_boundary_is_forwarded_as_the_same_generic_public_422() -> None:
    def reject_from_agent(_request: AgentKnowledgeAnswerRequest) -> dict[str, Any]:
        raise KnowledgeInputRejectedError()

    harness = _harness(knowledge_answer_handler=reject_from_agent)
    conversation_id = _new_knowledge_conversation(harness)
    rejected = harness.client.post(
        f"/api/v1/knowledge/conversations/{conversation_id}/messages",
        json={"content": "수출 신고 절차를 알려 주세요."},
        headers={**_auth(), "Idempotency-Key": "knowledge-ingress-003"},
    )

    assert rejected.status_code == 422
    assert rejected.json()["code"] == "knowledge_input_not_allowed"
    assert (
        harness.repository.list_knowledge_messages("local-dev", "reviewer-1", conversation_id) == []
    )


def test_global_knowledge_conversations_are_private_to_their_creator() -> None:
    harness = _harness()
    conversation_id = _new_knowledge_conversation(harness)

    listed = harness.client.get("/api/v1/knowledge/conversations", headers=_approver_auth())
    read = harness.client.get(
        f"/api/v1/knowledge/conversations/{conversation_id}/messages", headers=_approver_auth()
    )
    sent = harness.client.post(
        f"/api/v1/knowledge/conversations/{conversation_id}/messages",
        json={"content": "수출 신고 절차를 알려 주세요."},
        headers={**_approver_auth(), "Idempotency-Key": "knowledge-owner-scope-001"},
    )

    assert listed.status_code == 200
    assert listed.json() == {"items": []}
    assert read.status_code == 404
    assert sent.status_code == 404
    assert harness.knowledge_calls == []


def test_same_subject_in_another_org_cannot_read_or_send_global_knowledge_conversation() -> None:
    harness = _harness()
    conversation_id = _new_knowledge_conversation(harness)

    listed = harness.client.get(
        "/api/v1/knowledge/conversations", headers=_same_subject_other_org_auth()
    )
    read = harness.client.get(
        f"/api/v1/knowledge/conversations/{conversation_id}/messages",
        headers=_same_subject_other_org_auth(),
    )
    sent = harness.client.post(
        f"/api/v1/knowledge/conversations/{conversation_id}/messages",
        json={"content": "수출 신고 절차를 알려 주세요."},
        headers={
            **_same_subject_other_org_auth(),
            "Idempotency-Key": "knowledge-cross-org-owner-scope-001",
        },
    )

    assert listed.status_code == 200
    assert listed.json() == {"items": []}
    assert read.status_code == 404
    assert sent.status_code == 404
    assert harness.knowledge_calls == []
    assert (
        harness.repository.list_knowledge_messages("local-dev", "reviewer-1", conversation_id) == []
    )


@pytest.mark.parametrize(
    "source_uri",
    [
        "https://untrusted.example/guide",
        "https://www.customs.go.kr:/guide",
    ],
)
def test_global_knowledge_rejects_an_unsafe_citation_uri_before_persistence(
    source_uri: str,
) -> None:
    def untrusted_answer(request: AgentKnowledgeAnswerRequest) -> dict[str, Any]:
        as_of = request.as_of.isoformat()
        citation = {
            "kind": "guidance_evidence",
            "source_id": "source-unsafe-uri",
            "source_kind": "official_agency_guidance",
            "source_title": "공식 안내",
            "source_uri": source_uri,
            "as_of": as_of,
            "snapshot_id": "snapshot-safe",
            "coverage": "covered",
        }
        return {
            "schema_version": "1.0",
            "conversation_id": request.conversation_id,
            "answer_scope": "general_customs_guidance",
            "direct_answer": "공식 안내입니다.",
            "citations": [citation],
            "answer_sections": [
                {
                    "kind": "general_guidance",
                    "title": "공식 안내",
                    "content": "공식 안내입니다.",
                    "citations": [citation],
                }
            ],
            "suggested_questions": [],
            "coverage": "covered",
            "snapshot_id": "snapshot-safe",
            "as_of": as_of,
            "engine": {"kind": "deterministic", "model_id": "ingress-fixture"},
        }

    harness = _harness(knowledge_answer_handler=untrusted_answer)
    conversation_id = _new_knowledge_conversation(harness)
    rejected = harness.client.post(
        f"/api/v1/knowledge/conversations/{conversation_id}/messages",
        json={"content": "수출 신고 절차를 알려 주세요."},
        headers={**_auth(), "Idempotency-Key": "knowledge-uri-boundary-001"},
    )

    assert rejected.status_code == 409
    assert source_uri not in rejected.text
    assert len(harness.knowledge_calls) == 1
    assert (
        harness.repository.list_knowledge_messages("local-dev", "reviewer-1", conversation_id) == []
    )


def test_case_assistant_is_not_subject_to_global_knowledge_input_guard() -> None:
    harness = _harness()
    case = harness.client.post("/api/v1/cases", json={"title": "사건 심사"}, headers=_auth())
    assert case.status_code == 201
    case_id = case.json()["case_id"]
    document = harness.client.post(
        f"/api/v1/cases/{case_id}/documents",
        json={
            "document_type": "CI",
            "filename": "case-invoice.txt",
            "content_text": "Invoice No: INV-001",
        },
        headers=_auth(),
    )
    assert document.status_code == 201
    review = harness.client.post(
        f"/api/v1/cases/{case_id}/reviews",
        json={"review_as_of": "2026-08-05"},
        headers={**_auth(), "Idempotency-Key": "case-ingress-review-001"},
    )
    assert review.status_code == 202
    conversation = harness.client.post(
        f"/api/v1/review-runs/{review.json()['job']['review_run_id']}/assistant/conversations",
        json={},
        headers=_auth(),
    )
    assert conversation.status_code == 201
    response = harness.client.post(
        f"/api/v1/assistant/conversations/{conversation.json()['conversation_id']}/messages",
        json={"content": "Invoice No: INV-001\nQuantity: 2"},
        headers={**_auth(), "Idempotency-Key": "case-ingress-answer-001"},
    )

    assert response.status_code == 200, response.text
    assert len(harness.case_calls) == 1
    assert harness.knowledge_calls == []
