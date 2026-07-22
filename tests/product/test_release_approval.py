from __future__ import annotations

import base64
import hashlib
import json
from datetime import UTC, date, datetime, timedelta
from importlib import resources

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from fastapi.testclient import TestClient
from pydantic import ValidationError

from customs_backend.adapters import (
    CallableAgentClient,
    InMemoryJobPublisher,
    InMemoryObjectStore,
)
from customs_backend.domain import AgentDocumentInput, AgentReviewRequest, canonical_digest
from customs_backend.release import (
    AgentContractPayloadError,
    AgentReleasePin,
    AgentServicePin,
    AutoCompletionApprovalManifest,
    DomainReviewerTrustStore,
    EvidenceProviderContractPin,
    ReleaseConfigurationError,
    load_agent_contract_lock,
    load_auto_completion_approval,
    load_domain_reviewer_trust_store,
    validate_agent_contract_bundle,
    validate_agent_v3_payload,
)
from customs_backend.repository import InMemoryWorkbenchRepository
from customs_backend.api import build_container, create_app
from customs_backend.service import WorkbenchService
from customs_backend.settings import Settings
from tests.product.agent_v3_fixture import build_agent_v3_result


def _canonical_bytes(value: dict) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")


def _timestamp(value: datetime) -> str:
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")


def _public_key(private_key: Ed25519PrivateKey) -> str:
    value = private_key.public_key().public_bytes(
        encoding=serialization.Encoding.Raw,
        format=serialization.PublicFormat.Raw,
    )
    return base64.b64encode(value).decode("ascii")


def _evidence_release_payloads(
    provider: EvidenceProviderContractPin,
    *,
    snapshot_id: str,
) -> tuple[dict, dict]:
    base = {
        "contract": "evidence-runtime-release",
        "schema_version": "2.0",
        "retrieval_schema_version": "1.0",
        "provider_release": provider.provider_release,
        "provider_revision": provider.provider_revision,
        "provider_build_sha256": provider.provider_build_sha256,
        "retrieval_schema_sha256": provider.retrieval_schema_sha256,
        "provider_source_manifest_sha256": provider.provider_source_manifest_sha256,
        "knowledge_generation_id": "a" * 64,
        "knowledge_generation_attestation_sha256": "b" * 64,
        "knowledge_source_manifest_sha256": "c" * 64,
        "model_manifest_sha256": "d" * 64,
        "index_config_sha256": "e" * 64,
        "evaluation_report_sha256": "f" * 64,
        "seal_attestation_sha256": "0" * 64,
        "knowledge_snapshot_id": snapshot_id,
        "supported_review_as_of": "2026-07-22",
    }
    base["runtime_release_sha256"] = canonical_digest(base)
    v3 = {
        "contract": "evidence-runtime-release",
        "schema_version": "3.0",
        "retrieval_schema_version": "3.0",
        "base_runtime_release": base,
        "retrieval_schema_sha256": "1" * 64,
        "readiness_schema_sha256": "2" * 64,
        "requirement_policy_schema_sha256": "3" * 64,
        "requirement_policy_release": "requirements-0303540000-v1",
        "requirement_policy_manifest_sha256": "4" * 64,
        "coverage_matrix_sha256": "5" * 64,
        "requirement_policy_approval_sha256": "6" * 64,
        "reviewer_trust_store_sha256": "7" * 64,
    }
    v3["runtime_release_v3_sha256"] = canonical_digest(v3)
    return base, v3


def _release_pin() -> AgentReleasePin:
    provider_revision = "d" * 40
    provider_source_manifest = "f" * 64
    provider_retrieval_schema = "a" * 64
    provider_build = canonical_digest(
        {
            "contract": "evidence-retrieval",
            "schema_version": "1.0",
            "provider_release": "evidence-api-v1.2.0",
            "provider_revision": provider_revision,
            "schema_sha256": provider_retrieval_schema,
            "source_manifest_sha256": provider_source_manifest,
        }
    )
    provider = EvidenceProviderContractPin(
        provider_release="evidence-api-v1.2.0",
        provider_revision=provider_revision,
        provider_build_sha256=provider_build,
        provider_source_manifest_sha256=provider_source_manifest,
        retrieval_schema_sha256=provider_retrieval_schema,
        readiness_schema_sha256="b" * 64,
    )
    base_release, v3_release = _evidence_release_payloads(
        provider,
        snapshot_id="generation-1",
    )
    return AgentReleasePin(
        build_sha="a" * 40,
        wheel_sha256="b" * 64,
        source_snapshot_sha256="a" * 64,
        package_version="0.4.0",
        agent_code_snapshot_sha256="d" * 64,
        model_id="claude-test",
        model_revision="claude-test-revision",
        extraction_prompt_sha256="c" * 64,
        extraction_schema_sha256="e" * 64,
        coordinator_prompt_sha256="1" * 64,
        narrative_prompt_sha256="2" * 64,
        chat_prompt_sha256="3" * 64,
        coordinator_schema_sha256="4" * 64,
        narrative_schema_sha256="5" * 64,
        chat_schema_sha256="6" * 64,
        policy_id="policy-v3",
        tool_registry_id="tools-v1",
        assertion_trust_store_sha256="a" * 64,
        evidence_provider_contract=provider,
        evidence_snapshot_id="generation-1",
        evidence_runtime_release_sha256=base_release["runtime_release_sha256"],
        evidence_runtime_v3_sha256=v3_release["runtime_release_v3_sha256"],
        release_profile_id="profile-1",
        release_profile_sha256="7" * 64,
        release_profile_subject_sha256="8" * 64,
        release_profile_evaluation_report_sha256="9" * 64,
        reviewer_trust_store_sha256="0" * 64,
    )


