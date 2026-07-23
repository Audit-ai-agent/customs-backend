from __future__ import annotations

from typing import Any

import pytest
import httpx
from fastapi.testclient import TestClient

from customs_backend.adapters import (
    CallableAgentClient,
    HttpAgentClient,
    InMemoryJobPublisher,
    InMemoryObjectStore,
)
from customs_backend.api import Container, build_container, create_app
from customs_backend.auth import OidcJwtVerifier
from customs_backend.domain import canonical_digest
from customs_backend.repository import InMemoryWorkbenchRepository
from customs_backend.service import WorkbenchService
from customs_backend.settings import Settings


def _local_uat_values(**overrides: Any) -> dict[str, Any]:
    values: dict[str, Any] = {
        "environment": "local_uat",
        "database_url": "postgresql+psycopg://customs_uat:secret@operations-db:5432/customs",
        "public_base_url": "http://127.0.0.1:18088",
        "oidc_issuer": "http://127.0.0.1:18088/oidc/realms/customs",
        "oidc_audience": "customs-workbench-api",
        "oidc_jwks_url": ("http://keycloak:8080/oidc/realms/customs/protocol/openid-connect/certs"),
        "oidc_client_id": "customs-workbench-spa",
        "oidc_authorization_endpoint": (
            "http://127.0.0.1:18088/oidc/realms/customs/protocol/openid-connect/auth"
        ),
        "oidc_token_endpoint": (
            "http://127.0.0.1:18088/oidc/realms/customs/protocol/openid-connect/token"
        ),
        "agent_base_url": "http://agent-uat:8080",
        "agent_service_token": "uat-agent-token",
        "redis_url": "redis://redis:6379/0",
        "minio_endpoint": "minio:9000",
        "minio_access_key": "uat-minio-user",
        "minio_secret_key": "uat-minio-secret",
        "auto_completion_enabled": False,
        "uat_evidence_profile": "preserved-3law-v3-diagnostic",
        "uat_evidence_data_source": "ACTUAL_POSTGRESQL_QDRANT_NEO4J_READ_ONLY",
        "uat_evidence_expected_production_law_count": 58,
        "uat_evidence_observed_law_count": 3,
        "uat_evidence_observed_article_count": 1007,
        "uat_evidence_qdrant_point_count": 1007,
        "uat_evidence_neo4j_article_count": 1007,
        "uat_evidence_rag_quality_evaluated": False,
        "uat_planner_mode": "shadow",
    }
    values.update(overrides)
    return values


def _diagnostic_evidence_identity() -> dict[str, Any]:
    base = {
        "contract": "evidence-runtime-release",
        "schema_version": "2.0",
        "retrieval_schema_version": "1.0",
        "provider_release": "evidence-api-v1.2.0",
        "provider_revision": "a" * 40,
        "provider_build_sha256": "b" * 64,
        "retrieval_schema_sha256": "c" * 64,
        "provider_source_manifest_sha256": "d" * 64,
        "knowledge_generation_id": "e" * 64,
        "knowledge_generation_attestation_sha256": "f" * 64,
        "knowledge_source_manifest_sha256": "0" * 64,
        "model_manifest_sha256": "1" * 64,
        "index_config_sha256": "2" * 64,
        "evaluation_report_sha256": "3" * 64,
        "seal_attestation_sha256": "4" * 64,
        "knowledge_snapshot_id": "diagnostic-snapshot",
        "supported_review_as_of": "2026-07-10",
    }
    base["runtime_release_sha256"] = canonical_digest(base)
    identity = {
        "contract": "evidence-runtime-release",
        "schema_version": "3.0",
        "retrieval_schema_version": "3.0",
        "base_runtime_release": base,
        "retrieval_schema_sha256": "5" * 64,
        "readiness_schema_sha256": "6" * 64,
        "requirement_policy_schema_sha256": "7" * 64,
        "requirement_policy_release": "diagnostic-policy-v1",
        "requirement_policy_manifest_sha256": "8" * 64,
        "coverage_matrix_sha256": "9" * 64,
        "requirement_policy_approval_sha256": "a" * 64,
        "reviewer_trust_store_sha256": "b" * 64,
    }
    identity["runtime_release_v3_sha256"] = canonical_digest(identity)
    return identity


