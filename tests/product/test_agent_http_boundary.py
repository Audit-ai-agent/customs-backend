from __future__ import annotations

import json
from datetime import UTC, date, datetime

import httpx
import pytest
from pydantic import ValidationError
from tests.product.agent_v3_fixture import build_agent_v3_result

from customs_backend.adapters import HttpAgentClient
from customs_backend.domain import (
    AgentDocumentInput,
    AgentHumanDecision,
    AgentReviewRequest,
    AgentRevisionRequest,
    canonical_digest,
)
from customs_backend.ports import AgentResponseRejectedError


@pytest.mark.parametrize(
    ("content", "content_type"),
    [
        (b'{"status":"ready","status":"forged"}', "application/json"),
        (b'{"status":NaN}', "application/json"),
        (b'[{"status":"ready"}]', "application/json"),
        (b'{"status":"ready"}', "text/plain"),
    ],
)
def test_agent_metadata_rejects_noncanonical_or_wrongly_typed_json(
    content: bytes,
    content_type: str,
) -> None:
    transport = httpx.MockTransport(
        lambda request: httpx.Response(
            200,
            content=content,
            headers={"Content-Type": content_type},
            request=request,
        )
    )
    client = HttpAgentClient(
        base_url="http://agent.test",
        service_token="test-token",
        transport=transport,
    )
    try:
        with pytest.raises(RuntimeError):
            client.metadata()
    finally:
        client.close()


def test_agent_metadata_accepts_canonical_json_object() -> None:
    transport = httpx.MockTransport(
        lambda request: httpx.Response(
            200,
            json={"status": "ready", "schema_version": "3.0"},
            request=request,
        )
    )
    client = HttpAgentClient(
        base_url="http://agent.test",
        service_token="test-token",
        transport=transport,
    )
    try:
        assert client.metadata() == {"status": "ready", "schema_version": "3.0"}
    finally:
        client.close()


def test_agent_http_status_and_oversized_stream_are_definite_response_rejections() -> None:
    status_client = HttpAgentClient(
        base_url="http://agent.test",
        service_token="test-token",
        transport=httpx.MockTransport(
            lambda request: httpx.Response(422, json={"error": "rejected"}, request=request)
        ),
    )
    try:
        with pytest.raises(AgentResponseRejectedError, match="HTTP 422"):
            status_client.metadata()
    finally:
        status_client.close()

    response = httpx.Response(
        200,
        headers={"content-type": "application/json"},
        content=b'{"value":"0123456789"}',
    )
    with pytest.raises(AgentResponseRejectedError, match="safety limit"):
        HttpAgentClient._decode_object(
            response,
            maximum_bytes=8,
            response_name="test response",
        )


def test_http_agent_client_omits_unreleased_visual_keys_at_the_real_wire_boundary() -> None:
    captured: dict = {}

    def provider_boundary(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content)
        captured.update(payload)
        assert request.url.path == "/internal/agent/v2/reviews"
        assert request.headers["Authorization"] == "Bearer test-token"
        assert len(payload["documents"]) == 1
        assert "image_pages" not in payload["documents"][0]
        assert "pdf_pages" not in payload["documents"][0]
        return httpx.Response(
            200,
            json={"accepted": True},
            request=request,
        )

    transport = httpx.MockTransport(provider_boundary)
    client = HttpAgentClient(
        base_url="http://agent.test",
        service_token="test-token",
        transport=transport,
    )
    request = AgentReviewRequest(
        review_run_id="run-boundary",
        case_id="case-boundary",
        documents=[
            AgentDocumentInput(
                document_id="document-boundary",
                filename="invoice.txt",
                content_text="COMMERCIAL INVOICE",
            )
        ],
        review_as_of=date(2026, 7, 22),
        assertion_cutoff_at=datetime(2026, 7, 22, tzinfo=UTC),
        trade_type="import",
    )
    try:
        assert client.review(request) == {"accepted": True}
    finally:
        client.close()
    assert captured["llm_mode"] == "llm"


def test_backend_agent_request_model_rejects_seven_primary_documents() -> None:
    documents = [
        AgentDocumentInput(
            document_id=f"document-{index}",
            filename=f"document-{index}.txt",
            content_text="text",
        )
        for index in range(7)
    ]
    with pytest.raises(ValidationError):
        AgentReviewRequest(
            review_run_id="run-too-many-documents",
            case_id="case-too-many-documents",
            documents=documents,
            review_as_of=date(2026, 7, 22),
            assertion_cutoff_at=datetime(2026, 7, 22, tzinfo=UTC),
            trade_type="import",
        )


def test_http_revision_wire_contains_hash_bound_parent_and_omits_visual_keys() -> None:
    captured: dict = {}

    def provider_boundary(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content)
        captured.update(payload)
        assert request.url.path == "/internal/agent/v2/revisions"
        for nested_name in ("parent_request", "revised_request"):
            assert nested_name in payload
            assert "image_pages" not in payload[nested_name]["documents"][0]
            assert "pdf_pages" not in payload[nested_name]["documents"][0]
        return httpx.Response(200, json={"accepted": True}, request=request)

    parent = AgentReviewRequest(
        review_run_id="run-parent-boundary",
        case_id="case-boundary",
        documents=[
            AgentDocumentInput(
                document_id="document-boundary",
                filename="invoice.txt",
                content_text="COMMERCIAL INVOICE",
                declared_type="invoice",
            )
        ],
        review_as_of=date(2026, 7, 22),
        assertion_cutoff_at=datetime(2026, 7, 22, tzinfo=UTC),
        trade_type="import",
    )
    parent_result = build_agent_v3_result(parent)
    revised = parent.model_copy(update={"review_run_id": "run-child-boundary"})
    decision_payload = {
        "decision": "correct_field",
        "actor_id": "approver-1",
        "actor_role": "approver",
        "decided_at": "2026-07-22T00:00:00Z",
        "reason": "source confirmed",
        "confirmed_reference_id": None,
        "correction": {
            "document_id": "document-boundary",
            "field_path": "line_items.0.quantity",
            "value": "1",
            "confidence": 1.0,
        },
        "supporting_document": None,
    }
    decision = AgentHumanDecision(
        decision_id=f"decision_{canonical_digest(decision_payload)}",
        **decision_payload,
    )
    revision = AgentRevisionRequest(
        parent_result_sha256=parent_result["result_sha256"],
        parent_input_manifest_sha256=parent_result["input_manifest_sha256"],
        parent_result=parent_result,
        parent_request=parent,
        revised_request=revised,
        decisions=[decision],
    )
    client = HttpAgentClient(
        base_url="http://agent.test",
        service_token="test-token",
        transport=httpx.MockTransport(provider_boundary),
    )
    try:
        assert client.revise(revision) == {"accepted": True}
    finally:
        client.close()
    assert captured["parent_input_manifest_sha256"] == parent.input_manifest_sha256
    assert captured["parent_input_manifest_sha256"] != canonical_digest(captured["parent_request"])