def test_exact_provider_schema_bytes_are_packaged_and_enforced() -> None:
    lock = validate_agent_contract_bundle()
    package_root = resources.files("customs_backend")
    for path, expected_bytes, expected_sha256 in [
        (
            lock.review_request_v3_path,
            lock.review_request_v3_bytes,
            lock.review_request_v3_sha256,
        ),
        (
            lock.review_result_v3_path,
            lock.review_result_v3_bytes,
            lock.review_result_v3_sha256,
        ),
    ]:
        raw = package_root.joinpath(*path.split("/")).read_bytes()
        assert len(raw) == expected_bytes
        assert hashlib.sha256(raw).hexdigest() == expected_sha256

    request = AgentReviewRequest(
        review_run_id="run-contract",
        case_id="case-contract",
        documents=[
            AgentDocumentInput(
                document_id="doc-contract",
                filename="invoice.txt",
                content_text="INVOICE",
            )
        ],
        review_as_of=date(2026, 7, 22),
        assertion_cutoff_at=datetime(2026, 7, 22, tzinfo=UTC),
        trade_type="import",
    )
    validate_agent_v3_payload("review_request", request.to_agent_wire())
    result = build_agent_v3_result(request)
    validate_agent_v3_payload("review_result", result)
    result["provider_injected_field"] = "forbidden"
    with pytest.raises(AgentContractPayloadError):
        validate_agent_v3_payload("review_result", result)


@pytest.mark.parametrize(
    "field",
    [
        "agent_package_version",
        "agent_git_sha",
        "agent_source_snapshot_sha256",
        "agent_code_snapshot_sha256",
        "model_id",
        "model_revision",
        "extraction_prompt_sha256",
        "extraction_schema_sha256",
        "coordinator_prompt_sha256",
        "narrative_prompt_sha256",
        "chat_prompt_sha256",
        "coordinator_schema_sha256",
        "narrative_schema_sha256",
        "chat_schema_sha256",
        "result_schema_sha256",
        "policy_id",
        "tool_registry_id",
        "assertion_trust_store_sha256",
        "assertion_trust_store_state",
        "evidence_snapshot_id",
        "evidence_runtime_v3_sha256",
        "release_profile_id",
        "release_profile_state",
        "release_profile_sha256",
        "release_profile_subject_sha256",
        "release_profile_evaluation_report_sha256",
        "reviewer_trust_store_sha256",
        "kill_switch_active",
    ],
)
def test_release_pin_compares_every_runtime_identity_field(field: str) -> None:
    pin = _release_pin()
    identity = pin.runtime_identity()
    assert pin.matches_runtime(identity)
    original = getattr(identity, field)
    if isinstance(original, bool):
        changed = not original
    elif field == "release_profile_state":
        changed = "unreleased"
    elif isinstance(original, str) and len(original) == 64:
        changed = ("f" if original[0] != "f" else "e") + original[1:]
    else:
        changed = f"{original}-drift"
    drifted = identity.model_construct(**{**identity.__dict__, field: changed})
    assert not pin.matches_runtime(drifted)