def test_local_uat_settings_require_real_runtime_adapters_and_force_diagnostic_mode() -> None:
    settings = Settings(**_local_uat_values())
    assert settings.environment == "local_uat"
    assert settings.auto_completion_enabled is False
    assert settings.uat_claude_mode == "actual"
    assert (
        Settings(
            **_local_uat_values(uat_evidence_rag_quality_evaluated="false")
        ).uat_evidence_rag_quality_evaluated
        is False
    )

    for name in (
        "oidc_issuer",
        "agent_service_token",
        "redis_url",
        "minio_endpoint",
    ):
        values = _local_uat_values()
        values[name] = None
        with pytest.raises(ValueError, match="local_uat configuration is incomplete"):
            Settings(**values)

    with pytest.raises(ValueError, match="local_uat configuration requires database_url"):
        Settings(**_local_uat_values(database_url=None))

    with pytest.raises(ValueError, match="must be PostgreSQL"):
        Settings(**_local_uat_values(database_url="sqlite:///uat.db"))
    with pytest.raises(ValueError, match="auto_completion_enabled=false"):
        Settings(**_local_uat_values(auto_completion_enabled=True))
    with pytest.raises(ValueError, match="must be explicit false"):
        Settings(**_local_uat_values(uat_evidence_rag_quality_evaluated=True))
    with pytest.raises(ValueError, match="corroborated 3/58"):
        Settings(**_local_uat_values(uat_evidence_observed_law_count=2))
    with pytest.raises(ValueError, match="corroborated 3/58"):
        Settings(**_local_uat_values(uat_evidence_qdrant_point_count=1006))
    with pytest.raises(ValueError, match="zero actual-store observations"):
        Settings(
            **_local_uat_values(
                uat_evidence_profile="synthetic-v3",
                uat_evidence_data_source="SYNTHETIC_V3_NO_RAG_QUALITY",
            )
        )


@pytest.mark.parametrize("value", [True, "true", 1, "1", 0, "0", "no", "off"])
def test_rag_quality_claim_rejects_non_false_values_before_environment_policy(
    value: object,
) -> None:
    with pytest.raises(ValueError, match="must be explicit false"):
        Settings(uat_evidence_rag_quality_evaluated=value)  # type: ignore[arg-type]


def test_rag_quality_claim_parses_the_actual_base_settings_environment_source(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    name = "CUSTOMS_BACKEND_UAT_EVIDENCE_RAG_QUALITY_EVALUATED"
    monkeypatch.setenv(name, "false")
    assert Settings(_env_file=None).uat_evidence_rag_quality_evaluated is False

    monkeypatch.setenv(name, "true")
    with pytest.raises(ValueError, match="must be explicit false"):
        Settings(_env_file=None)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("agent_expected_model_revision", "claude-release-1"),
        ("agent_expected_evidence_snapshot_id", "sealed-snapshot"),
        ("agent_expected_release_profile_id", "approved-profile"),
        ("assertion_signing_key_registry_path", "/run/secrets/signing.json"),
        ("requirement_policy_release_path", "/run/releases/policy.json"),
        ("auto_completion_profile_id", "approved-profile"),
    ],
)
def test_local_uat_rejects_production_release_only_configuration(
    field: str,
    value: str,
) -> None:
    with pytest.raises(ValueError, match="rejects Production release-only configuration"):
        Settings(**_local_uat_values(**{field: value}))

    with pytest.raises(ValueError, match="unreleased Agent trust-store"):
        Settings(**_local_uat_values(agent_expected_assertion_trust_store_release_state="active"))


def test_local_uat_migration_and_outbox_roles_are_minimal_and_supported() -> None:
    migration = Settings(
        environment="local_uat",
        process_role="migration",
        database_url="postgresql+psycopg://customs_uat:secret@operations-db:5432/customs",
    )
    assert migration.process_role == "migration"

    outbox = Settings(
        environment="local_uat",
        process_role="outbox",
        database_url="postgresql+psycopg://customs_uat:secret@operations-db:5432/customs",
        redis_url="redis://redis:6379/0",
    )
    assert outbox.process_role == "outbox"

    with pytest.raises(ValueError, match="outbox configuration requires redis_url"):
        Settings(
            environment="local_uat",
            process_role="outbox",
            database_url="postgresql+psycopg://customs_uat:secret@operations-db:5432/customs",
        )


