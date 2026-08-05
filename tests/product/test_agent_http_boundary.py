from __future__ import annotations

import json
from datetime import date

import httpx
import pytest

from customs_backend.adapters import HttpAgentClient
from customs_backend.agent_contract import validate_agent_v3_payload
from customs_backend.domain import AgentDocumentInput, AgentReviewRequest
from customs_backend.ports import AgentResponseRejectedError


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