def _released_metadata(pin: AgentReleasePin) -> dict:
    lock = load_agent_contract_lock()
    evidence_release, evidence_v3_release = _evidence_release_payloads(
        pin.evidence_provider_contract,
        snapshot_id=pin.evidence_snapshot_id,
    )
    assert evidence_release["runtime_release_sha256"] == pin.evidence_runtime_release_sha256
    assert evidence_v3_release["runtime_release_v3_sha256"] == pin.evidence_runtime_v3_sha256
    return {
        "agent": {
            "build_sha": pin.build_sha,
            "package_version": pin.package_version,
            "source_snapshot_sha256": pin.source_snapshot_sha256,
            "wheel_sha256": pin.wheel_sha256,
        },
        "capabilities": [
            "text",
            "bounded_planning_shadow",
            "bounded_active_executor_injectable",
            "citation_verified_narrative",
            "grounded_case_answer",
            "immutable_revision",
        ],
        "agentic_limits": {
            "max_cycles": 2,
            "max_tool_calls": 6,
            "ocr_enabled": False,
            "planner_mode": "active",
            "active_execution_authorized_by_release": True,
            "active_tool_handlers_registered": True,
            "trusted_assertion_verifier_configured": True,
            "effective_execution_mode": "active",
        },
        "transport_limits": {
            "max_body_bytes": 2 * 1024 * 1024,
            "max_documents": 6,
            "max_text_characters": 500_000,
            "text_only": True,
            "empty_visual_placeholders_accepted": True,
        },
        "automatic_completion_release": {
            "profile_id": pin.release_profile_id,
            "release_state": "approved",
            "kill_switch_active": False,
            "bounded_adaptive_active_enabled": True,
            "profile_sha256": pin.release_profile_sha256,
            "review_subject_sha256": pin.release_profile_subject_sha256,
            "model_revision": pin.model_revision,
            "evaluation_report_sha256": pin.release_profile_evaluation_report_sha256,
            "reviewer_trust_store": {
                "release_state": "approved",
                "trust_store_sha256": pin.reviewer_trust_store_sha256,
                "trusted_reviewer_count": 2,
            },
            "assertion_trust_store": {
                "release_state": "active",
                "trust_domain": "customs-ai-agent/supporting-document-verification/v1",
                "trust_store_sha256": pin.assertion_trust_store_sha256,
                "active_key_count": 1,
            },
            "production_admission": {"admitted": True, "reason_code": None},
        },
        "evidence_provider_contract": pin.evidence_provider_contract.model_dump(mode="json"),
        "evidence_runtime_release": {
            "release_state": "released",
            "identity": evidence_release,
        },
        "evidence_runtime_release_v3": {
            "release_state": "released",
            "identity": evidence_v3_release,
        },
        "model": {
            "id": pin.model_id,
            "prompt_id": "customs_document_analysis",
            "prompt_sha256": pin.extraction_prompt_sha256,
            "prompt_version": "1.2.0",
        },
        "runtime_release": pin.runtime_identity().model_dump(mode="json"),
        "schemas": {
            "service_openapi_sha256": lock.service_openapi_sha256,
            "review_request_v2_sha256": "4" * 64,
            "review_result_v2_sha256": "5" * 64,
            "review_request_v3_sha256": lock.review_request_v3_sha256,
            "review_result_v3_sha256": lock.review_result_v3_sha256,
        },
        "service_api_version": lock.service_api_version,
    }


