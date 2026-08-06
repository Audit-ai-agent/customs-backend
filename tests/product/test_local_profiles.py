from __future__ import annotations

import sys
from datetime import date
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient

from customs_backend.adapters import CallableAgentClient, HttpAgentClient, InMemoryObjectStore
from customs_backend.api import Container, build_container, create_app
from customs_backend.auth import StaticTokenVerifier
from customs_backend.domain import Principal, Role
from customs_backend.repository import InMemoryWorkbenchRepository
from customs_backend.service import WorkbenchService
from customs_backend.settings import Settings
from customs_backend import local_profile
from customs_backend.local_profile import _functional_uat
from tests.product.agent_v3_fixture import build_agent_v3_result


def _development_settings(**overrides: object) -> Settings:
    values: dict[str, object] = {
        "environment": "local-development",
        "public_base_url": "http://127.0.0.1:18081",
        "agent_base_url": "http://127.0.0.1:18090",
        "agent_service_token": "a" * 32,
        "dev_reviewer_session_token": "d" * 32,
        "dev_approver_session_token": "p" * 32,
    }
    values.update(overrides)
    return Settings(_env_file=None, **values)


def _development_client(*, client_host: str) -> TestClient:
    settings = _development_settings()
    reviewer = Principal(
        subject=settings.dev_reviewer_subject,
        org_id=settings.dev_session_org_id,
        roles=frozenset({Role.reviewer}),
    )
    approver = Principal(
        subject=settings.dev_approver_subject,
        org_id=settings.dev_session_org_id,
        roles=frozenset({Role.approver}),
    )
    repository = InMemoryWorkbenchRepository()
    objects = InMemoryObjectStore()
    service = WorkbenchService(
        repository=repository,
        object_store=objects,
        agent=CallableAgentClient(lambda _request: {}),
    )
    return TestClient(
        create_app(
            container=Container(
                settings=settings,
                repository=repository,
                object_store=objects,
                agent=service.agent,
                token_verifier=StaticTokenVerifier({"d" * 32: reviewer, "p" * 32: approver}),
                service=service,
            ),
            settings=settings,
        ),
        client=(client_host, 50_000),
    )


def test_local_development_session_is_loopback_only_and_memory_token_based() -> None:
    with _development_client(client_host="127.0.0.1") as client:
        response = client.post("/api/v1/dev/session")
        assert response.status_code == 200
        assert response.headers["x-customs-service-mode"] == "local-development"
        payload = response.json()
        assert payload["token_type"] == "Bearer"
        assert payload["session"]["subject"] == "local-reviewer"
        assert payload["session"]["roles"] == ["reviewer"]
        session = client.get(
            "/api/v1/session",
            headers={"Authorization": f"Bearer {payload['access_token']}"},
        )
        assert session.status_code == 200
        assert client.get("/api/v1/auth/config").status_code == 404
        assert client.get("/api/v2/jobs/not-a-local-api").status_code == 404
        approver = client.post("/api/v1/dev/session", json={"actor": "approver"})
        assert approver.status_code == 200
        assert approver.json()["session"]["subject"] == "local-approver"
        assert approver.json()["session"]["roles"] == ["approver"]
    with _development_client(client_host="192.0.2.10") as client:
        assert client.post("/api/v1/dev/session").status_code == 404


def test_local_profile_container_binds_distinct_reviewer_and_approver_tokens() -> None:
    container = build_container(_development_settings())
    try:
        reviewer = container.token_verifier.verify("d" * 32)
        approver = container.token_verifier.verify("p" * 32)
        assert reviewer.roles == frozenset({Role.reviewer})
        assert approver.roles == frozenset({Role.approver})
        assert reviewer.subject != approver.subject
    finally:
        close = getattr(container.agent, "close", None)
        if callable(close):
            close()


def test_local_development_rejects_non_loopback_or_store_configuration() -> None:
    with pytest.raises(ValueError, match="literal HTTP loopback"):
        _development_settings(public_base_url="http://localhost:8080")
    with pytest.raises(ValueError, match="in-memory state"):
        _development_settings(database_url="postgresql://example.invalid/db")
    with pytest.raises(ValueError, match="in-memory state"):
        _development_settings(oidc_issuer="http://127.0.0.1:18088/realms/customs")