def test_local_uat_container_selects_only_real_boundary_adapters(monkeypatch) -> None:
    import customs_backend.api as api_module

    class FakePostgres:
        def __init__(self, database_url: str) -> None:
            self.database_url = database_url

    class FakeMinio:
        def __init__(self, **values: Any) -> None:
            self.values = values

    class FakeDramatiq:
        def __init__(self, redis_url: str) -> None:
            self.redis_url = redis_url

    class FakeHttpAgent:
        def __init__(self, **values: Any) -> None:
            self.values = values

        def metadata(self) -> dict[str, str]:
            return {"status": "ready"}

    monkeypatch.setattr(api_module, "PostgresWorkbenchRepository", FakePostgres)
    monkeypatch.setattr(api_module, "MinioObjectStore", FakeMinio)
    monkeypatch.setattr(api_module, "DramatiqJobPublisher", FakeDramatiq)
    monkeypatch.setattr(api_module, "HttpAgentClient", FakeHttpAgent)

    container = build_container(Settings(**_local_uat_values()))
    assert isinstance(container.repository, FakePostgres)
    assert isinstance(container.object_store, FakeMinio)
    assert isinstance(container.publisher, FakeDramatiq)
    assert isinstance(container.agent, FakeHttpAgent)
    assert container.service.auto_completion_enabled is False


def _local_uat_app(*, agent_status: str = "ready") -> TestClient:
    settings = Settings(**_local_uat_values())
    repository = InMemoryWorkbenchRepository()
    object_store = InMemoryObjectStore()
    publisher = InMemoryJobPublisher()
    evidence_identity = _diagnostic_evidence_identity()
    agent = CallableAgentClient(
        lambda _request: {},
        metadata={
            "status": agent_status,
            "service_api_version": "internal-agent-service/v2",
            "service_mode": "local_user_uat_diagnostic",
            "assurance_level": "diagnostic",
            "transport_limits": {"text_only": True},
            "agentic_limits": {"effective_execution_mode": "shadow"},
            "model": {"id": "claude-sonnet-5"},
            "uat": {
                "diagnostic_only": True,
                "mock_fallback_allowed": False,
                "auto_completion_allowed": False,
                "evidence_mode": "preserved-3law-v3-diagnostic",
                "evidence_runtime_identity": evidence_identity,
            },
            "evidence_runtime_release_v3": {
                "release_state": "diagnostic",
                "identity": evidence_identity,
            },
        },
        uat_readiness={
            "status": "uat-ready" if agent_status == "ready" else "unavailable",
            "diagnostic_only": True,
            "text_only": True,
            "mock_fallback_allowed": False,
            "auto_completion_allowed": False,
            "evidence_mode": "preserved-3law-v3-diagnostic",
        },
    )
    verifier = OidcJwtVerifier(
        issuer=settings.oidc_issuer,
        audience=settings.oidc_audience,
        jwks_url=settings.oidc_jwks_url,
    )
    service = WorkbenchService(
        repository=repository,
        object_store=object_store,
        publisher=publisher,
        agent=agent,
        auto_completion_enabled=False,
    )
    container = Container(
        settings=settings,
        repository=repository,
        object_store=object_store,
        publisher=publisher,
        agent=agent,
        token_verifier=verifier,
        service=service,
    )
    return TestClient(create_app(container=container, settings=settings))


def test_http_agent_client_probes_the_explicit_uat_readiness_boundary() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/health/uat-ready"
        assert request.headers["Authorization"] == "Bearer uat-agent-token"
        return httpx.Response(
            200,
            json={
                "status": "uat-ready",
                "diagnostic_only": True,
                "text_only": True,
                "mock_fallback_allowed": False,
                "auto_completion_allowed": False,
                "evidence_mode": "synthetic-v3",
            },
            request=request,
        )

    client = HttpAgentClient(
        base_url="http://agent-uat.test",
        service_token="uat-agent-token",
        transport=httpx.MockTransport(handler),
    )
    try:
        assert client.uat_readiness()["status"] == "uat-ready"
    finally:
        client.close()