def _unreleased_metadata(pin: AgentServicePin) -> dict:
    lock = load_agent_contract_lock()
    runtime_release = {
        "agent_package_version": pin.package_version,
        "agent_git_sha": pin.build_sha,
        "agent_source_snapshot_sha256": pin.source_snapshot_sha256,
        "agent_code_snapshot_sha256": pin.agent_code_snapshot_sha256,
        "model_id": pin.model_id,
        "model_revision": None,
        "extraction_prompt_sha256": pin.extraction_prompt_sha256,
        "extraction_schema_sha256": pin.extraction_schema_sha256,
        "coordinator_prompt_sha256": pin.coordinator_prompt_sha256,
        "narrative_prompt_sha256": pin.narrative_prompt_sha256,
        "chat_prompt_sha256": pin.chat_prompt_sha256,
        "coordinator_schema_sha256": pin.coordinator_schema_sha256,
        "narrative_schema_sha256": pin.narrative_schema_sha256,
        "chat_schema_sha256": pin.chat_schema_sha256,
        "result_schema_sha256": lock.review_result_v3_sha256,
        "policy_id": pin.policy_id,
        "tool_registry_id": pin.tool_registry_id,
        "assertion_trust_store_sha256": pin.assertion_trust_store_sha256,
        "assertion_trust_store_state": pin.assertion_trust_store_release_state,
        "evidence_snapshot_id": None,
        "evidence_runtime_v3_sha256": None,
        "release_profile_id": "unreleased",
        "release_profile_state": "unreleased",
        "release_profile_sha256": None,
        "release_profile_subject_sha256": None,
        "release_profile_evaluation_report_sha256": None,
        "reviewer_trust_store_sha256": None,
        "kill_switch_active": True,
    }
    return {
        "agent": {
            "build_sha": pin.build_sha,
            "package_version": pin.package_version,
            "source_snapshot_sha256": pin.source_snapshot_sha256,
            "wheel_sha256": pin.wheel_sha256,
        },
        "capabilities": [
            "text",
            "bounded_planning_shadow",
            "bounded_active_executor_injectable",
            "citation_verified_narrative",
            "grounded_case_answer",
            "immutable_revision",
        ],
        "agentic_limits": {
            "max_cycles": 2,
            "max_tool_calls": 6,
            "ocr_enabled": False,
            "planner_mode": "shadow",
            "active_execution_authorized_by_release": False,
            "active_tool_handlers_registered": True,
            "trusted_assertion_verifier_configured": (
                pin.assertion_trust_store_release_state == "active"
            ),
            "effective_execution_mode": "shadow",
        },
        "transport_limits": {
            "max_body_bytes": 2 * 1024 * 1024,
            "max_documents": 6,
            "max_text_characters": 500_000,
            "text_only": True,
            "empty_visual_placeholders_accepted": True,
        },
        "automatic_completion_release": {
            "profile_id": "unreleased",
            "release_state": "unreleased",
            "kill_switch_active": True,
            "bounded_adaptive_active_enabled": False,
            "profile_sha256": None,
            "review_subject_sha256": None,
            "model_revision": None,
            "evaluation_report_sha256": None,
            "reviewer_trust_store": {
                "release_state": "unreleased",
                "trust_store_sha256": "0" * 64,
                "trusted_reviewer_count": 0,
            },
            "assertion_trust_store": {
                "release_state": pin.assertion_trust_store_release_state,
                "trust_domain": "customs-ai-agent/supporting-document-verification/v1",
                "trust_store_sha256": pin.assertion_trust_store_sha256,
                "active_key_count": (
                    1 if pin.assertion_trust_store_release_state == "active" else 0
                ),
            },
            "production_admission": {
                "admitted": False,
                "reason_code": "release_profile_unreleased",
            },
        },
        "evidence_provider_contract": pin.evidence_provider_contract.model_dump(mode="json"),
        "evidence_runtime_release": {"release_state": "unreleased"},
        "evidence_runtime_release_v3": {"release_state": "unreleased"},
        "model": {
            "id": pin.model_id,
            "prompt_id": "customs_document_analysis",
            "prompt_sha256": pin.extraction_prompt_sha256,
            "prompt_version": "1.2.0",
        },
        "runtime_release": runtime_release,
        "schemas": {
            "service_openapi_sha256": lock.service_openapi_sha256,
            "review_request_v2_sha256": "4" * 64,
            "review_result_v2_sha256": "5" * 64,
            "review_request_v3_sha256": lock.review_request_v3_sha256,
            "review_result_v3_sha256": lock.review_result_v3_sha256,
        },
        "service_api_version": lock.service_api_version,
    }


def test_release_pin_requires_exact_full_metadata_and_active_policy() -> None:
    pin = _release_pin()
    metadata = _released_metadata(pin)
    assert pin.validate_metadata(metadata) is metadata

    extra = json.loads(json.dumps(metadata))
    extra["untrusted_extension"] = True
    with pytest.raises(ReleaseConfigurationError, match="shape"):
        pin.validate_metadata(extra)

    shadow = json.loads(json.dumps(metadata))
    shadow["agentic_limits"]["effective_execution_mode"] = "shadow"
    with pytest.raises(ReleaseConfigurationError, match="exact release pin"):
        pin.validate_metadata(shadow)

    drifted_runtime = json.loads(json.dumps(metadata))
    drifted_runtime["runtime_release"]["chat_prompt_sha256"] = "f" * 64
    with pytest.raises(ReleaseConfigurationError, match="exact release pin"):
        pin.validate_metadata(drifted_runtime)

    drifted_provider = json.loads(json.dumps(metadata))
    provider = drifted_provider["evidence_provider_contract"]
    provider["provider_revision"] = "c" * 40
    provider["provider_build_sha256"] = canonical_digest(
        {
            "contract": "evidence-retrieval",
            "schema_version": provider["schema_version"],
            "provider_release": provider["provider_release"],
            "provider_revision": provider["provider_revision"],
            "schema_sha256": provider["retrieval_schema_sha256"],
            "source_manifest_sha256": provider["provider_source_manifest_sha256"],
        }
    )
    with pytest.raises(ReleaseConfigurationError, match="exact release pin"):
        pin.validate_metadata(drifted_provider)

    drifted_evidence = json.loads(json.dumps(metadata))
    drifted_evidence["evidence_runtime_release"]["identity"]["model_manifest_sha256"] = "0" * 64
    with pytest.raises(ReleaseConfigurationError, match="shape"):
        pin.validate_metadata(drifted_evidence)

    valid_but_unpinned_evidence = json.loads(json.dumps(metadata))
    base_identity = valid_but_unpinned_evidence["evidence_runtime_release"]["identity"]
    base_identity["model_manifest_sha256"] = "0" * 64
    base_identity["runtime_release_sha256"] = canonical_digest(
        {key: value for key, value in base_identity.items() if key != "runtime_release_sha256"}
    )
    v3_identity = valid_but_unpinned_evidence["evidence_runtime_release_v3"]["identity"]
    v3_identity["base_runtime_release"] = json.loads(json.dumps(base_identity))
    v3_identity["runtime_release_v3_sha256"] = canonical_digest(
        {key: value for key, value in v3_identity.items() if key != "runtime_release_v3_sha256"}
    )
    with pytest.raises(ReleaseConfigurationError, match="exact release pin"):
        pin.validate_metadata(valid_but_unpinned_evidence)


