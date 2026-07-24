from __future__ import annotations

import importlib
import json
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import httpx
import pytest
from fastapi.testclient import TestClient

from customs_backend.adapters import (
    CallableAgentClient,
    HttpAgentClient,
    InMemoryJobPublisher,
    InMemoryObjectStore,
)
from customs_backend.api import Container, build_container, create_app
from customs_backend.assertion_signing import (
    AssertionSigningConfigurationError,
    LocalUatRequirementPolicyFixtureV1,
    LocalUatRequirementPolicyIdentityResolver,
    load_local_uat_requirement_policy_resolver,
)
from customs_backend.auth import OidcJwtVerifier, StaticTokenVerifier
from customs_backend.domain import (
    Principal,
    Role,
    SupportingDocumentRecord,
    canonical_digest,
)
from customs_backend.repository import InMemoryWorkbenchRepository
from customs_backend.service import WorkbenchService
from customs_backend.settings import Settings
from customs_backend.uat_evidence_trace import UatEvidenceTraceResponse


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
        "agent_timeout_seconds": 435,
        "redis_url": "redis://redis:6379/0",
        "minio_endpoint": "minio:9000",
        "minio_access_key": "uat-minio-user",
        "minio_secret_key": "uat-minio-secret",
        "auto_completion_enabled": False,
        "review_actor_time_limit_seconds": 540,
        "job_lease_seconds": 600,
        "uat_evidence_profile": "preserved-3law-v3-scenario-eval",
        "uat_evidence_data_source": "ACTUAL_POSTGRESQL_QDRANT_NEO4J_READ_ONLY",
        "uat_evidence_policy_mode": "SEALED_TEST_ONLY_ACTUAL_CCCT_HUMAN_ONLY",
        "uat_evidence_expected_production_law_count": 58,
        "uat_evidence_observed_law_count": 3,
        "uat_evidence_observed_article_count": 1007,
        "uat_evidence_qdrant_point_count": 1007,
        "uat_evidence_neo4j_article_count": 1007,
        "uat_evidence_rag_quality_evaluated": False,
        "uat_planner_mode": "shadow",
        "uat_assertion_signing_key_registry_path": (
            "C:/_secrets/uat/assertion-signing-key-registry.json"
        ),
        "uat_assertion_trust_store_sha256": "d" * 64,
        "uat_requirement_policy_fixture_path": ("C:/_secrets/uat/requirement-policy-fixture.json"),
        "uat_requirement_policy_fixture_sha256": "e" * 64,
        "uat_evidence_runtime_v3_sha256": (
            _diagnostic_evidence_identity()["runtime_release_v3_sha256"]
        ),
        "uat_evidence_trace_path": (
            "C:/_artifacts/runtime/local-user-uat/three-law-scenario-eval-trace.jsonl"
        ),
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
        "requirement_policy_release": ("test-only-three-law-scenario-eval-human-only.v1"),
        "requirement_policy_manifest_sha256": (
            "2467226fe64c84f8fdc0b41a8ab44ebaba04e6ba7280cd3e4bd56bd2e9c38c95"
        ),
        "coverage_matrix_sha256": (
            "21012116b4fddf0ae0e0bb12343558aca472373480d32d535e6b59875800c444"
        ),
        "requirement_policy_approval_sha256": "a" * 64,
        "reviewer_trust_store_sha256": (
            "7864482157b701b44c457bd35c7721b31ffc31564ea43d83948aa27e08233637"
        ),
    }
    identity["runtime_release_v3_sha256"] = canonical_digest(identity)
    return identity


def _disabled_uat_assertion_fixture() -> dict[str, None]:
    return {
        "uat_assertion_signing_key_registry_path": None,
        "uat_assertion_trust_store_sha256": None,
        "uat_requirement_policy_fixture_path": None,
        "uat_requirement_policy_fixture_sha256": None,
        "uat_evidence_runtime_v3_sha256": None,
        "uat_evidence_trace_path": None,
    }


