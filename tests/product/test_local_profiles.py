from __future__ import annotations

import sys
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient

from customs_backend.adapters import CallableAgentClient, HttpAgentClient, InMemoryObjectStore
from customs_backend.api import Container, create_app
from customs_backend.auth import StaticTokenVerifier
from customs_backend.domain import Principal, Role
from customs_backend.repository import InMemoryWorkbenchRepository
from customs_backend.service import WorkbenchService
from customs_backend.settings import Settings
from customs_backend import local_profile
from customs_backend.local_profile import _functional_uat


def _development_settings(**overrides: object) -> Settings:
    values: dict[str, object] = {
        "environment": "local-development",
        "public_base_url": "http://127.0.0.1:8080",
        "agent_base_url": "http://127.0.0.1:8090",
        "agent_service_token": "a" * 32,
        "dev_session_token": "d" * 32,
    }
    values.update(overrides)
    return Settings(_env_file=None, **values)


def _development_client(*, client_host: str) -> TestClient:
    settings = _development_settings()
    principal = Principal(
        subject=settings.dev_session_subject,
        org_id=settings.dev_session_org_id,
        roles=frozenset({Role.reviewer}),
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
                token_verifier=StaticTokenVerifier({"d" * 32: principal}),
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
        assert payload["session"]["subject"] == "local-developer"
        assert payload["session"]["roles"] == ["reviewer"]
        session = client.get(
            "/api/v1/session",
            headers={"Authorization": f"Bearer {payload['access_token']}"},
        )
        assert session.status_code == 200
        assert client.get("/api/v1/auth/config").status_code == 404
        assert client.get("/api/v2/jobs/not-a-local-api").status_code == 404
    with _development_client(client_host="192.0.2.10") as client:
        assert client.post("/api/v1/dev/session").status_code == 404


def test_local_development_rejects_non_loopback_or_store_configuration() -> None:
    with pytest.raises(ValueError, match="literal HTTP loopback"):
        _development_settings(public_base_url="http://localhost:8080")
    with pytest.raises(ValueError, match="in-memory state"):
        _development_settings(database_url="postgresql://example.invalid/db")
    with pytest.raises(ValueError, match="in-memory state"):
        _development_settings(oidc_issuer="http://127.0.0.1:18088/realms/customs")


def test_functional_uat_has_a_fixed_backend_owned_review_date() -> None:
    settings = Settings(
        _env_file=None,
        environment="functional-uat",
        public_base_url="http://127.0.0.1:8080",
        agent_base_url="http://127.0.0.1:8090",
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
        "public_base_url": "http://127.0.0.1:8080",
        "agent_base_url": "http://127.0.0.1:8090",
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
        Settings(_env_file=None, **{**values, "database_url": "postgresql://customs:x@db.invalid/customs"})
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


def test_functional_profile_uses_oidc_code_flow_and_reviewer_only(tmp_path) -> None:
    values, realm = _functional_uat(realm_path=tmp_path / "customs-realm.json")
    environment = dict(values)

    assert environment["CUSTOMS_BACKEND_DATABASE_URL"].startswith("postgresql://")
    assert environment["CUSTOMS_AGENT_SERVICE_MODE"] == "functional-uat"
    assert realm["roles"] == {"realm": [{"name": "reviewer"}]}
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
    assert (profile.parent / "customs-realm.json").is_file()


def test_settings_do_not_load_a_repository_dotenv() -> None:
    assert Settings.model_config["env_file"] is None


def test_functional_login_helper_only_reads_the_reviewer_password_without_output() -> None:
    helper = (
        Path(__file__).resolve().parents[2] / "tools" / "Copy-FunctionalUatPassword.ps1"
    ).read_text(encoding="utf-8")

    assert "CUSTOMS_FUNCTIONAL_REVIEWER_PASSWORD" in helper
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
    assert "Set-Item -LiteralPath \"Env:$key\" -Value $value" in loader
    assert "CUSTOMS_AGENT_SERVICE_BEARER_TOKEN" in loader
    assert "CUSTOMS_AGENT_SERVICE_MODE" in loader
    assert "CUSTOMS_FUNCTIONAL_KEYCLOAK_ADMIN_PASSWORD" in loader
    assert "$allowedKeys = if ($Scope -eq 'backend') { $backendKeys } else { $agentKeys }" in loader
    assert "Write-Host \"Imported local $Profile $Scope profile values.\"" in loader
    assert "Invoke-Expression" not in loader