def _candidate_settings(pin: AgentServicePin, **overrides) -> Settings:
    values = {
        "agent_expected_build_sha": pin.build_sha,
        "agent_expected_wheel_sha256": pin.wheel_sha256,
        "agent_expected_source_snapshot_sha256": pin.source_snapshot_sha256,
        "agent_expected_package_version": pin.package_version,
        "agent_expected_code_snapshot_sha256": pin.agent_code_snapshot_sha256,
        "agent_expected_model_id": pin.model_id,
        "agent_expected_extraction_prompt_sha256": pin.extraction_prompt_sha256,
        "agent_expected_extraction_schema_sha256": pin.extraction_schema_sha256,
        "agent_expected_coordinator_prompt_sha256": pin.coordinator_prompt_sha256,
        "agent_expected_narrative_prompt_sha256": pin.narrative_prompt_sha256,
        "agent_expected_chat_prompt_sha256": pin.chat_prompt_sha256,
        "agent_expected_coordinator_schema_sha256": pin.coordinator_schema_sha256,
        "agent_expected_narrative_schema_sha256": pin.narrative_schema_sha256,
        "agent_expected_chat_schema_sha256": pin.chat_schema_sha256,
        "agent_expected_policy_id": pin.policy_id,
        "agent_expected_tool_registry_id": pin.tool_registry_id,
        "agent_expected_assertion_trust_store_release_state": (
            pin.assertion_trust_store_release_state
        ),
        "agent_expected_assertion_trust_store_sha256": pin.assertion_trust_store_sha256,
        "agent_expected_evidence_provider_release": (
            pin.evidence_provider_contract.provider_release
        ),
        "agent_expected_evidence_provider_revision": (
            pin.evidence_provider_contract.provider_revision
        ),
        "agent_expected_evidence_provider_build_sha256": (
            pin.evidence_provider_contract.provider_build_sha256
        ),
        "agent_expected_evidence_provider_source_manifest_sha256": (
            pin.evidence_provider_contract.provider_source_manifest_sha256
        ),
        "agent_expected_evidence_provider_retrieval_schema_sha256": (
            pin.evidence_provider_contract.retrieval_schema_sha256
        ),
        "agent_expected_evidence_provider_readiness_schema_sha256": (
            pin.evidence_provider_contract.readiness_schema_sha256
        ),
        **overrides,
    }
    return Settings(**values)


def test_honest_unreleased_candidate_starts_but_full_readiness_remains_closed() -> None:
    pin = _release_pin().service_pin()
    metadata = _unreleased_metadata(pin)
    assert pin.validate_unreleased_metadata(metadata) is metadata

    settings = _candidate_settings(
        pin,
        oidc_issuer="https://issuer.example",
        oidc_audience="customs-workbench-api",
        oidc_jwks_url="https://issuer.example/.well-known/jwks.json",
    )
    container = build_container(settings)
    agent = CallableAgentClient(lambda _request: {}, metadata=metadata)
    container.agent = agent
    container.service.agent = agent
    assert container.service.service_pin == pin
    assert container.service.release_pin is None

    client = TestClient(create_app(container=container, settings=settings))
    assert client.get("/health/live").status_code == 200
    # Code/provider identity is genuine, but unsealed Data V2/V3 is not review-ready.
    assert client.get("/health/ready").status_code == 503


def test_unreleased_candidate_metadata_drift_fails_closed() -> None:
    pin = _release_pin().service_pin()
    drifted = _unreleased_metadata(pin)
    drifted["runtime_release"]["coordinator_prompt_sha256"] = "0" * 64
    with pytest.raises(ReleaseConfigurationError, match="candidate pin"):
        pin.validate_unreleased_metadata(drifted)