def _uat_policy_fixture_payload() -> dict[str, Any]:
    payload: dict[str, Any] = {
        "schema_version": "customs-backend-local-uat-requirement-policy/v1",
        "test_only": True,
        "production_eligible": False,
        "evidence_profile": "preserved-3law-v3-scenario-eval",
        "evidence_policy_mode": "SEALED_TEST_ONLY_ACTUAL_CCCT_HUMAN_ONLY",
        "evidence_runtime_v3_sha256": (
            _diagnostic_evidence_identity()["runtime_release_v3_sha256"]
        ),
        "required_document_code": "CCCT_DOC_46E819BB87B27371",
        "verification_policy": "human_only",
        "policy_source_sha256": (
            "8d57414b0b0352a78856d57ebaf5b69050b584ec6e9d94ff630d8d093a585dde"
        ),
        "policy_release": "test-only-three-law-scenario-eval-human-only.v1",
    }
    payload["fixture_sha256"] = canonical_digest(payload)
    return payload


def _uat_policy_resolver() -> LocalUatRequirementPolicyIdentityResolver:
    payload = _uat_policy_fixture_payload()
    return LocalUatRequirementPolicyIdentityResolver(
        LocalUatRequirementPolicyFixtureV1.model_validate(payload)
    )


def _write_uat_policy_fixture(root: Path, payload: dict[str, Any]) -> Path:
    path = root / "_secrets" / "uat" / "requirement-policy-fixture.json"
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


def test_local_uat_policy_loader_accepts_only_the_data_pinned_exact_code(
    tmp_path: Path,
) -> None:
    payload = _uat_policy_fixture_payload()
    path = _write_uat_policy_fixture(tmp_path, payload)
    resolver = load_local_uat_requirement_policy_resolver(
        path,
        expected_fixture_sha256=payload["fixture_sha256"],
        expected_runtime_v3_sha256=payload["evidence_runtime_v3_sha256"],
    )
    matched = resolver.resolve(
        SupportingDocumentRecord.model_construct(
            document_code="CCCT_DOC_46E819BB87B27371",
        )
    )
    unknown = resolver.resolve(
        SupportingDocumentRecord.model_construct(
            document_code="UNKNOWN_CODE",
        )
    )

    assert matched is not None
    assert matched.policy_source_sha256 == (
        "8d57414b0b0352a78856d57ebaf5b69050b584ec6e9d94ff630d8d093a585dde"
    )
    assert unknown is None


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("required_document_code", "CCCT_DOC_WRONG"),
        ("policy_source_sha256", "0" * 64),
        ("policy_release", "test-only-wrong-release.v1"),
    ],
)
def test_local_uat_policy_loader_rejects_rehashed_wrong_data_policy_identity(
    tmp_path: Path,
    field: str,
    value: str,
) -> None:
    payload = _uat_policy_fixture_payload()
    payload[field] = value
    payload["fixture_sha256"] = canonical_digest(
        {key: item for key, item in payload.items() if key != "fixture_sha256"}
    )
    path = _write_uat_policy_fixture(tmp_path, payload)

    with pytest.raises(
        AssertionSigningConfigurationError,
        match="release artifact is invalid",
    ):
        load_local_uat_requirement_policy_resolver(
            path,
            expected_fixture_sha256=payload["fixture_sha256"],
            expected_runtime_v3_sha256=payload["evidence_runtime_v3_sha256"],
        )


class _ReadyUatAssertionSigner:
    ready = True
    trust_store_sha256 = "d" * 64

    def sign_human_decision(self, **_kwargs: Any) -> None:
        return None


def test_local_uat_settings_require_real_runtime_adapters_and_force_diagnostic_mode() -> None:
    settings = Settings(**_local_uat_values())
    assert settings.environment == "local_uat"
    assert settings.auto_completion_enabled is False
    assert settings.agent_timeout_seconds == 435
    assert settings.review_actor_time_limit_seconds == 540
    assert settings.review_actor_time_limit_milliseconds == 540_000
    assert settings.job_lease_seconds == 600
    assert settings.uat_claude_mode == "actual"
    assert settings.uat_evidence_profile == "preserved-3law-v3-scenario-eval"
    assert settings.uat_evidence_policy_mode == "SEALED_TEST_ONLY_ACTUAL_CCCT_HUMAN_ONLY"
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
    with pytest.raises(ValueError, match="exact PostgreSQL/Qdrant/Neo4j count 1007"):
        Settings(
            **_local_uat_values(
                uat_evidence_observed_article_count=1006,
                uat_evidence_qdrant_point_count=1006,
                uat_evidence_neo4j_article_count=1006,
            )
        )
    with pytest.raises(
        ValueError,
        match="evidence_policy_mode=SEALED_TEST_ONLY_ACTUAL_CCCT_HUMAN_ONLY",
    ):
        Settings(**_local_uat_values(uat_evidence_policy_mode="ACTUAL_CODES_HUMAN_ONLY"))
    with pytest.raises(ValueError, match="requires uat_evidence_trace_path"):
        Settings(**_local_uat_values(uat_evidence_trace_path=None))
    with pytest.raises(ValueError, match="zero actual-store observations"):
        Settings(
            **_local_uat_values(
                uat_evidence_profile="synthetic-v3",
                uat_evidence_data_source="SYNTHETIC_V3_NO_RAG_QUALITY",
                uat_evidence_policy_mode="SYNTHETIC_FIXTURE",
                **_disabled_uat_assertion_fixture(),
            )
        )


