from __future__ import annotations

import base64
import hashlib
import json
from datetime import date
from pathlib import Path

import pytest
import customs_backend.api as api_module
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from tests.product.test_workbench_api import workbench as workbench

from customs_backend.assertion_signing import (
    ASSERTION_TRUST_DOMAIN,
    AssertionSigningKeyRegistryV1,
    RegistrySupportingAssertionSigner,
    RequirementPolicyIdentity,
    AssertionSigningConfigurationError,
    load_local_uat_assertion_signer,
    load_requirement_policy_resolver,
)
from customs_backend.api import build_container
from customs_backend.adapters import CallableAgentClient, InMemoryJobPublisher, InMemoryObjectStore
from customs_backend.repository import InMemoryWorkbenchRepository
from customs_backend.domain import (
    SupportingDocumentRecord,
    canonical_digest,
    canonical_json_bytes,
)
from customs_backend.release import (
    AgentReleasePin,
    EvidenceProviderContractPin,
    ReleaseConfigurationError,
)
from customs_backend.settings import Settings


def _b64(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).decode("ascii").rstrip("=")


def _write_local_uat_signer_registry(
    root: Path,
    *,
    authority_id: str = (
        "backend:principal-sha256:6cc4551495f1ec8da505fdeaa4f34c17eeb8c234bcaeb6ca184f92a0cdd6318d"
    ),
    valid_from: str = "2026-01-01",
    valid_until: str = "2027-12-31",
) -> tuple[Path, str]:
    private = Ed25519PrivateKey.generate()
    private_bytes = private.private_bytes(
        encoding=serialization.Encoding.Raw,
        format=serialization.PrivateFormat.Raw,
        encryption_algorithm=serialization.NoEncryption(),
    )
    public_bytes = private.public_key().public_bytes(
        encoding=serialization.Encoding.Raw,
        format=serialization.PublicFormat.Raw,
    )
    trust_sha256 = "d" * 64
    payload = {
        "schema_version": "customs-backend-local-uat-assertion-signing-keys/v1",
        "test_only": True,
        "production_eligible": False,
        "loopback_only": True,
        "trust_domain": ASSERTION_TRUST_DOMAIN,
        "agent_trust_store_release_state": "active",
        "agent_trust_store_sha256": trust_sha256,
        "keys": [
            {
                "authority_id": authority_id,
                "authority_release": "backend-human-decision-policy/v1",
                "key_id": "local-uat-approver-key-v1",
                "private_key_base64url": _b64(private_bytes),
                "public_key_base64url": _b64(public_bytes),
                "valid_from": valid_from,
                "valid_until": valid_until,
                "receipt_ttl_seconds": 86_400,
            }
        ],
    }
    path = root / "_local" / "secrets" / "uat" / "assertion-signing-key-registry.json"
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path, trust_sha256


def test_local_uat_signer_loader_accepts_only_the_exact_active_uat_approver(
    tmp_path: Path,
) -> None:
    path, trust_sha256 = _write_local_uat_signer_registry(tmp_path)
    signer = load_local_uat_assertion_signer(
        path,
        expected_trust_store_sha256=trust_sha256,
        today=date(2026, 7, 24),
    )
    assert signer.ready is True
    assert signer.trust_store_sha256 == trust_sha256

    wrong_path, _ = _write_local_uat_signer_registry(
        tmp_path / "wrong",
        authority_id=(
            "backend:principal-sha256:" + hashlib.sha256(b"not-uat-approver").hexdigest()
        ),
    )
    with pytest.raises(
        AssertionSigningConfigurationError,
        match="release artifact is invalid",
    ):
        load_local_uat_assertion_signer(
            wrong_path,
            expected_trust_store_sha256=trust_sha256,
            today=date(2026, 7, 24),
        )

    with pytest.raises(
        AssertionSigningConfigurationError,
        match="active Agent trust",
    ):
        load_local_uat_assertion_signer(
            path,
            expected_trust_store_sha256=trust_sha256,
            today=date(2028, 1, 1),
        )


