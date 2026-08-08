from __future__ import annotations

import json
from datetime import date

import httpx
import pytest

from customs_backend.adapters import HttpAgentClient
from customs_backend.agent_contract import validate_agent_v3_payload
from customs_backend.domain import (
    AgentAssistantAnswerRequest,
    AgentDocumentInput,
    AgentKnowledgeAnswerRequest,
    AgentReviewRequest,
)
from customs_backend.ports import AgentResponseRejectedError, KnowledgeInputRejectedError
from tests.product.agent_v3_fixture import build_agent_v3_result


def _request() -> AgentReviewRequest:
    return AgentReviewRequest(
        review_run_id="run-boundary",
        case_id="case-boundary",
        documents=[
            AgentDocumentInput(
                document_id="document-boundary",
                filename="invoice.txt",
                content_text="COMMERCIAL INVOICE",
            )
        ],
        review_as_of=date(2026, 8, 5),
        trade_type="import",
    )


def test_packaged_agent_v3_request_schema_accepts_backend_projection() -> None:
    request = _request()

    validate_agent_v3_payload("review_request", request.to_agent_wire())


def test_packaged_assistant_schemas_accept_the_bounded_backend_projection() -> None:
    review = _request()
    request = AgentAssistantAnswerRequest(
        case_id=review.case_id,
        review_run_id=review.review_run_id,
        conversation_id="conversation-boundary",
        review_result=build_agent_v3_result(review),
        question="What should be reviewed?",
        recent_turns=[],
    )
    validate_agent_v3_payload("assistant_answer_request", request.to_agent_wire())
    validate_agent_v3_payload(
        "assistant_answer_response",
        {
            "schema_version": "1.0",
            "case_id": review.case_id,
            "review_run_id": review.review_run_id,
            "conversation_id": "conversation-boundary",
            "answer_scope": "case_grounded",
            "direct_answer": "Review the documented facts first.",
            "citations": [],
            "suggested_questions": [],
            "correction_draft": None,
            "engine": {"kind": "deterministic", "model_id": "fixture"},
        },
    )


def test_packaged_knowledge_schemas_accept_a_snapshot_grounded_projection() -> None:
    request = AgentKnowledgeAnswerRequest(
        conversation_id="knowledge-boundary",
        question="수출 신고 전에 무엇을 확인해야 하나요?",
        trade_type="export",
        as_of=date(2026, 8, 5),
        recent_turns=[],
    )
    citation = {
        "kind": "guidance_evidence",
        "source_id": "official-export-guide-2026",
        "source_kind": "official_law",
        "source_title": "관세청 수출 통관 안내",
        "source_uri": "https://www.customs.go.kr/guide/export",
        "as_of": "2026-08-05",
        "snapshot_id": "kr-trade-2026-08-05",
        "coverage": "covered",
    }
    validate_agent_v3_payload("knowledge_answer_request", request.to_agent_wire())
    validate_agent_v3_payload(
        "knowledge_answer_response",
        {
            "schema_version": "1.0",
            "conversation_id": request.conversation_id,
            "answer_scope": "general_customs_guidance",
            "direct_answer": "수출 신고 전에는 필요한 증빙을 확인해야 합니다.",
            "citations": [citation],
            "answer_sections": [
                {
                    "kind": "general_guidance",
                    "title": "공식 수출 통관 안내",
                    "content": "수출 신고 전에는 필요한 증빙을 확인해야 합니다.",
                    "citations": [citation],
                }
            ],
            "suggested_questions": [],
            "coverage": "covered",
            "snapshot_id": "kr-trade-2026-08-05",
            "as_of": "2026-08-05",
            "engine": {"kind": "deterministic", "model_id": "fixture"},
        },
    )