def test_local_development_uses_the_fixture_date_when_review_date_is_omitted() -> None:
    settings = _development_settings()
    reviewer = Principal(
        subject="local-reviewer", org_id="local-dev", roles=frozenset({Role.reviewer})
    )
    repository = InMemoryWorkbenchRepository()
    objects = InMemoryObjectStore()
    observed_dates: list[date] = []

    def review_handler(request):
        observed_dates.append(request.review_as_of)
        return build_agent_v3_result(request)

    agent = CallableAgentClient(review_handler)
    service = WorkbenchService(repository=repository, object_store=objects, agent=agent)
    app = create_app(
        settings=settings,
        container=Container(
            settings=settings,
            repository=repository,
            object_store=objects,
            agent=agent,
            token_verifier=StaticTokenVerifier({"reviewer-token": reviewer}),
            service=service,
        ),
    )
    with TestClient(app, client=("127.0.0.1", 50_000)) as client:
        headers = {"Authorization": "Bearer reviewer-token"}
        case = client.post("/api/v1/cases", json={"title": "Fixture date"}, headers=headers)
        assert case.status_code == 201
        case_id = case.json()["case_id"]
        document = client.post(
            f"/api/v1/cases/{case_id}/documents",
            json={
                "document_type": "CI",
                "filename": "fixture-date.txt",
                "content_text": "COMMERCIAL INVOICE\\nInvoice No: FIXTURE-DATE",
            },
            headers=headers,
        )
        assert document.status_code == 201
        review = client.post(
            f"/api/v1/cases/{case_id}/reviews",
            json={"trade_type": "import"},
            headers={**headers, "Idempotency-Key": "fixture-date-001"},
        )
        assert review.status_code == 202
        assert review.json()["job"]["status"] == "succeeded"

    assert observed_dates == [date(2026, 8, 5)]


def test_functional_uat_has_a_fixed_backend_owned_review_date() -> None:
    settings = Settings(
        _env_file=None,
        environment="functional-uat",
        public_base_url="http://127.0.0.1:18081",
        agent_base_url="http://127.0.0.1:18090",
        agent_service_token="a" * 32,
        database_url="postgresql://customs:local@127.0.0.1/customs",
        minio_endpoint="127.0.0.1:19000",
        minio_access_key="local-user",
        minio_secret_key="m" * 32,
        oidc_issuer="http://127.0.0.1:18088/realms/customs",
        oidc_audience="customs-workbench-api",
        oidc_jwks_url="http://127.0.0.1:18088/realms/customs/protocol/openid-connect/certs",
        oidc_client_id="customs-workbench-spa",
        oidc_authorization_endpoint="http://127.0.0.1:18088/realms/customs/protocol/openid-connect/auth",
        oidc_token_endpoint="http://127.0.0.1:18088/realms/customs/protocol/openid-connect/token",
    )
    assert settings.service_mode == "functional-uat"
    assert settings.functional_uat_as_of.isoformat() == "2026-08-05"


def test_functional_uat_rejects_nonlocal_store_or_identity_endpoints() -> None:
    values: dict[str, object] = {
        "environment": "functional-uat",
        "public_base_url": "http://127.0.0.1:18081",
        "agent_base_url": "http://127.0.0.1:18090",
        "agent_service_token": "a" * 32,
        "database_url": "postgresql://customs:local@127.0.0.1/customs",
        "minio_endpoint": "127.0.0.1:19000",
        "minio_access_key": "local-user",
        "minio_secret_key": "m" * 32,
        "oidc_issuer": "http://127.0.0.1:18088/realms/customs",
        "oidc_audience": "customs-workbench-api",
        "oidc_jwks_url": "http://127.0.0.1:18088/realms/customs/protocol/openid-connect/certs",
        "oidc_client_id": "customs-workbench-spa",
        "oidc_authorization_endpoint": "http://127.0.0.1:18088/realms/customs/protocol/openid-connect/auth",
        "oidc_token_endpoint": "http://127.0.0.1:18088/realms/customs/protocol/openid-connect/token",
    }
    with pytest.raises(ValueError, match="PostgreSQL on literal loopback"):
        Settings(
            _env_file=None,
            **{**values, "database_url": "postgresql://customs:x@db.invalid/customs"},
        )
    with pytest.raises(ValueError, match="minio_endpoint must use literal loopback"):
        Settings(_env_file=None, **{**values, "minio_endpoint": "store.invalid:9000"})
    with pytest.raises(ValueError, match="literal HTTP loopback"):
        Settings(
            _env_file=None,
            **{**values, "oidc_issuer": "https://identity.invalid/realms/customs"},
        )


def test_http_agent_mode_header_is_bidirectionally_bound() -> None:
    def accepted(request: httpx.Request) -> httpx.Response:
        assert request.headers["X-Customs-Service-Mode"] == "local-development"
        return httpx.Response(
            200,
            json={"status": "ready"},
            headers={"X-Customs-Service-Mode": "local-development"},
            request=request,
        )

    client = HttpAgentClient(
        base_url="http://agent.test",
        service_token="test-token",
        service_mode="local-development",
        transport=httpx.MockTransport(accepted),
    )
    try:
        assert client.metadata() == {"status": "ready"}
    finally:
        client.close()

    rejected = HttpAgentClient(
        base_url="http://agent.test",
        service_token="test-token",
        service_mode="local-development",
        transport=httpx.MockTransport(
            lambda request: httpx.Response(200, json={"status": "ready"}, request=request)
        ),
    )
    try:
        with pytest.raises(RuntimeError, match="service mode"):
            rejected.metadata()
    finally:
        rejected.close()