def test_local_uat_signer_rejects_retired_workspace_secret_root(tmp_path: Path) -> None:
    path, trust_sha256 = _write_local_uat_signer_registry(tmp_path)
    retired = tmp_path / "_secrets" / "uat" / path.name
    retired.parent.mkdir(parents=True)
    retired.write_bytes(path.read_bytes())

    with pytest.raises(
        AssertionSigningConfigurationError,
        match="fixture path is invalid",
    ):
        load_local_uat_assertion_signer(
            retired,
            expected_trust_store_sha256=trust_sha256,
            today=date(2026, 7, 24),
        )


class _PinnedPolicyResolver:
    @property
    def ready(self) -> bool:
        return True

    def resolve(self, document: SupportingDocumentRecord) -> RequirementPolicyIdentity | None:
        if document.document_code != "IMPORT_REQUIREMENT_CERT":
            return None
        return RequirementPolicyIdentity(
            policy_source_sha256="b" * 64,
            policy_release="requirements-release-v1",
            evidence_runtime_v3_sha256="c" * 64,
        )


def _write_signed_data_release(root: Path) -> tuple[dict[str, Path], AgentReleasePin]:
    private_keys = [Ed25519PrivateKey.generate(), Ed25519PrivateKey.generate()]
    public_keys = [
        key.public_key().public_bytes(
            encoding=serialization.Encoding.Raw,
            format=serialization.PublicFormat.Raw,
        )
        for key in private_keys
    ]
    trust = {
        "contract": "evidence-domain-reviewer-trust-store",
        "schema_version": "1.0",
        "trust_store_release": "reviewer-trust-v1",
        "valid_from": "2026-01-01",
        "valid_until": "2030-12-31",
        "keys": [
            {
                "reviewer_id": f"reviewer-{index}",
                "key_id": f"reviewer-{index}-key-v1",
                "role": "customs_domain_reviewer",
                "algorithm": "ed25519",
                "public_key_hex": public.hex(),
                "valid_from": "2026-01-01",
                "valid_until": "2030-12-31",
                "revoked": False,
            }
            for index, public in enumerate(public_keys, start=1)
        ],
    }
    trust_sha = canonical_digest(trust)
    policies = [
        {
            "required_document_code": "IMPORT_REQUIREMENT_CERT",
            "verification_policy": "human_only",
            "policy_source_sha256": "b" * 64,
        }
    ]
    policy_manifest = canonical_digest(policies)
    reviewer_claims = [
        {
            "reviewer_id": f"reviewer-{index}",
            "key_id": f"reviewer-{index}-key-v1",
            "review_reference_sha256": str(index) * 64,
        }
        for index in (1, 2)
    ]
    subject = canonical_digest(
        {
            "domain": "customs-evidence/requirement-policy-approval/v1",
            "contract": "requirement-verification-policy",
            "schema_version": "1.0",
            "policy_release": "requirements-0303540000-v1",
            "knowledge_source_manifest_sha256": "c" * 64,
            "knowledge_snapshot_id": "generation-1",
            "coverage_matrix_sha256": "d" * 64,
            "policy_manifest_sha256": policy_manifest,
            "adjudication_sha256": "e" * 64,
            "reviewer_claims": reviewer_claims,
            "trust_store_release": "reviewer-trust-v1",
            "reviewer_trust_store_sha256": trust_sha,
            "approved_at": "2026-01-01",
            "valid_until": "2030-12-31",
        }
    )
    signature_message = b"customs-evidence/requirement-policy-approval/v1\x00" + bytes.fromhex(
        subject
    )
    approval = {
        "reviewer_attestations": [
            {
                **claim,
                "signature_hex": key.sign(signature_message).hex(),
            }
            for claim, key in zip(reviewer_claims, private_keys, strict=True)
        ],
        "adjudication_sha256": "e" * 64,
        "subject_sha256": subject,
        "trust_store_release": "reviewer-trust-v1",
        "reviewer_trust_store_sha256": trust_sha,
        "approved_at": "2026-01-01",
        "valid_until": "2030-12-31",
    }
    policy = {
        "contract": "requirement-verification-policy",
        "schema_version": "1.0",
        "release_state": "approved",
        "policy_release": "requirements-0303540000-v1",
        "knowledge_source_manifest_sha256": "c" * 64,
        "knowledge_snapshot_id": "generation-1",
        "coverage_matrix_sha256": "d" * 64,
        "policy_manifest_sha256": policy_manifest,
        "approval_sha256": canonical_digest(approval),
        "approval": approval,
        "policies": policies,
    }
    verified_approval = canonical_digest(
        {
            "domain": "customs-evidence/verified-policy-approval/v1",
            "policy_approval_sha256": policy["approval_sha256"],
            "reviewer_trust_store_sha256": trust_sha,
        }
    )
    provider_revision = "f" * 40
    provider_source = "1" * 64
    retrieval_schema = "2" * 64
    provider_build = canonical_digest(
        {
            "contract": "evidence-retrieval",
            "schema_version": "1.0",
            "provider_release": "evidence-api-v1.2.0",
            "provider_revision": provider_revision,
            "schema_sha256": retrieval_schema,
            "source_manifest_sha256": provider_source,
        }
    )
    provider = EvidenceProviderContractPin(
        provider_release="evidence-api-v1.2.0",
        provider_revision=provider_revision,
        provider_build_sha256=provider_build,
        provider_source_manifest_sha256=provider_source,
        retrieval_schema_sha256=retrieval_schema,
        readiness_schema_sha256="3" * 64,
    )
    base_runtime = {
        "contract": "evidence-runtime-release",
        "schema_version": "2.0",
        "retrieval_schema_version": "1.0",
        "provider_release": provider.provider_release,
        "provider_revision": provider.provider_revision,
        "provider_build_sha256": provider.provider_build_sha256,
        "retrieval_schema_sha256": provider.retrieval_schema_sha256,
        "provider_source_manifest_sha256": provider.provider_source_manifest_sha256,
        "knowledge_generation_id": "4" * 64,
        "knowledge_generation_attestation_sha256": "5" * 64,
        "knowledge_source_manifest_sha256": "c" * 64,
        "model_manifest_sha256": "6" * 64,
        "index_config_sha256": "7" * 64,
        "evaluation_report_sha256": "8" * 64,
        "seal_attestation_sha256": "9" * 64,
        "knowledge_snapshot_id": "generation-1",
        "supported_review_as_of": "2026-07-22",
    }
    base_runtime["runtime_release_sha256"] = canonical_digest(base_runtime)
    runtime = {
        "contract": "evidence-runtime-release",
        "schema_version": "3.0",
        "retrieval_schema_version": "3.0",
        "base_runtime_release": base_runtime,
        "retrieval_schema_sha256": "a" * 64,
        "readiness_schema_sha256": "b" * 64,
        "requirement_policy_schema_sha256": "c" * 64,
        "requirement_policy_release": policy["policy_release"],
        "requirement_policy_manifest_sha256": policy_manifest,
        "coverage_matrix_sha256": policy["coverage_matrix_sha256"],
        "requirement_policy_approval_sha256": verified_approval,
        "reviewer_trust_store_sha256": trust_sha,
    }
    runtime["runtime_release_v3_sha256"] = canonical_digest(runtime)
    paths = {
        "policy": root / "requirement-policy.json",
        "trust": root / "trusted-reviewers.json",
        "runtime": root / "evidence-runtime-v3.json",
    }
    for key, payload in (("policy", policy), ("trust", trust), ("runtime", runtime)):
        paths[key].write_text(json.dumps(payload), encoding="utf-8", newline="\n")
    pin = AgentReleasePin(
        build_sha="a" * 40,
        wheel_sha256="b" * 64,
        source_snapshot_sha256="c" * 64,
        package_version="0.4.0",
        agent_code_snapshot_sha256="d" * 64,
        model_id="claude-test",
        model_revision="claude-test-revision",
        extraction_prompt_sha256="e" * 64,
        extraction_schema_sha256="f" * 64,
        coordinator_prompt_sha256="1" * 64,
        narrative_prompt_sha256="2" * 64,
        chat_prompt_sha256="3" * 64,
        coordinator_schema_sha256="4" * 64,
        narrative_schema_sha256="5" * 64,
        chat_schema_sha256="6" * 64,
        policy_id="policy-v3",
        tool_registry_id="tools-v1",
        assertion_trust_store_sha256="7" * 64,
        evidence_provider_contract=provider,
        evidence_snapshot_id="generation-1",
        evidence_runtime_release_sha256=base_runtime["runtime_release_sha256"],
        evidence_runtime_v3_sha256=runtime["runtime_release_v3_sha256"],
        release_profile_id="profile-1",
        release_profile_sha256="8" * 64,
        release_profile_subject_sha256="9" * 64,
        release_profile_evaluation_report_sha256="a" * 64,
        reviewer_trust_store_sha256="b" * 64,
    )
    return paths, pin