def test_agent_timeout_parses_from_environment_and_has_safe_bounds(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    name = "CUSTOMS_BACKEND_AGENT_TIMEOUT_SECONDS"
    monkeypatch.setenv(name, "435")
    monkeypatch.setenv("CUSTOMS_BACKEND_REVIEW_ACTOR_TIME_LIMIT_SECONDS", "540")
    monkeypatch.setenv("CUSTOMS_BACKEND_JOB_LEASE_SECONDS", "600")
    assert Settings(_env_file=None).agent_timeout_seconds == 435

    for value in (0, 901):
        with pytest.raises(ValueError):
            Settings(agent_timeout_seconds=value)

    assert Settings(agent_timeout_seconds=1).agent_timeout_seconds == 1
    assert (
        Settings(
            agent_timeout_seconds=900,
            review_actor_time_limit_seconds=901,
            job_lease_seconds=902,
        ).agent_timeout_seconds
        == 900
    )


def test_review_actor_time_limit_parses_from_environment_and_has_safe_bounds(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    name = "CUSTOMS_BACKEND_REVIEW_ACTOR_TIME_LIMIT_SECONDS"
    monkeypatch.setenv(name, "540")
    monkeypatch.setenv("CUSTOMS_BACKEND_JOB_LEASE_SECONDS", "600")
    assert Settings(_env_file=None).review_actor_time_limit_milliseconds == 540_000

    for value in (59, 1801):
        with pytest.raises(ValueError):
            Settings(review_actor_time_limit_seconds=value)


def test_review_worker_registers_the_configured_hard_time_limit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from customs_backend import api as api_module
    from customs_backend import settings as settings_module

    settings = Settings(**_local_uat_values())
    fake_container = object()
    monkeypatch.setattr(settings_module, "get_settings", lambda: settings)
    monkeypatch.setattr(api_module, "build_container", lambda _settings: fake_container)
    sys.modules.pop("customs_backend.worker", None)

    try:
        worker_module = importlib.import_module("customs_backend.worker")
        assert worker_module._container() is fake_container
        assert (
            worker_module.execute_review_job.options["time_limit"]
            == settings.review_actor_time_limit_milliseconds
            == 540_000
        )
    finally:
        sys.modules.pop("customs_backend.worker", None)


def test_synthetic_local_uat_retains_the_existing_timeout_and_lease_defaults() -> None:
    values = _local_uat_values(
        uat_evidence_profile="synthetic-v3",
        uat_evidence_data_source="SYNTHETIC_V3_NO_RAG_QUALITY",
        uat_evidence_policy_mode="SYNTHETIC_FIXTURE",
        uat_evidence_observed_law_count=0,
        uat_evidence_observed_article_count=0,
        uat_evidence_qdrant_point_count=0,
        uat_evidence_neo4j_article_count=0,
        **_disabled_uat_assertion_fixture(),
    )
    values.pop("agent_timeout_seconds")
    values.pop("review_actor_time_limit_seconds")
    values.pop("job_lease_seconds")

    settings = Settings(**values)

    assert settings.agent_timeout_seconds == 180
    assert settings.review_actor_time_limit_seconds == 240
    assert settings.job_lease_seconds == 300


def test_existing_preserved_three_law_diagnostic_profile_keeps_its_identity() -> None:
    settings = Settings(
        **_local_uat_values(
            uat_evidence_profile="preserved-3law-v3-diagnostic",
            uat_evidence_policy_mode="ACTUAL_CODES_HUMAN_ONLY",
            uat_evidence_observed_article_count=1006,
            uat_evidence_qdrant_point_count=1006,
            uat_evidence_neo4j_article_count=1006,
            **_disabled_uat_assertion_fixture(),
        )
    )

    assert settings.uat_evidence_profile == "preserved-3law-v3-diagnostic"
    assert settings.uat_evidence_policy_mode == "ACTUAL_CODES_HUMAN_ONLY"
    assert settings.uat_evidence_observed_article_count == 1006

    with pytest.raises(
        ValueError,
        match="evidence_policy_mode=ACTUAL_CODES_HUMAN_ONLY",
    ):
        Settings(
            **_local_uat_values(
                uat_evidence_profile="preserved-3law-v3-diagnostic",
                uat_evidence_policy_mode=("SEALED_TEST_ONLY_ACTUAL_CCCT_HUMAN_ONLY"),
                **_disabled_uat_assertion_fixture(),
            )
        )


@pytest.mark.parametrize(
    (
        "agent_timeout_seconds",
        "review_actor_time_limit_seconds",
        "job_lease_seconds",
        "message",
    ),
    [
        (180, 540, 600, "agent_timeout_seconds >= 435"),
        (434, 540, 600, "agent_timeout_seconds >= 435"),
        (435, 435, 600, "review_actor_time_limit_seconds > agent_timeout_seconds"),
        (540, 540, 600, "review_actor_time_limit_seconds > agent_timeout_seconds"),
        (435, 540, 540, "job_lease_seconds > review_actor_time_limit_seconds"),
        (435, 600, 540, "job_lease_seconds > review_actor_time_limit_seconds"),
    ],
)
def test_preserved_three_law_local_uat_rejects_unsafe_timeout_chains(
    agent_timeout_seconds: int,
    review_actor_time_limit_seconds: int,
    job_lease_seconds: int,
    message: str,
) -> None:
    with pytest.raises(ValueError, match=message):
        Settings(
            **_local_uat_values(
                agent_timeout_seconds=agent_timeout_seconds,
                review_actor_time_limit_seconds=review_actor_time_limit_seconds,
                job_lease_seconds=job_lease_seconds,
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

    with pytest.raises(ValueError, match="Evidence trace configuration is runtime-only"):
        Settings(
            environment="test",
            uat_evidence_trace_path=(
                "C:/_artifacts/runtime/local-user-uat/three-law-scenario-eval-trace.jsonl"
            ),
        )


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
    monkeypatch.setattr(
        api_module,
        "load_local_uat_assertion_signer",
        lambda *_args, **_kwargs: _ReadyUatAssertionSigner(),
    )
    monkeypatch.setattr(
        api_module,
        "load_local_uat_requirement_policy_resolver",
        lambda *_args, **_kwargs: _uat_policy_resolver(),
    )
    monkeypatch.setattr(
        api_module,
        "UatEvidenceTraceReader",
        lambda *_args, **_kwargs: object(),
    )

    container = build_container(Settings(**_local_uat_values()))
    assert isinstance(container.repository, FakePostgres)
    assert isinstance(container.object_store, FakeMinio)
    assert isinstance(container.publisher, FakeDramatiq)
    assert isinstance(container.agent, FakeHttpAgent)
    assert container.agent.values["timeout_seconds"] == 435
    assert container.service.auto_completion_enabled is False

    development_container = build_container(
        Settings(
            environment="test",
            agent_base_url="http://agent.test",
            agent_service_token="test-agent-token",
            agent_timeout_seconds=321,
            review_actor_time_limit_seconds=360,
            job_lease_seconds=400,
        )
    )
    assert isinstance(development_container.agent, FakeHttpAgent)
    assert development_container.agent.values == {
        "base_url": "http://agent.test",
        "service_token": "test-agent-token",
        "timeout_seconds": 321,
    }


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
                "evidence_mode": "preserved-3law-v3-scenario-eval",
                "evidence_policy_mode": "SEALED_TEST_ONLY_ACTUAL_CCCT_HUMAN_ONLY",
                "evidence_profile": {
                    "profile_id": "preserved-3law-v3-scenario-eval",
                    "direct_database_access": False,
                    "mock_fallback_allowed": False,
                    "release_eligible": False,
                },
                "evidence_runtime_identity": evidence_identity,
                "assertion_fixture_mode": "LOOPBACK_TEST_ONLY_HUMAN_APPROVER",
                "assertion_trust_store_sha256": "d" * 64,
                "trusted_assertion_verifier_configured": True,
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
            "evidence_mode": "preserved-3law-v3-scenario-eval",
            "evidence_policy_mode": "SEALED_TEST_ONLY_ACTUAL_CCCT_HUMAN_ONLY",
            "assertion_fixture_mode": "LOOPBACK_TEST_ONLY_HUMAN_APPROVER",
            "assertion_trust_store_sha256": "d" * 64,
            "trusted_assertion_verifier_configured": True,
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
        assertion_signer=_ReadyUatAssertionSigner(),
        requirement_policy_resolver=_uat_policy_resolver(),
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
                "evidence_policy_mode": "SYNTHETIC_FIXTURE",
                "assertion_fixture_mode": "DISABLED",
                "assertion_trust_store_sha256": None,
                "trusted_assertion_verifier_configured": False,
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
        assert ready.json()["evidence_profile"] == "preserved-3law-v3-scenario-eval"
        assert ready.json()["evidence_scope"] == "ACTUAL_3_LAW_RAG_SCENARIO_EVAL_3_OF_58"
        assert ready.json()["evidence_scope_label"] == "ACTUAL_3_LAW_RAG_SCENARIO_EVAL_3_OF_58"
        assert ready.json()["evidence_data_source"] == "ACTUAL_POSTGRESQL_QDRANT_NEO4J_READ_ONLY"
        assert ready.json()["evidence_policy_mode"] == "SEALED_TEST_ONLY_ACTUAL_CCCT_HUMAN_ONLY"
        assert ready.json()["agent_status"] == "ready"
        assert ready.json()["agent_uat_status"] == "uat-ready"
        assert (
            ready.json()["evidence_runtime_v3_sha256"]
            == (_diagnostic_evidence_identity()["runtime_release_v3_sha256"])
        )
        assert "agent" not in ready.json()
        assert "agent_uat" not in ready.json()
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
            "evidence_profile": "preserved-3law-v3-scenario-eval",
            "evidence_scope": "ACTUAL_3_LAW_RAG_SCENARIO_EVAL_3_OF_58",
            "evidence_scope_label": "ACTUAL_3_LAW_RAG_SCENARIO_EVAL_3_OF_58",
            "evidence_data_source": "ACTUAL_POSTGRESQL_QDRANT_NEO4J_READ_ONLY",
            "evidence_policy_mode": "SEALED_TEST_ONLY_ACTUAL_CCCT_HUMAN_ONLY",
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


def test_local_uat_health_never_serializes_upstream_urls_secrets_or_raw_metadata(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(OidcJwtVerifier, "health", lambda _self: True)
    with _local_uat_app() as client:
        agent = client.app.state.container.agent
        agent._metadata["uat"]["evidence_base_url"] = "http://127.0.0.1:18013/PRIVATE-DATA-TOPOLOGY"
        agent._metadata["uat"]["private_marker"] = "UPSTREAM-PRIVATE-METADATA"
        agent._uat_readiness["raw_upstream_marker"] = "UPSTREAM-READINESS-PRIVATE"
        response = client.get("/health/uat-ready")

    assert response.status_code == 200
    for forbidden in (
        "PRIVATE-DATA-TOPOLOGY",
        "UPSTREAM-PRIVATE-METADATA",
        "UPSTREAM-READINESS-PRIVATE",
        "evidence_base_url",
    ):
        assert forbidden not in response.text


def test_scenario_trace_endpoint_is_authenticated_and_run_identity_bound(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    with _local_uat_app() as client:
        container = client.app.state.container
        container.token_verifier = StaticTokenVerifier(
            {
                "reviewer-token": Principal(
                    subject="reviewer-1",
                    org_id="org-a",
                    roles=frozenset({Role.reviewer}),
                )
            }
        )
        calls: list[dict[str, str]] = []

        class Reader:
            def projection(
                self,
                *,
                request_id: str,
                knowledge_snapshot_id: str,
            ) -> UatEvidenceTraceResponse:
                calls.append(
                    {
                        "request_id": request_id,
                        "knowledge_snapshot_id": knowledge_snapshot_id,
                    }
                )
                return UatEvidenceTraceResponse.model_validate(
                    {
                        "request_id": request_id,
                        "knowledge_snapshot_id": knowledge_snapshot_id,
                        "retrieval": {
                            stage: {
                                "attempted": True,
                                "succeeded": True,
                                "result_count": 0,
                                "duration_ms": 1,
                                "top_k_count": 0,
                            }
                            for stage in ("structured", "dense", "bm25", "graph")
                        },
                    }
                )

        container.uat_evidence_trace_reader = Reader()
        run = SimpleNamespace(
            result_detail={
                "knowledge_snapshot_id": "snapshot-1",
                "evidence_provenance": {"request_id": "run-1-retrieve-0"},
            },
            release_identity={"evidence_snapshot_id": "snapshot-1"},
        )

        def get_run(principal: Principal, review_run_id: str) -> Any:
            assert principal.org_id == "org-a"
            assert review_run_id == "run-1"
            return run

        monkeypatch.setattr(container.service, "get_run", get_run)
        response = client.get(
            "/api/v1/uat/review-runs/run-1/evidence-trace",
            headers={"Authorization": "Bearer reviewer-token"},
        )

    assert response.status_code == 200
    assert response.json()["retrieval"]["graph"] == {
        "attempted": True,
        "succeeded": True,
        "result_count": 0,
        "duration_ms": 1,
        "top_k_count": 0,
    }
    assert calls == [
        {
            "request_id": "run-1-retrieve-0",
            "knowledge_snapshot_id": "snapshot-1",
        }
    ]


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


@pytest.mark.parametrize(
    ("field", "value"),
    [
        (
            "requirement_policy_release",
            "test-only-conformance.no-production-policy.v1",
        ),
        (
            "requirement_policy_manifest_sha256",
            "54aa31a0baed9a8eeeea57f9c436a78b8d687e244538491a46e34af46238cf7a",
        ),
        ("coverage_matrix_sha256", "a" * 64),
        ("reviewer_trust_store_sha256", "b" * 64),
    ],
)
def test_backend_independently_rejects_rehashed_cross_profile_policy_identity(
    monkeypatch: pytest.MonkeyPatch,
    field: str,
    value: str,
) -> None:
    monkeypatch.setattr(OidcJwtVerifier, "health", lambda _self: True)
    with _local_uat_app() as client:
        agent = client.app.state.container.agent
        identity = agent._metadata["evidence_runtime_release_v3"]["identity"]
        identity[field] = value
        unsigned = {
            key: item for key, item in identity.items() if key != "runtime_release_v3_sha256"
        }
        identity["runtime_release_v3_sha256"] = canonical_digest(unsigned)
        agent._metadata["uat"]["evidence_runtime_identity"] = identity

        assert client.get("/health/uat-ready").status_code == 503
        assert client.get("/api/v1/uat/metadata").status_code == 503


@pytest.mark.parametrize(
    ("target", "value"),
    [
        ("metadata_profile", "preserved-3law-v3-diagnostic"),
        ("metadata_policy", "ACTUAL_CODES_HUMAN_ONLY"),
        ("readiness_profile", "preserved-3law-v3-diagnostic"),
        ("readiness_policy", "ACTUAL_CODES_HUMAN_ONLY"),
    ],
)
def test_local_uat_fails_closed_for_agent_profile_or_policy_drift(
    monkeypatch: pytest.MonkeyPatch,
    target: str,
    value: str,
) -> None:
    monkeypatch.setattr(OidcJwtVerifier, "health", lambda _self: True)
    with _local_uat_app() as client:
        agent = client.app.state.container.agent
        if target == "metadata_profile":
            agent._metadata["uat"]["evidence_mode"] = value
        elif target == "metadata_policy":
            agent._metadata["uat"]["evidence_policy_mode"] = value
        elif target == "readiness_profile":
            agent._uat_readiness["evidence_mode"] = value
        else:
            agent._uat_readiness["evidence_policy_mode"] = value

        assert client.get("/health/uat-ready").status_code == 503
        if target.startswith("metadata_"):
            assert client.get("/api/v1/uat/metadata").status_code == 503


def test_uat_endpoints_are_unavailable_outside_local_uat() -> None:
    settings = Settings(environment="test")
    repository = InMemoryWorkbenchRepository()
    object_store = InMemoryObjectStore()
    publisher = InMemoryJobPublisher()
    agent = CallableAgentClient(lambda _request: {}, metadata={"status": "ready"})
    verifier = StaticTokenVerifier(
        {
            "reviewer-token": Principal(
                subject="reviewer-1",
                org_id="org-a",
                roles=frozenset({Role.reviewer}),
            )
        }
    )
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
        assert (
            client.get(
                "/api/v1/uat/review-runs/run-1/evidence-trace",
                headers={"Authorization": "Bearer reviewer-token"},
            ).status_code
            == 404
        )