def test_local_uat_metadata_preserves_the_exact_candidate_code_pin() -> None:
    pin = _release_pin().service_pin()
    metadata = _unreleased_metadata(pin)
    metadata.update(
        {
            "status": "ready",
            "service_mode": "local_user_uat_diagnostic",
            "assurance_level": "diagnostic",
            "uat": {
                "diagnostic_only": True,
                "mock_fallback_allowed": False,
                "auto_completion_allowed": False,
                "evidence_mode": "synthetic-v3",
            },
        }
    )
    metadata["automatic_completion_release"]["uat_forced_disabled"] = True
    metadata["evidence_runtime_release_v3"] = {
        "release_state": "diagnostic",
        "identity": {"diagnostic_manifest_sha256": "f" * 64},
    }
    assert pin.validate_local_uat_metadata(metadata) is metadata

    drifted = json.loads(json.dumps(metadata))
    drifted["agent"]["build_sha"] = "0" * 40
    with pytest.raises(ReleaseConfigurationError, match="candidate pin"):
        pin.validate_local_uat_metadata(drifted)


def test_candidate_pin_cannot_enable_automatic_completion() -> None:
    pin = _release_pin().service_pin()
    with pytest.raises(ValueError, match="release approval"):
        WorkbenchService(
            repository=InMemoryWorkbenchRepository(),
            object_store=InMemoryObjectStore(),
            publisher=InMemoryJobPublisher(),
            agent=CallableAgentClient(lambda _request: {}),
            auto_completion_enabled=True,
            approved_profile_id="profile-1",
            service_pin=pin,
        )


def test_production_candidate_settings_do_not_require_fabricated_release_values() -> None:
    pin = _release_pin().service_pin()
    settings = _candidate_settings(
        pin,
        environment="production",
        public_base_url="https://customs.internal.example",
        database_url=("postgresql+psycopg://customs_runtime:secret@operations-db:5432/customs"),
        oidc_issuer="https://customs.internal.example/oidc/realms/customs",
        oidc_audience="customs-workbench-api",
        oidc_jwks_url="http://keycloak:8080/oidc/realms/customs/protocol/openid-connect/certs",
        oidc_client_id="customs-workbench-spa",
        oidc_authorization_endpoint=(
            "https://customs.internal.example/oidc/realms/customs/protocol/openid-connect/auth"
        ),
        oidc_token_endpoint=(
            "https://customs.internal.example/oidc/realms/customs/protocol/openid-connect/token"
        ),
        agent_service_token="not-a-real-secret",
        redis_url="redis://redis:6379/0",
        minio_endpoint="minio:9000",
        minio_access_key="not-a-real-access-key",
        minio_secret_key="not-a-real-secret-key",
    )
    assert settings.auto_completion_enabled is False
    assert settings.agent_expected_model_revision is None
    assert settings.agent_expected_evidence_runtime_v3_sha256 is None

    with pytest.raises(ValueError, match="approved profile"):
        Settings(
            **{
                **settings.model_dump(mode="python"),
                "auto_completion_enabled": True,
            }
        )
    with pytest.raises(ValueError, match="production runtime database_url must use"):
        Settings(
            **{
                **settings.model_dump(mode="python"),
                "database_url": (
                    "postgresql+psycopg://customs_migrator:secret@operations-db:5432/customs"
                ),
            }
        )
    with pytest.raises(ValueError, match="production migration database_url must use"):
        Settings(
            **{
                **settings.model_dump(mode="python"),
                "process_role": "migration",
                "database_url": (
                    "postgresql+psycopg://customs_runtime:secret@operations-db:5432/customs"
                ),
            }
        )


def test_production_migration_role_requires_only_the_distinct_database_principal() -> None:
    settings = Settings(
        environment="production",
        process_role="migration",
        database_url=("postgresql+psycopg://customs_migrator:secret@operations-db:5432/customs"),
    )
    assert settings.process_role == "migration"
    assert settings.oidc_issuer is None
    assert settings.agent_service_token is None
    assert settings.minio_secret_key is None

    with pytest.raises(ValueError, match="migration configuration requires database_url"):
        Settings(environment="production", process_role="migration")


def test_production_outbox_role_requires_only_runtime_database_and_redis() -> None:
    settings = Settings(
        environment="production",
        process_role="outbox",
        database_url=("postgresql+psycopg://customs_runtime:secret@operations-db:5432/customs"),
        redis_url="redis://redis:6379/0",
    )
    assert settings.process_role == "outbox"
    assert settings.oidc_issuer is None
    assert settings.agent_service_token is None
    assert settings.minio_secret_key is None

    with pytest.raises(ValueError, match="outbox configuration requires"):
        Settings(environment="production", process_role="outbox")
    with pytest.raises(ValueError, match="outbox database_url must use customs_runtime"):
        Settings(
            environment="production",
            process_role="outbox",
            database_url=(
                "postgresql+psycopg://customs_migrator:secret@operations-db:5432/customs"
            ),
            redis_url="redis://redis:6379/0",
        )


