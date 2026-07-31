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
from customs_backend.ports import (
    AgentPreExecutionRetryableError,
    AgentResponseRejectedError,
)
from customs_backend.release import load_agent_contract_lock


_AGENT_REQUEST_ID = "8a5dc01d-41f5-4e56-b9ba-a305f493af2a"
_AGENT_SERVICE_OPENAPI_SHA256 = "e0ac396417ddc37a20fce7ed9ba612db3db5d259a3a3393faadfb1ad95f6c64e"


def _review_request() -> AgentReviewRequest:
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
        review_as_of=date(2026, 7, 22),
        assertion_cutoff_at=datetime(2026, 7, 22, tzinfo=UTC),
        trade_type="import",
    )


def _dependency_unready_payload() -> dict:
    return {
        "error": {
            "code": "dependency_unready",
            "message": "A required service dependency is not ready.",
            "request_id": _AGENT_REQUEST_ID,
            "retryable": True,
        }
    }


def test_backend_pins_the_agent_pre_execution_header_openapi_release() -> None:
    assert load_agent_contract_lock().service_openapi_sha256 == _AGENT_SERVICE_OPENAPI_SHA256


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
    request = _review_request()
    try:
        assert client.review(request) == {"accepted": True}
    finally:
        client.close()
    assert captured["llm_mode"] == "llm"


def test_http_agent_client_recognizes_only_proven_pre_execution_dependency_rejection() -> None:
    def reject_before_execution(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            503,
            json=_dependency_unready_payload(),
            headers={
                "X-Request-ID": _AGENT_REQUEST_ID,
                "X-Agent-Execution-State": "not-started",
            },
            request=request,
        )

    client = HttpAgentClient(
        base_url="http://agent.test",
        service_token="test-token",
        transport=httpx.MockTransport(reject_before_execution),
    )
    try:
        with pytest.raises(AgentPreExecutionRetryableError) as captured:
            client.review(_review_request())
    finally:
        client.close()

    assert captured.value.request_id == _AGENT_REQUEST_ID
    assert captured.value.error_code == "dependency_unready"


