"""Approver-only LLM settings API and its direct Agent HTTP boundary."""

from __future__ import annotations

import json
from typing import Any

import httpx
import pytest
from fastapi.testclient import TestClient

from customs_backend.adapters import CallableAgentClient, HttpAgentClient
from customs_backend.admin_settings import (
    LlmSettingsChangeRequest,
    LlmSettingsUpdateRequest,
    decode_llm_settings_view,
)
from customs_backend.api import Container, create_app
from customs_backend.auth import StaticTokenVerifier
from customs_backend.domain import Principal, Role
from customs_backend.ports import AgentResponseRejectedError, LlmSettingsRejectedError
from customs_backend.repository import InMemoryWorkbenchRepository
from customs_backend.service import WorkbenchService
from customs_backend.settings import Settings
from tests.product.agent_v3_fixture import build_agent_v3_result
from tests.product.test_workbench_api import _approver_auth, _auth
from customs_backend.adapters import InMemoryObjectStore


def _client_with_agent(agent: CallableAgentClient) -> TestClient:
    settings = Settings(_env_file=None, environment="test")
    repository = InMemoryWorkbenchRepository()
    objects = InMemoryObjectStore()
    service = WorkbenchService(repository=repository, object_store=objects, agent=agent)
    return TestClient(
        create_app(
            settings=settings,
            container=Container(
                settings=settings,
                repository=repository,
                object_store=objects,
                agent=agent,
                token_verifier=StaticTokenVerifier(
                    {
                        "test-token": Principal(
                            subject="reviewer-1", org_id="local-dev", roles=frozenset({Role.reviewer})
                        ),
                        "approver-token": Principal(
                            subject="approver-1", org_id="local-dev", roles=frozenset({Role.approver})
                        ),
                    }
                ),
                service=service,
            ),
        )
    )


def _default_agent(**handlers: Any) -> CallableAgentClient:
    return CallableAgentClient(lambda request: build_agent_v3_result(request), **handlers)


def test_approver_reads_updates_tests_and_resets_llm_settings() -> None:
    received: dict[str, Any] = {}

    def update(request: dict[str, Any]) -> dict[str, Any]:
        received["update"] = request
        view = CallableAgentClient.default_llm_settings_view()
        view.update(request)
        view["source"] = "override"
        view["version"] = 7
        view["updated_at"] = "2026-09-09T01:02:03+00:00"
        return view

    def test(request: dict[str, Any]) -> dict[str, Any]:
        received["test"] = request
        return {"ok": True, "provider": "openai", "model": "gpt-5.6-luna", "latency_ms": 812, "error_code": None}

    def reset() -> dict[str, Any]:
        received["reset"] = True
        return CallableAgentClient.default_llm_settings_view()

    with _client_with_agent(
        _default_agent(update_llm_settings=update, test_llm_settings=test, reset_llm_settings=reset)
    ) as client:
        read = client.get("/api/v1/admin/llm-settings", headers=_approver_auth())
        updated = client.put(
            "/api/v1/admin/llm-settings",
            headers=_approver_auth(),
            json={"model": "  gpt-5.6-luna ", "max_tokens": 8192, "timeout_seconds": 120},
        )
        tested_active = client.post(
            "/api/v1/admin/llm-settings/test", headers=_approver_auth(), json={}
        )
        tested_candidate = client.post(
            "/api/v1/admin/llm-settings/test",
            headers=_approver_auth(),
            json={"provider": "openai", "model": "gpt-4.1-mini"},
        )
        reset_response = client.delete("/api/v1/admin/llm-settings", headers=_approver_auth())

    assert read.status_code == 200
    body = read.json()
    assert body["provider"] == "openai"
    assert body["model"] == "gpt-5.6-luna"
    assert body["adjustable"] is True
    assert body["updated_at"] is None  # nulls are kept, not dropped
    assert {item["provider"] for item in body["providers"]} == {"openai", "anthropic"}
    assert "api_key" not in json.dumps(body)

    assert updated.status_code == 200
    assert received["update"] == {"model": "gpt-5.6-luna", "max_tokens": 8192, "timeout_seconds": 120.0}
    assert updated.json()["source"] == "override"
    assert updated.json()["version"] == 7
    assert updated.json()["updated_at"] == "2026-09-09T01:02:03+00:00"

    assert tested_active.status_code == 200
    assert received["test"] == {"provider": "openai", "model": "gpt-4.1-mini"}
    assert tested_active.json() == {
        "ok": True,
        "provider": "openai",
        "model": "gpt-5.6-luna",
        "latency_ms": 812,
        "error_code": None,
    }
    assert tested_candidate.status_code == 200

    assert reset_response.status_code == 200
    assert received["reset"] is True
    assert reset_response.json()["source"] == "environment"