def _trust_store(
    keys: list[tuple[str, Ed25519PrivateKey]], *, now: datetime
) -> DomainReviewerTrustStore:
    payload = {
        "schema_version": "backend-domain-reviewer-trust-store/v1",
        "reviewers": [
            {
                "reviewer_id": reviewer_id,
                "public_key_ed25519_base64": _public_key(key),
                "valid_from": _timestamp(now - timedelta(days=30)),
                "valid_until": _timestamp(now + timedelta(days=30)),
                "status": "active",
            }
            for reviewer_id, key in sorted(keys)
        ],
        "trust_store_digest": "0" * 64,
    }
    payload["trust_store_digest"] = canonical_digest(payload)
    return DomainReviewerTrustStore.model_validate(payload)


def _approval(
    *,
    trust_store: DomainReviewerTrustStore,
    signing_keys: list[tuple[str, Ed25519PrivateKey]],
    issued_at: datetime,
    expires_at: datetime,
    pin: AgentReleasePin | None = None,
) -> AutoCompletionApprovalManifest:
    actual_pin = pin or _release_pin()
    subject = {
        "schema_version": "backend-auto-completion-subject/v1",
        "profile_id": "profile-1",
        "backend_decision_policy_release": "backend-human-decision-policy/v1",
        "adjudication_sha256": "e" * 64,
        "locked_holdout_report_sha256": "f" * 64,
        "holdout_case_count": 500,
        "incorrect_or_unsafe_auto_completion_count": 0,
        "eligible_clean_completion_recall": 0.95,
        "kill_switch_verification_sha256": "9" * 64,
        "release_pin": actual_pin.model_dump(mode="json"),
        "agent_contract_lock": load_agent_contract_lock().model_dump(mode="json"),
        "trust_store_digest": trust_store.trust_store_digest,
        "issued_at": _timestamp(issued_at),
        "expires_at": _timestamp(expires_at),
    }
    subject_bytes = _canonical_bytes(subject)
    payload = {
        "schema_version": "backend-auto-completion-approval/v2",
        "profile_id": "profile-1",
        "backend_decision_policy_release": "backend-human-decision-policy/v1",
        "adjudication_sha256": "e" * 64,
        "locked_holdout_report_sha256": "f" * 64,
        "holdout_case_count": 500,
        "incorrect_or_unsafe_auto_completion_count": 0,
        "eligible_clean_completion_recall": 0.95,
        "kill_switch_verification_sha256": "9" * 64,
        "release_pin": actual_pin.model_dump(mode="json"),
        "trust_store_digest": trust_store.trust_store_digest,
        "issued_at": _timestamp(issued_at),
        "expires_at": _timestamp(expires_at),
        "approval_subject_sha256": canonical_digest(subject),
        "reviewer_signatures": [
            {
                "reviewer_id": reviewer_id,
                "signature_ed25519_base64": base64.b64encode(key.sign(subject_bytes)).decode(
                    "ascii"
                ),
            }
            for reviewer_id, key in sorted(signing_keys)
        ],
        "approval_digest": "0" * 64,
    }
    payload["approval_digest"] = canonical_digest(payload)
    return AutoCompletionApprovalManifest.model_validate(payload)


def _recompute_manifest_digest(payload: dict) -> dict:
    payload["approval_digest"] = "0" * 64
    payload["approval_digest"] = canonical_digest(payload)
    return payload


def test_two_independent_trusted_ed25519_signatures_are_required() -> None:
    now = datetime.now(UTC)
    key_a = Ed25519PrivateKey.generate()
    key_b = Ed25519PrivateKey.generate()
    trust_store = _trust_store([("reviewer-a", key_a), ("reviewer-b", key_b)], now=now)
    approval = _approval(
        trust_store=trust_store,
        signing_keys=[("reviewer-a", key_a), ("reviewer-b", key_b)],
        issued_at=now - timedelta(minutes=1),
        expires_at=now + timedelta(days=1),
    )
    assert approval.verify_with_trust_store(trust_store, now=now) is approval

    duplicate = approval.model_dump(mode="json")
    duplicate["reviewer_signatures"] = [
        duplicate["reviewer_signatures"][0],
        duplicate["reviewer_signatures"][0],
    ]
    with pytest.raises(ValidationError, match="unique, sorted"):
        AutoCompletionApprovalManifest.model_validate(_recompute_manifest_digest(duplicate))