def test_packaged_assistant_and_knowledge_schemas_accept_every_registry_source_kind() -> None:
    """The consumer contract must not collapse official registry categories.

    Data distinguishes official law text from its official catalog, agency
    guidance, requirement references, and public guidance.  The wire contract
    carries that distinction even though Functional-UAT later rejects the one
    deterministic development-only kind.
    """

    source_kinds = (
        "official_law",
        "official_law_catalog",
        "official_agency_guidance",
        "official_requirement_reference",
        "official_public_guidance",
        "development_fixture",
    )
    for source_kind in source_kinds:
        citation = {
            "kind": "guidance_evidence",
            "source_id": "guide-registry-source",
            "source_kind": source_kind,
            "source_title": "공식 통관 안내",
            "source_uri": "https://www.customs.go.kr/guide",
            "as_of": "2026-08-05",
            "snapshot_id": "kr-trade-2026-08-05",
            "coverage": "covered",
        }
        validate_agent_v3_payload(
            "assistant_answer_response",
            {
                "schema_version": "1.0",
                "case_id": "case-boundary",
                "review_run_id": "run-boundary",
                "conversation_id": "conversation-boundary",
                "answer_scope": "general_customs_guidance",
                "direct_answer": "공식 안내를 확인해야 합니다.",
                "citations": [citation],
                "answer_sections": [
                    {
                        "kind": "general_guidance",
                        "title": "공식 안내",
                        "content": "공식 안내를 확인해야 합니다.",
                        "citations": [citation],
                    }
                ],
                "suggested_questions": [],
                "correction_draft": None,
                "engine": {"kind": "deterministic", "model_id": "fixture"},
            },
        )
        validate_agent_v3_payload(
            "knowledge_answer_response",
            {
                "schema_version": "1.0",
                "conversation_id": "knowledge-boundary",
                "answer_scope": "general_customs_guidance",
                "direct_answer": "공식 안내를 확인해야 합니다.",
                "citations": [citation],
                "answer_sections": [
                    {
                        "kind": "general_guidance",
                        "title": "공식 안내",
                        "content": "공식 안내를 확인해야 합니다.",
                        "citations": [citation],
                    }
                ],
                "suggested_questions": [],
                "coverage": "covered",
                "snapshot_id": "kr-trade-2026-08-05",
                "as_of": "2026-08-05",
                "engine": {"kind": "deterministic", "model_id": "fixture"},
            },
        )


def test_review_uses_the_one_bearer_and_mode_bound_http_endpoint() -> None:
    seen: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content)
        seen.update(payload)
        assert request.method == "POST"
        assert request.url.path == "/internal/agent/v2/reviews"
        assert request.headers["Authorization"] == "Bearer test-token"
        assert request.headers["X-Customs-Service-Mode"] == "local-development"
        assert "image_pages" not in payload["documents"][0]
        assert "pdf_pages" not in payload["documents"][0]
        return httpx.Response(
            200,
            json={"accepted": True},
            headers={"X-Customs-Service-Mode": "local-development"},
            request=request,
        )

    client = HttpAgentClient(
        base_url="http://127.0.0.1:8090",
        service_token="test-token",
        service_mode="local-development",
        transport=httpx.MockTransport(handler),
    )
    try:
        assert client.review(_request()) == {"accepted": True}
    finally:
        client.close()

    assert set(seen) == {
        "case_id",
        "documents",
        "jurisdiction",
        "review_as_of",
        "review_run_id",
        "trade_type",
    }


def test_assistant_uses_the_same_bearer_and_mode_bound_http_endpoint() -> None:
    review = _request()
    assistant = AgentAssistantAnswerRequest(
        case_id=review.case_id,
        review_run_id=review.review_run_id,
        conversation_id="conversation-boundary",
        review_result=build_agent_v3_result(review),
        question="What should be reviewed?",
        recent_turns=[],
    )

    def handler(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content)
        assert request.method == "POST"
        assert request.url.path == "/internal/agent/v2/assistant/answers"
        assert request.headers["Authorization"] == "Bearer test-token"
        assert request.headers["X-Customs-Service-Mode"] == "local-development"
        assert payload["question"] == "What should be reviewed?"
        return httpx.Response(
            200,
            json={
                "schema_version": "1.0",
                "case_id": review.case_id,
                "review_run_id": review.review_run_id,
                "conversation_id": "conversation-boundary",
                "answer_scope": "case_grounded",
                "direct_answer": "Review the documented facts first.",
                "citations": [],
                "suggested_questions": [],
                "correction_draft": None,
                "engine": {"kind": "deterministic", "model_id": "fixture"},
            },
            headers={"X-Customs-Service-Mode": "local-development"},
            request=request,
        )

    client = HttpAgentClient(
        base_url="http://127.0.0.1:8090",
        service_token="test-token",
        service_mode="local-development",
        transport=httpx.MockTransport(handler),
    )
    try:
        assert client.answer(assistant)["answer_scope"] == "case_grounded"
    finally:
        client.close()


def test_knowledge_assistant_uses_the_dedicated_mode_bound_http_endpoint() -> None:
    knowledge = AgentKnowledgeAnswerRequest(
        conversation_id="knowledge-boundary",
        question="수출 신고 전에 무엇을 확인해야 하나요?",
        trade_type="export",
        as_of=date(2026, 8, 5),
        recent_turns=[],
    )

    def handler(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content)
        assert request.method == "POST"
        assert request.url.path == "/internal/agent/v2/knowledge/answers"
        assert request.headers["Authorization"] == "Bearer test-token"
        assert request.headers["X-Customs-Service-Mode"] == "local-development"
        assert payload["trade_type"] == "export"
        assert "case_id" not in payload
        return httpx.Response(
            200,
            json={"accepted": True},
            headers={"X-Customs-Service-Mode": "local-development"},
            request=request,
        )

    client = HttpAgentClient(
        base_url="http://127.0.0.1:8090",
        service_token="test-token",
        service_mode="local-development",
        transport=httpx.MockTransport(handler),
    )
    try:
        assert client.answer_knowledge(knowledge) == {"accepted": True}
    finally:
        client.close()