def test_reviewer_cannot_use_admin_llm_settings() -> None:
    with _client_with_agent(_default_agent()) as client:
        responses = [
            client.get("/api/v1/admin/llm-settings", headers=_auth()),
            client.put("/api/v1/admin/llm-settings", headers=_auth(), json={"model": "gpt-4.1"}),
            client.delete("/api/v1/admin/llm-settings", headers=_auth()),
            client.post("/api/v1/admin/llm-settings/test", headers=_auth(), json={}),
        ]
    assert [response.status_code for response in responses] == [403, 403, 403, 403]
    assert all(response.json()["code"] == "permission_denied" for response in responses)


def test_unauthenticated_admin_llm_settings_is_401() -> None:
    with _client_with_agent(_default_agent()) as client:
        response = client.get("/api/v1/admin/llm-settings")
    assert response.status_code == 401


@pytest.mark.parametrize(
    "body",
    [
        {},
        {"model": ""},
        {"model": "   "},
        {"max_tokens": 10},
        {"max_tokens": 100_000},
        {"timeout_seconds": 0},
        {"provider": "gemini"},
        {"model": "gpt-4.1", "api_key": "sk-x"},
        {"model": "x" * 129},
    ],
)
def test_update_rejects_invalid_bodies_before_reaching_the_agent(body: dict[str, Any]) -> None:
    calls = 0

    def update(_request: dict[str, Any]) -> dict[str, Any]:
        nonlocal calls
        calls += 1
        return CallableAgentClient.default_llm_settings_view()

    with _client_with_agent(_default_agent(update_llm_settings=update)) as client:
        response = client.put("/api/v1/admin/llm-settings", headers=_approver_auth(), json=body)

    assert response.status_code == 422
    assert response.json()["code"] == "request_validation_failed"
    assert calls == 0


@pytest.mark.parametrize(
    ("code", "status"),
    [
        ("llm_settings_invalid", 422),
        ("llm_credential_missing", 422),
        ("llm_settings_unavailable", 409),
    ],
)
def test_agent_rejection_codes_pass_through(code: str, status: int) -> None:
    def update(_request: dict[str, Any]) -> dict[str, Any]:
        raise LlmSettingsRejectedError(code, status=status)

    with _client_with_agent(_default_agent(update_llm_settings=update)) as client:
        response = client.put(
            "/api/v1/admin/llm-settings", headers=_approver_auth(), json={"model": "gpt-4.1"}
        )

    assert response.status_code == status
    assert response.json()["code"] == code
    assert response.json()["status"] == status


def test_malformed_agent_view_is_rejected_at_the_boundary() -> None:
    def view() -> dict[str, Any]:
        broken = CallableAgentClient.default_llm_settings_view()
        broken["provider"] = "gemini"
        return broken

    with _client_with_agent(_default_agent(llm_settings=view)) as client:
        response = client.get("/api/v1/admin/llm-settings", headers=_approver_auth())

    assert response.status_code == 503
    assert response.json()["code"] == "agent_unavailable"


def test_decoder_rejects_extra_keys_and_secret_looking_fields() -> None:
    raw = CallableAgentClient.default_llm_settings_view()
    raw["api_key"] = "sk-leak"
    with pytest.raises(AgentResponseRejectedError):
        decode_llm_settings_view(raw)