def test_unknown_invalid_expired_and_subject_drift_fail_closed() -> None:
    now = datetime.now(UTC)
    key_a = Ed25519PrivateKey.generate()
    key_b = Ed25519PrivateKey.generate()
    trust_store = _trust_store([("reviewer-a", key_a), ("reviewer-b", key_b)], now=now)

    unknown = _approval(
        trust_store=trust_store,
        signing_keys=[("reviewer-a", key_a), ("reviewer-z", key_b)],
        issued_at=now - timedelta(minutes=1),
        expires_at=now + timedelta(days=1),
    )
    with pytest.raises(ReleaseConfigurationError, match="unknown"):
        unknown.verify_with_trust_store(trust_store, now=now)

    valid = _approval(
        trust_store=trust_store,
        signing_keys=[("reviewer-a", key_a), ("reviewer-b", key_b)],
        issued_at=now - timedelta(minutes=1),
        expires_at=now + timedelta(days=1),
    )
    invalid = valid.model_dump(mode="json")
    signature = base64.b64decode(invalid["reviewer_signatures"][0]["signature_ed25519_base64"])
    invalid["reviewer_signatures"][0]["signature_ed25519_base64"] = base64.b64encode(
        bytes([signature[0] ^ 1]) + signature[1:]
    ).decode("ascii")
    invalid_manifest = AutoCompletionApprovalManifest.model_validate(
        _recompute_manifest_digest(invalid)
    )
    with pytest.raises(ReleaseConfigurationError, match="signature is invalid"):
        invalid_manifest.verify_with_trust_store(trust_store, now=now)

    expired = _approval(
        trust_store=trust_store,
        signing_keys=[("reviewer-a", key_a), ("reviewer-b", key_b)],
        issued_at=now - timedelta(days=2),
        expires_at=now - timedelta(days=1),
    )
    with pytest.raises(ReleaseConfigurationError, match="not currently valid"):
        expired.verify_with_trust_store(trust_store, now=now)

    drifted = valid.model_dump(mode="json")
    drifted["profile_id"] = "profile-drifted"
    with pytest.raises(ValidationError, match="subject has drifted"):
        AutoCompletionApprovalManifest.model_validate(_recompute_manifest_digest(drifted))

    for field, unsafe_value in {
        "holdout_case_count": 499,
        "incorrect_or_unsafe_auto_completion_count": 1,
        "eligible_clean_completion_recall": 0.949,
    }.items():
        below_gate = valid.model_dump(mode="json")
        below_gate[field] = unsafe_value
        with pytest.raises(ValidationError):
            AutoCompletionApprovalManifest.model_validate(_recompute_manifest_digest(below_gate))


def test_packaged_trust_root_is_empty_and_blocks_runtime_self_assertion() -> None:
    now = datetime.now(UTC)
    packaged = load_domain_reviewer_trust_store()
    assert packaged.reviewers == []
    key_a = Ed25519PrivateKey.generate()
    key_b = Ed25519PrivateKey.generate()
    self_asserted = _approval(
        trust_store=packaged,
        signing_keys=[("reviewer-a", key_a), ("reviewer-b", key_b)],
        issued_at=now - timedelta(minutes=1),
        expires_at=now + timedelta(days=1),
    )
    with pytest.raises(ReleaseConfigurationError, match="unknown"):
        WorkbenchService(
            repository=InMemoryWorkbenchRepository(),
            object_store=InMemoryObjectStore(),
            publisher=InMemoryJobPublisher(),
            agent=CallableAgentClient(lambda _request: {}),
            auto_completion_enabled=True,
            approved_profile_id="profile-1",
            service_pin=_release_pin().service_pin(),
            release_pin=_release_pin(),
            auto_completion_approval=self_asserted,
        )


@pytest.mark.parametrize(
    "raw",
    [
        b'{"schema_version":"x","schema_version":"y"}',
        b'{"schema_version":"x","value":NaN}',
        b"{" + b" " * 262_144 + b"}",
    ],
    ids=["duplicate", "nan", "oversize"],
)
def test_approval_file_parser_rejects_duplicate_nan_and_oversize(tmp_path, raw: bytes) -> None:
    path = tmp_path / "approval.json"
    path.write_bytes(raw)
    with pytest.raises(ReleaseConfigurationError):
        load_auto_completion_approval(
            str(path),
            expected_digest="0" * 64,
            expected_profile_id="profile-1",
            expected_release_pin=_release_pin(),
            expected_backend_decision_policy_release="backend-human-decision-policy/v1",
        )