def test_server_derived_policy_and_enrolled_key_produce_agent_verifiable_receipt(
    workbench,
) -> None:
    client, container = workbench
    private = Ed25519PrivateKey.generate()
    private_bytes = private.private_bytes(
        encoding=serialization.Encoding.Raw,
        format=serialization.PrivateFormat.Raw,
        encryption_algorithm=serialization.NoEncryption(),
    )
    public_bytes = private.public_key().public_bytes(
        encoding=serialization.Encoding.Raw,
        format=serialization.PublicFormat.Raw,
    )
    authority_id = "backend:principal-sha256:" + hashlib.sha256(b"approver-1").hexdigest()
    registry = AssertionSigningKeyRegistryV1.model_validate(
        {
            "schema_version": "customs-backend-assertion-signing-keys/v1",
            "trust_domain": ASSERTION_TRUST_DOMAIN,
            "agent_trust_store_release_state": "active",
            "agent_trust_store_sha256": "d" * 64,
            "keys": [
                {
                    "authority_id": authority_id,
                    "authority_release": "backend-human-decision-policy/v1",
                    "key_id": "approver-1-key-v1",
                    "private_key_base64url": _b64(private_bytes),
                    "public_key_base64url": _b64(public_bytes),
                    "valid_from": "2025-01-01",
                    "valid_until": "2027-12-31",
                    "receipt_ttl_seconds": 86400,
                }
            ],
        }
    )
    container.service.assertion_signer = RegistrySupportingAssertionSigner(registry)
    container.service.requirement_policy_resolver = _PinnedPolicyResolver()

    case = client.post(
        "/api/v1/cases",
        json={"title": "signed support"},
        headers={"Authorization": "Bearer reviewer-token"},
    ).json()
    source = client.post(
        f"/api/v1/cases/{case['case_id']}/documents",
        json={
            "document_type": "SUPPORTING",
            "filename": "permit.txt",
            "content_text": "server-bound support",
        },
        headers={"Authorization": "Bearer reviewer-token"},
    ).json()["record"]
    client.post(
        f"/api/v1/cases/{case['case_id']}/supporting-documents",
        json={
            "document_id": source["document_id"],
            "document_code": "IMPORT_REQUIREMENT_CERT",
            "issuer": "TEST ISSUER",
            "document_number": "P-1",
            "issued_on": date(2026, 7, 1).isoformat(),
            "valid_until": date(2027, 1, 1).isoformat(),
            "subject_item_ids": ["item-1"],
            "hs_codes": ["0303540000"],
            "source_sha256": source["content_sha256"],
        },
        headers={"Authorization": "Bearer reviewer-token"},
    )
    response = client.post(
        f"/api/v1/cases/{case['case_id']}/supporting-documents/{source['document_id']}/assertions",
        json={"method": "human_approver"},
        headers={"Authorization": "Bearer approver-token"},
    )
    assert response.status_code == 201, response.text
    assertion = response.json()["verification_assertions"][0]
    receipt = assertion["receipt"]
    assert receipt["policy_source_sha256"] == "b" * 64
    assert receipt["policy_release"] == "requirements-release-v1"
    assert assertion["decision_record"]["policy_source_sha256"] == "b" * 64
    signature = base64.urlsafe_b64decode(receipt["signature_base64url"] + "==")
    unsigned = {key: value for key, value in receipt.items() if key != "signature_base64url"}
    private.public_key().verify(
        signature,
        ASSERTION_TRUST_DOMAIN.encode("ascii") + b"\x00" + canonical_json_bytes(unsigned),
    )

    untrusted_policy = client.post(
        f"/api/v1/cases/{case['case_id']}/supporting-documents/{source['document_id']}/assertions",
        json={
            "method": "human_approver",
            "policy_source_sha256": "f" * 64,
            "policy_release": "browser-controlled",
        },
        headers={"Authorization": "Bearer approver-token"},
    )
    assert untrusted_policy.status_code == 422