def test_request_models_normalise_input() -> None:
    change = LlmSettingsChangeRequest.model_validate({"model": " gpt-5.6-luna "})
    assert change.to_agent_wire() == {"model": "gpt-5.6-luna"}
    empty = LlmSettingsUpdateRequest.model_validate({})
    assert empty.is_empty() and empty.to_agent_wire() == {}
    with pytest.raises(ValueError):
        LlmSettingsChangeRequest.model_validate({})


# ---------------------------------------------------------------- HTTP boundary


def _http_client(handler) -> HttpAgentClient:
    return HttpAgentClient(
        base_url="http://127.0.0.1:8090",
        service_token="test-token",
        service_mode="functional-uat",
        transport=httpx.MockTransport(handler),
    )


def test_http_agent_client_llm_settings_wire_shape() -> None:
    seen: list[tuple[str, str, bytes]] = []
    view = CallableAgentClient.default_llm_settings_view()

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append((request.method, request.url.path, request.content))
        assert request.headers["Authorization"] == "Bearer test-token"
        assert request.headers["X-Customs-Service-Mode"] == "functional-uat"
        if request.method in ("PUT", "POST"):
            assert request.headers["Content-Type"] == "application/json"
        payload: dict[str, Any] = view
        if request.url.path.endswith("/test"):
            payload = {"ok": True, "provider": "openai", "model": "gpt-5.6-luna", "latency_ms": 5, "error_code": None}
        return httpx.Response(
            200, json=payload, headers={"X-Customs-Service-Mode": "functional-uat"}, request=request
        )

    client = _http_client(handler)
    try:
        assert client.llm_settings() == view
        assert client.update_llm_settings({"model": "gpt-5.6-luna"}) == view
        assert client.reset_llm_settings() == view
        assert client.test_llm_settings({})["ok"] is True
    finally:
        client.close()

    assert [(method, path) for method, path, _ in seen] == [
        ("GET", "/internal/agent/v3/llm-settings"),
        ("PUT", "/internal/agent/v3/llm-settings"),
        ("DELETE", "/internal/agent/v3/llm-settings"),
        ("POST", "/internal/agent/v3/llm-settings/test"),
    ]
    assert json.loads(seen[1][2]) == {"model": "gpt-5.6-luna"}
    assert json.loads(seen[3][2]) == {}


@pytest.mark.parametrize(
    ("status", "body", "expected_code", "expected_status"),
    [
        (422, {"error": {"code": "llm_credential_missing"}}, "llm_credential_missing", 422),
        (422, {"error": {"code": "Bad Code!"}}, "llm_settings_invalid", 422),
        (422, "not json", "llm_settings_invalid", 422),
        (409, {"error": {"code": "llm_settings_unavailable"}}, "llm_settings_unavailable", 409),
    ],
)
def test_http_agent_client_maps_refusals_to_stable_codes(
    status: int, body: Any, expected_code: str, expected_status: int
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        kwargs: dict[str, Any] = {"json": body} if isinstance(body, dict) else {"text": body}
        return httpx.Response(
            status, headers={"X-Customs-Service-Mode": "functional-uat"}, request=request, **kwargs
        )

    client = _http_client(handler)
    try:
        with pytest.raises(LlmSettingsRejectedError) as error:
            client.update_llm_settings({"provider": "anthropic"})
    finally:
        client.close()
    assert error.value.code == expected_code
    assert error.value.status == expected_status


@pytest.mark.parametrize("status", [500, 503, 404])
def test_http_agent_client_other_failures_are_boundary_rejections(status: int) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            status,
            json={"error": {"code": "internal_error"}},
            headers={"X-Customs-Service-Mode": "functional-uat"},
            request=request,
        )

    client = _http_client(handler)
    try:
        with pytest.raises(AgentResponseRejectedError):
            client.llm_settings()
    finally:
        client.close()


def test_http_agent_client_rejects_service_mode_mismatch() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json=CallableAgentClient.default_llm_settings_view(),
            headers={"X-Customs-Service-Mode": "local-development"},
            request=request,
        )

    client = _http_client(handler)
    try:
        with pytest.raises(AgentResponseRejectedError):
            client.llm_settings()
    finally:
        client.close()