def test_functional_profile_uses_oidc_code_flow_and_two_local_roles(tmp_path) -> None:
    values, realm = _functional_uat(realm_path=tmp_path / "customs-realm.json")
    environment = dict(values)

    assert environment["CUSTOMS_BACKEND_DATABASE_URL"].startswith("postgresql://")
    assert environment["CUSTOMS_AGENT_SERVICE_MODE"] == "functional-uat"
    assert realm["roles"] == {"realm": [{"name": "reviewer"}, {"name": "approver"}]}
    assert [user["realmRoles"] for user in realm["users"]] == [["reviewer"], ["approver"]]
    assert [user["firstName"] for user in realm["users"]] == ["Functional", "Functional"]
    assert [user["lastName"] for user in realm["users"]] == ["Reviewer", "Approver"]
    assert all(user["emailVerified"] is True for user in realm["users"])
    client = realm["clients"][0]
    assert client["standardFlowEnabled"] is True
    assert client["directAccessGrantsEnabled"] is False


def test_profile_initializer_writes_ignored_local_values_without_echoing_them(
    tmp_path, monkeypatch, capsys
) -> None:
    monkeypatch.setattr(local_profile, "_workspace_root", lambda: tmp_path)
    monkeypatch.setattr(
        sys,
        "argv",
        ["customs-backend-profile-init", "--profile", "functional-uat"],
    )

    assert local_profile.main() == 0
    output = capsys.readouterr().out
    profile = tmp_path / "_local" / "profiles" / "functional-uat" / "backend.env"
    values = profile.read_text(encoding="utf-8")

    assert output == "Created _local/profiles/functional-uat/backend.env\n"
    assert "change_me" not in values
    assert "CUSTOMS_FUNCTIONAL_REVIEWER_PASSWORD=" in values
    assert "CUSTOMS_FUNCTIONAL_APPROVER_PASSWORD=" in values
    assert (profile.parent / "customs-realm.json").is_file()


def test_settings_do_not_load_a_repository_dotenv() -> None:
    assert Settings.model_config["env_file"] is None


def test_functional_login_helper_reads_only_the_selected_login_password_without_output() -> None:
    helper = (
        Path(__file__).resolve().parents[2] / "tools" / "Copy-FunctionalUatPassword.ps1"
    ).read_text(encoding="utf-8")

    assert "CUSTOMS_FUNCTIONAL_REVIEWER_PASSWORD" in helper
    assert "CUSTOMS_FUNCTIONAL_APPROVER_PASSWORD" in helper
    assert "ValidateSet('reviewer', 'approver')" in helper
    assert "Set-Clipboard -Value $passwords[0]" in helper
    assert "Write-Host" not in helper
    assert "Write-Output" not in helper
    assert "CUSTOMS_FUNCTIONAL_POSTGRES_PASSWORD" not in helper
    assert "CUSTOMS_FUNCTIONAL_MINIO_ROOT_PASSWORD" not in helper
    assert "CUSTOMS_FUNCTIONAL_KEYCLOAK_ADMIN_PASSWORD" not in helper
    assert "CUSTOMS_AGENT_SERVICE" not in helper


def test_profile_loader_is_local_only_and_does_not_echo_values() -> None:
    loader = (Path(__file__).resolve().parents[2] / "tools" / "Import-LocalProfile.ps1").read_text(
        encoding="utf-8"
    )

    assert "ValidateSet('dev-fixture', 'functional-uat')" in loader
    assert "ValidateSet('backend', 'agent')" in loader
    assert 'Set-Item -LiteralPath "Env:$key" -Value $value' in loader
    assert "CUSTOMS_AGENT_SERVICE_BEARER_TOKEN" in loader
    assert "CUSTOMS_AGENT_SERVICE_MODE" in loader
    assert "CUSTOMS_BACKEND_DEV_REVIEWER_SESSION_TOKEN" in loader
    assert "CUSTOMS_BACKEND_DEV_APPROVER_SESSION_TOKEN" in loader
    assert "CUSTOMS_FUNCTIONAL_APPROVER_PASSWORD" in loader
    assert "CUSTOMS_FUNCTIONAL_KEYCLOAK_ADMIN_PASSWORD" in loader
    assert "$allowedKeys = if ($Scope -eq 'backend') { $backendKeys } else { $agentKeys }" in loader
    assert 'Write-Host "Imported local $Profile $Scope profile values."' in loader
    assert "Invoke-Expression" not in loader