def test_http_agent_v3_review_stream_uses_exact_ndjson_wire_and_forwards_events() -> None:
    observed_events: list[dict] = []
    stage = {"sequence": 0, "stage_code": "request_validation"}
    stage_terminal = {
        "sequence": 1,
        "stage_code": "completed",
        "state": "completed",
    }
    terminal = {"envelope_sha256": "a" * 64}

    def provider_boundary(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/internal/agent/v3/reviews/stream"
        assert request.headers["Authorization"] == "Bearer test-token"
        assert request.headers["Accept"] == "application/x-ndjson"
        payload = json.loads(request.content)
        assert "image_pages" not in payload["documents"][0]
        content = (
            json.dumps({"type": "stage_event", "event": stage})
            + "\n"
            + json.dumps({"type": "stage_event", "event": stage_terminal})
            + "\n"
            + json.dumps({"type": "result", "result": terminal})
            + "\n"
        )
        return httpx.Response(
            200,
            content=content.encode(),
            headers={"Content-Type": "application/x-ndjson"},
            request=request,
        )

    client = HttpAgentClient(
        base_url="http://agent.test",
        service_token="test-token",
        transport=httpx.MockTransport(provider_boundary),
    )
    try:
        assert (
            client.review_experience_stream(
                _review_request(),
                on_event=observed_events.append,
            )
            == terminal
        )
    finally:
        client.close()
    assert observed_events == [stage, stage_terminal]


def test_http_agent_v3_stream_binds_failed_stage_terminal_to_one_error() -> None:
    observed_events: list[dict] = []
    failed_terminal = {
        "sequence": 0,
        "stage_code": "failed",
        "state": "failed",
    }
    content = (
        json.dumps({"type": "stage_event", "event": failed_terminal})
        + "\n"
        + json.dumps(
            {
                "type": "error",
                "error": {"code": "internal_error", "retryable": False},
            }
        )
        + "\n"
    )
    client = HttpAgentClient(
        base_url="http://agent.test",
        service_token="test-token",
        transport=httpx.MockTransport(
            lambda request: httpx.Response(
                200,
                content=content.encode(),
                headers={"Content-Type": "application/x-ndjson"},
                request=request,
            )
        ),
    )
    try:
        with pytest.raises(AgentResponseRejectedError, match="failed with internal_error"):
            client.review_experience_stream(
                _review_request(),
                on_event=observed_events.append,
            )
    finally:
        client.close()
    assert observed_events == [failed_terminal]


def test_http_agent_v3_stream_recognizes_only_proven_pre_execution_rejection() -> None:
    def reject_before_execution(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            503,
            json=_dependency_unready_payload(),
            headers={
                "X-Request-ID": _AGENT_REQUEST_ID,
                "X-Agent-Execution-State": "not-started",
            },
            request=request,
        )

    client = HttpAgentClient(
        base_url="http://agent.test",
        service_token="test-token",
        transport=httpx.MockTransport(reject_before_execution),
    )
    try:
        with pytest.raises(AgentPreExecutionRetryableError) as captured:
            client.review_experience_stream(
                _review_request(),
                on_event=lambda _event: None,
            )
    finally:
        client.close()
    assert captured.value.request_id == _AGENT_REQUEST_ID


@pytest.mark.parametrize(
    "content",
    [
        '{"type":"result","result":{}}\n{"type":"stage_event","event":{}}\n',
        (
            '{"type":"stage_event","event":{"stage_code":"completed","state":"completed"}}\n'
            '{"type":"error","error":{"code":"internal_error","retryable":false}}\n'
        ),
        (
            '{"type":"stage_event","event":{"stage_code":"failed","state":"failed"}}\n'
            '{"type":"result","result":{}}\n'
        ),
        (
            '{"type":"stage_event","event":{"stage_code":"completed","state":"completed"}}\n'
            '{"type":"stage_event","event":{"stage_code":"completed","state":"completed"}}\n'
            '{"type":"result","result":{}}\n'
        ),
        (
            '{"type":"stage_event","event":{"stage_code":"completed","state":"completed"}}\n'
            '{"type":"stage_event","event":{"stage_code":"result_validation",'
            '"state":"completed"}}\n'
            '{"type":"result","result":{}}\n'
        ),
        '{"type":"stage_event","event":{}}\n',
        '{"type":"stage_event","event":{},"extra":true}\n',
        '{"type":"result","result":NaN}\n',
        '{"type":"result","result":{},"result":{}}\n',
    ],
)
def test_http_agent_v3_stream_rejects_ambiguous_or_incomplete_ndjson(content: str) -> None:
    client = HttpAgentClient(
        base_url="http://agent.test",
        service_token="test-token",
        transport=httpx.MockTransport(
            lambda request: httpx.Response(
                200,
                content=content.encode(),
                headers={"Content-Type": "application/x-ndjson"},
                request=request,
            )
        ),
    )
    try:
        with pytest.raises(AgentResponseRejectedError):
            client.review_experience_stream(
                _review_request(),
                on_event=lambda _event: None,
            )
    finally:
        client.close()


@pytest.mark.parametrize(
    "case",
    [
        "status-502",
        "status-504",
        "wrong-code",
        "retryable-false",
        "retryable-string",
        "request-id-missing",
        "request-id-mismatch",
        "request-id-duplicate",
        "request-id-invalid",
        "execution-state-missing",
        "execution-state-wrong",
        "execution-state-duplicate",
        "root-extra",
        "error-extra",
        "message-wrong",
        "message-empty",
        "message-too-long",
        "wrong-content-type",
        "duplicate-json-key",
    ],
)
def test_http_agent_client_rejects_unproven_503_without_reopening_call_once(
    case: str,
) -> None:
    def unsafe_response(request: httpx.Request) -> httpx.Response:
        status = 503
        payload = _dependency_unready_payload()
        headers: list[tuple[str, str]] = [
            ("Content-Type", "application/json"),
            ("X-Request-ID", _AGENT_REQUEST_ID),
            ("X-Agent-Execution-State", "not-started"),
        ]
        content: bytes | None = None
        if case == "status-502":
            status = 502
        elif case == "status-504":
            status = 504
        elif case == "wrong-code":
            payload["error"]["code"] = "service_busy"
        elif case == "retryable-false":
            payload["error"]["retryable"] = False
        elif case == "retryable-string":
            payload["error"]["retryable"] = "true"
        elif case == "request-id-missing":
            headers = [item for item in headers if item[0] != "X-Request-ID"]
        elif case == "request-id-mismatch":
            headers[1] = ("X-Request-ID", "54f62934-21be-4dc4-9c40-2fd30edb4332")
        elif case == "request-id-duplicate":
            headers.append(("X-Request-ID", _AGENT_REQUEST_ID))
        elif case == "request-id-invalid":
            payload["error"]["request_id"] = "NOT-A-UUID"
            headers[1] = ("X-Request-ID", "NOT-A-UUID")
        elif case == "execution-state-missing":
            headers = [item for item in headers if item[0] != "X-Agent-Execution-State"]
        elif case == "execution-state-wrong":
            headers[2] = ("X-Agent-Execution-State", "started")
        elif case == "execution-state-duplicate":
            headers.append(("X-Agent-Execution-State", "not-started"))
        elif case == "root-extra":
            payload["unexpected"] = True
        elif case == "error-extra":
            payload["error"]["unexpected"] = True
        elif case == "message-wrong":
            payload["error"]["message"] = "Temporarily unavailable."
        elif case == "message-empty":
            payload["error"]["message"] = ""
        elif case == "message-too-long":
            payload["error"]["message"] = "x" * 257
        elif case == "wrong-content-type":
            headers[0] = ("Content-Type", "text/plain")
        elif case == "duplicate-json-key":
            content = (
                b'{"error":{"code":"dependency_unready","code":"dependency_unready",'
                b'"message":"not ready","request_id":"'
                + _AGENT_REQUEST_ID.encode()
                + b'","retryable":true}}'
            )
        return httpx.Response(
            status,
            json=payload if content is None else None,
            content=content,
            headers=headers,
            request=request,
        )

    client = HttpAgentClient(
        base_url="http://agent.test",
        service_token="test-token",
        transport=httpx.MockTransport(unsafe_response),
    )
    try:
        with pytest.raises(AgentResponseRejectedError):
            client.review(_review_request())
    finally:
        client.close()


def test_metadata_503_never_uses_review_pre_execution_retry_classification() -> None:
    client = HttpAgentClient(
        base_url="http://agent.test",
        service_token="test-token",
        transport=httpx.MockTransport(
            lambda request: httpx.Response(
                503,
                json=_dependency_unready_payload(),
                headers={
                    "X-Request-ID": _AGENT_REQUEST_ID,
                    "X-Agent-Execution-State": "not-started",
                },
                request=request,
            )
        ),
    )
    try:
        with pytest.raises(AgentResponseRejectedError, match="HTTP 503"):
            client.metadata()
    finally:
        client.close()


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