def test_local_uat_readiness_is_separate_from_strict_production_readiness(
    monkeypatch,
) -> None:
    monkeypatch.setattr(OidcJwtVerifier, "health", lambda _self: True)
    with _local_uat_app() as client:
        assert client.get("/health/ready").status_code == 503
        ready = client.get("/health/uat-ready")
        assert ready.status_code == 200
        assert ready.json()["mode"] == "local_uat"
        assert ready.json()["auto_completion_enabled"] is False
        assert ready.json()["production_release"] is False
        assert ready.json()["evidence_profile"] == "preserved-3law-v3-diagnostic"
        assert ready.json()["evidence_scope_label"] == "ACTUAL_3_LAW_RAG_3_OF_58"
        assert ready.json()["evidence_dataset"] == {
            "data_source": "ACTUAL_POSTGRESQL_QDRANT_NEO4J_READ_ONLY",
            "expected_production_law_count": 58,
            "observed_law_count": 3,
            "observed_article_count": 1007,
            "store_counts": {
                "postgresql_article_count": 1007,
                "qdrant_point_count": 1007,
                "neo4j_article_count": 1007,
            },
            "rag_quality_evaluated": False,
        }

        metadata = client.get("/api/v1/uat/metadata")
        assert metadata.status_code == 200
        assert metadata.json() == {
            "mode": "local_uat",
            "assurance_ceiling": "diagnostic",
            "text_only": True,
            "auto_completion_enabled": False,
            "claude_mode": "actual",
            "evidence_profile": "preserved-3law-v3-diagnostic",
            "evidence_scope_label": "ACTUAL_3_LAW_RAG_3_OF_58",
            "evidence_dataset": {
                "data_source": "ACTUAL_POSTGRESQL_QDRANT_NEO4J_READ_ONLY",
                "expected_production_law_count": 58,
                "observed_law_count": 3,
                "observed_article_count": 1007,
                "store_counts": {
                    "postgresql_article_count": 1007,
                    "qdrant_point_count": 1007,
                    "neo4j_article_count": 1007,
                },
                "rag_quality_evaluated": False,
            },
            "supported_review_as_of": "2026-07-10",
            "planner_mode": "shadow",
            "production_release": False,
        }
        assert ready.json()["evidence_dataset"] == metadata.json()["evidence_dataset"]


def test_local_uat_readiness_fails_closed_for_an_unready_agent(monkeypatch) -> None:
    monkeypatch.setattr(OidcJwtVerifier, "health", lambda _self: True)
    with _local_uat_app(agent_status="unavailable") as client:
        assert client.get("/health/uat-ready").status_code == 503


@pytest.mark.parametrize("mutation", ["missing", "invalid_date", "identity_drift"])
def test_local_uat_metadata_fails_closed_for_invalid_agent_evidence_identity(
    monkeypatch,
    mutation: str,
) -> None:
    monkeypatch.setattr(OidcJwtVerifier, "health", lambda _self: True)
    with _local_uat_app() as client:
        metadata = client.app.state.container.agent._metadata
        if mutation == "missing":
            metadata.pop("evidence_runtime_release_v3")
        elif mutation == "invalid_date":
            metadata["evidence_runtime_release_v3"]["identity"]["base_runtime_release"][
                "supported_review_as_of"
            ] = "2026-07-10T00:00:00Z"
        else:
            metadata["uat"]["evidence_runtime_identity"] = _diagnostic_evidence_identity()
            metadata["uat"]["evidence_runtime_identity"]["runtime_release_v3_sha256"] = "0" * 64
        assert client.get("/health/uat-ready").status_code == 503
        assert client.get("/api/v1/uat/metadata").status_code == 503


def test_uat_endpoints_are_unavailable_outside_local_uat() -> None:
    settings = Settings(environment="test")
    repository = InMemoryWorkbenchRepository()
    object_store = InMemoryObjectStore()
    publisher = InMemoryJobPublisher()
    agent = CallableAgentClient(lambda _request: {}, metadata={"status": "ready"})
    verifier = OidcJwtVerifier(issuer=None, audience=None, jwks_url=None)
    service = WorkbenchService(
        repository=repository,
        object_store=object_store,
        publisher=publisher,
        agent=agent,
    )
    container = Container(
        settings=settings,
        repository=repository,
        object_store=object_store,
        publisher=publisher,
        agent=agent,
        token_verifier=verifier,
        service=service,
    )
    with TestClient(create_app(container=container, settings=settings)) as client:
        assert client.get("/health/uat-ready").status_code == 404
        assert client.get("/api/v1/uat/metadata").status_code == 404