def test_knowledge_assistant_preserves_a_safe_agent_input_boundary_without_body_echo() -> None:
    knowledge = AgentKnowledgeAnswerRequest(
        conversation_id="knowledge-boundary",
        question="일반 통관 안내를 알려 주세요.",
        trade_type="both",
        as_of=date(2026, 8, 5),
        recent_turns=[],
    )

    client = HttpAgentClient(
        base_url="http://127.0.0.1:8090",
        service_token="test-token",
        service_mode="local-development",
        transport=httpx.MockTransport(
            lambda request: httpx.Response(
                422,
                json={"code": "knowledge_input_boundary", "detail": "must not surface"},
                headers={"X-Customs-Service-Mode": "local-development"},
                request=request,
            )
        ),
    )
    try:
        with pytest.raises(KnowledgeInputRejectedError) as rejected:
            client.answer_knowledge(knowledge)
    finally:
        client.close()

    assert "must not surface" not in str(rejected.value)


def test_metadata_requires_the_same_mode_in_both_directions() -> None:
    accepted = HttpAgentClient(
        base_url="http://127.0.0.1:8090",
        service_token="test-token",
        service_mode="functional-uat",
        transport=httpx.MockTransport(
            lambda request: httpx.Response(
                200,
                json={"status": "ready"},
                headers={"X-Customs-Service-Mode": "functional-uat"},
                request=request,
            )
        ),
    )
    try:
        assert accepted.metadata() == {"status": "ready"}
    finally:
        accepted.close()

    rejected = HttpAgentClient(
        base_url="http://127.0.0.1:8090",
        service_token="test-token",
        service_mode="functional-uat",
        transport=httpx.MockTransport(
            lambda request: httpx.Response(200, json={"status": "ready"}, request=request)
        ),
    )
    try:
        with pytest.raises(AgentResponseRejectedError, match="mode"):
            rejected.metadata()
    finally:
        rejected.close()


def test_non_successful_review_is_rejected_without_a_retry_protocol() -> None:
    client = HttpAgentClient(
        base_url="http://127.0.0.1:8090",
        service_token="test-token",
        service_mode="local-development",
        transport=httpx.MockTransport(
            lambda request: httpx.Response(
                503,
                json={"error": "unavailable"},
                headers={"X-Customs-Service-Mode": "local-development"},
                request=request,
            )
        ),
    )
    try:
        with pytest.raises(AgentResponseRejectedError, match="HTTP 503"):
            client.review(_request())
    finally:
        client.close()


def test_review_trace_records_success_body_but_never_transport_headers() -> None:
    events: list[tuple[str, str, dict[str, object]]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={"accepted": True},
            headers={"X-Customs-Service-Mode": "functional-uat"},
            request=request,
        )

    client = HttpAgentClient(
        base_url="http://127.0.0.1:8090",
        service_token="live-service-token-value",
        service_mode="functional-uat",
        review_trace=lambda run_id, event, payload: events.append((run_id, event, payload)),
        transport=httpx.MockTransport(handler),
    )
    try:
        assert client.review(_request()) == {"accepted": True}
    finally:
        client.close()

    assert [event for _run_id, event, _payload in events] == ["review_request", "review_response"]
    assert all(run_id == "run-boundary" for run_id, _event, _payload in events)
    assert events[0][2]["path"] == "/internal/agent/v2/reviews"
    assert events[1][2]["response_body"] == {"accepted": True}
    assert "authorization" not in events[0][2]
    assert "live-service-token-value" not in json.dumps(events)


def test_review_trace_never_records_non_success_response_body() -> None:
    events: list[tuple[str, str, dict[str, object]]] = []
    client = HttpAgentClient(
        base_url="http://127.0.0.1:8090",
        service_token="test-token",
        service_mode="local-development",
        review_trace=lambda run_id, event, payload: events.append((run_id, event, payload)),
        transport=httpx.MockTransport(
            lambda request: httpx.Response(
                503,
                json={"provider_error": "raw secret failure response"},
                headers={"X-Customs-Service-Mode": "local-development"},
                request=request,
            )
        ),
    )
    try:
        with pytest.raises(AgentResponseRejectedError, match="HTTP 503"):
            client.review(_request())
    finally:
        client.close()

    assert [event for _run_id, event, _payload in events] == ["review_request", "review_failure"]
    failure = events[1][2]
    assert failure["response_status"] == 503
    assert "response_body" not in failure
    assert "raw secret failure response" not in json.dumps(events)