def test_default_empty_trust_root_preserves_unsigned_human_assertion(workbench) -> None:
    client, _container = workbench
    case = client.post(
        "/api/v1/cases",
        json={"title": "unsigned diagnostic"},
        headers={"Authorization": "Bearer reviewer-token"},
    ).json()
    source = client.post(
        f"/api/v1/cases/{case['case_id']}/documents",
        json={
            "document_type": "SUPPORTING",
            "filename": "permit.txt",
            "content_text": "diagnostic support",
        },
        headers={"Authorization": "Bearer reviewer-token"},
    ).json()["record"]
    client.post(
        f"/api/v1/cases/{case['case_id']}/supporting-documents",
        json={
            "document_id": source["document_id"],
            "document_code": "IMPORT_REQUIREMENT_CERT",
            "issuer": "TEST ISSUER",
            "document_number": "P-2",
            "issued_on": "2026-07-01",
            "subject_item_ids": ["item-1"],
            "hs_codes": ["0303540000"],
            "source_sha256": source["content_sha256"],
        },
        headers={"Authorization": "Bearer reviewer-token"},
    )
    response = client.post(
        f"/api/v1/cases/{case['case_id']}/supporting-documents/{source['document_id']}/assertions",
        json={"method": "human_approver"},
        headers={"Authorization": "Bearer approver-token"},
    )
    assert response.status_code == 201
    assertion = response.json()["verification_assertions"][0]
    assert assertion["receipt"] is None
    assert assertion["decision_record"] is None


def test_data_signed_policy_release_is_verified_and_runtime_bound(tmp_path: Path) -> None:
    paths, pin = _write_signed_data_release(tmp_path)
    resolver = load_requirement_policy_resolver(
        policy_path=paths["policy"],
        trust_store_path=paths["trust"],
        runtime_v3_path=paths["runtime"],
        expected_runtime_v3_sha256=pin.evidence_runtime_v3_sha256,
    )
    assert resolver.ready is True

    payload = json.loads(paths["policy"].read_text(encoding="utf-8"))
    payload["approval"]["reviewer_attestations"][0]["signature_hex"] = "0" * 128
    paths["policy"].write_text(json.dumps(payload), encoding="utf-8", newline="\n")
    with pytest.raises(AssertionSigningConfigurationError, match="policy|signature"):
        load_requirement_policy_resolver(
            policy_path=paths["policy"],
            trust_store_path=paths["trust"],
            runtime_v3_path=paths["runtime"],
            expected_runtime_v3_sha256=pin.evidence_runtime_v3_sha256,
        )


def test_production_container_verifies_policy_but_signer_absence_keeps_gate_closed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    paths, pin = _write_signed_data_release(tmp_path)
    service = pin.service_pin()
    settings = Settings(
        environment="production",
        process_role="runtime",
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
        agent_service_token="test-service-token",
        agent_expected_build_sha=service.build_sha,
        agent_expected_wheel_sha256=service.wheel_sha256,
        agent_expected_source_snapshot_sha256=service.source_snapshot_sha256,
        agent_expected_package_version=service.package_version,
        agent_expected_code_snapshot_sha256=service.agent_code_snapshot_sha256,
        agent_expected_model_id=service.model_id,
        agent_expected_extraction_prompt_sha256=service.extraction_prompt_sha256,
        agent_expected_extraction_schema_sha256=service.extraction_schema_sha256,
        agent_expected_coordinator_prompt_sha256=service.coordinator_prompt_sha256,
        agent_expected_narrative_prompt_sha256=service.narrative_prompt_sha256,
        agent_expected_chat_prompt_sha256=service.chat_prompt_sha256,
        agent_expected_coordinator_schema_sha256=service.coordinator_schema_sha256,
        agent_expected_narrative_schema_sha256=service.narrative_schema_sha256,
        agent_expected_chat_schema_sha256=service.chat_schema_sha256,
        agent_expected_policy_id=service.policy_id,
        agent_expected_tool_registry_id=service.tool_registry_id,
        agent_expected_assertion_trust_store_release_state=(
            service.assertion_trust_store_release_state
        ),
        agent_expected_assertion_trust_store_sha256=service.assertion_trust_store_sha256,
        agent_expected_evidence_provider_release=(
            service.evidence_provider_contract.provider_release
        ),
        agent_expected_evidence_provider_revision=(
            service.evidence_provider_contract.provider_revision
        ),
        agent_expected_evidence_provider_build_sha256=(
            service.evidence_provider_contract.provider_build_sha256
        ),
        agent_expected_evidence_provider_source_manifest_sha256=(
            service.evidence_provider_contract.provider_source_manifest_sha256
        ),
        agent_expected_evidence_provider_retrieval_schema_sha256=(
            service.evidence_provider_contract.retrieval_schema_sha256
        ),
        agent_expected_evidence_provider_readiness_schema_sha256=(
            service.evidence_provider_contract.readiness_schema_sha256
        ),
        agent_expected_model_revision=pin.model_revision,
        agent_expected_evidence_snapshot_id=pin.evidence_snapshot_id,
        agent_expected_evidence_runtime_release_sha256=(pin.evidence_runtime_release_sha256),
        agent_expected_evidence_runtime_v3_sha256=pin.evidence_runtime_v3_sha256,
        agent_expected_release_profile_id=pin.release_profile_id,
        agent_expected_release_profile_sha256=pin.release_profile_sha256,
        agent_expected_release_profile_subject_sha256=pin.release_profile_subject_sha256,
        agent_expected_release_profile_evaluation_report_sha256=(
            pin.release_profile_evaluation_report_sha256
        ),
        agent_expected_reviewer_trust_store_sha256=pin.reviewer_trust_store_sha256,
        requirement_policy_release_path=str(paths["policy"]),
        requirement_policy_trust_store_path=str(paths["trust"]),
        evidence_runtime_v3_release_path=str(paths["runtime"]),
        redis_url="redis://redis:6379/0",
        minio_endpoint="minio:9000",
        minio_access_key="test-access",
        minio_secret_key="test-secret",
    )
    monkeypatch.setattr(
        api_module,
        "PostgresWorkbenchRepository",
        lambda *_args, **_kwargs: InMemoryWorkbenchRepository(),
    )
    monkeypatch.setattr(
        api_module,
        "MinioObjectStore",
        lambda *_args, **_kwargs: InMemoryObjectStore(),
    )
    monkeypatch.setattr(
        api_module,
        "DramatiqJobPublisher",
        lambda *_args, **_kwargs: InMemoryJobPublisher(),
    )
    monkeypatch.setattr(
        api_module,
        "HttpAgentClient",
        lambda *_args, **_kwargs: CallableAgentClient(lambda _request: {}),
    )
    container = build_container(settings)
    assert container.service.requirement_policy_resolver.ready is True
    assert container.service.assertion_signer.ready is False
    # A worker can verify/read the release without owning a private signing key;
    # forcing the feature flag in this test still cannot cross the approval gate.
    container.service.auto_completion_enabled = True
    with pytest.raises(ReleaseConfigurationError, match="approval is unavailable"):
        container.service.assert_auto_completion_ready()
