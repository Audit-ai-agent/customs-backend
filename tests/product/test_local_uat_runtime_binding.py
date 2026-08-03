from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest

from customs_backend.api import _binding_requires_static_service_pin
from customs_backend.domain import (
    AgentDocumentInput,
    AgentReviewRequest,
    AgentReviewResultV3,
    ReleaseIdentityV3,
    canonical_digest,
)
from customs_backend.ports import ConflictError
from customs_backend.release import (
    LocalUatRuntimeBindingV1,
    ReleaseConfigurationError,
    load_local_uat_runtime_binding,
)
from customs_backend.service import (
    _expected_parent_extraction_reuse_key,
    _revision_document_model_call_counts,
)
from customs_backend.settings import Settings


def _binding_payload(*, now: datetime, mode: str = "development") -> dict[str, object]:
    payload: dict[str, object] = {
        "schema_version": "customs-local-uat-runtime-binding/v1",
        "mode": mode,
        "issued_at_utc": now.isoformat().replace("+00:00", "Z"),
        "expires_at_utc": (now + timedelta(hours=1)).isoformat().replace("+00:00", "Z"),
        "loopback_origin": "http://127.0.0.1:18088",
        "agent": {
            "commit": "1" * 40,
            "working_tree_sha256": "2" * 64,
            "wheel_sha256": "3" * 64,
            "code_snapshot_sha256": "4" * 64,
            "model_id": "claude-test",
            "prompt_bundle_sha256": "5" * 64,
            "schema_bundle_sha256": "6" * 64,
            "tool_registry_id": "customs-bounded-tools/v1",
            "tool_registry_sha256": "7" * 64,
            "meaning_catalog_sha256": "8" * 64,
        },
        "evidence_runtime_v3_sha256": "9" * 64,
        "capability_sha256": "a" * 64,
        "capability_file_sha256": "b" * 64,
        "mock_fallback_allowed": False,
        "auto_completion_allowed": False,
    }
    payload["binding_sha256"] = canonical_digest(payload)
    return payload


def test_local_uat_runtime_binding_loads_one_exact_active_file(tmp_path) -> None:
    now = datetime(2026, 8, 3, 4, 0, tzinfo=UTC)
    raw = json.dumps(
        _binding_payload(now=now),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode()
    path = tmp_path / "runtime-binding-v1.json"
    path.write_bytes(raw)

    binding = load_local_uat_runtime_binding(
        path,
        expected_file_sha256=hashlib.sha256(raw).hexdigest(),
        now=now + timedelta(minutes=1),
    )

    assert binding.mode == "development"
    assert binding.agent.commit == "1" * 40
    with pytest.raises(ReleaseConfigurationError, match="file SHA-256 differs"):
        load_local_uat_runtime_binding(path, expected_file_sha256="f" * 64, now=now)
    with pytest.raises(ReleaseConfigurationError, match="expired"):
        load_local_uat_runtime_binding(
            path,
            expected_file_sha256=hashlib.sha256(raw).hexdigest(),
            now=now + timedelta(hours=2),
        )


def test_local_uat_runtime_binding_is_rejected_outside_local_runtime() -> None:
    with pytest.raises(ValueError, match="configured together"):
        Settings(uat_runtime_binding_path="C:/binding.json")
    with pytest.raises(ValueError, match="restricted to local_uat runtime"):
        Settings(
            environment="production",
            uat_runtime_binding_path="C:/binding.json",
            uat_runtime_binding_sha256="a" * 64,
        )


def test_development_binding_does_not_require_static_release_pin() -> None:
    now = datetime(2026, 8, 3, 4, 0, tzinfo=UTC)
    development = LocalUatRuntimeBindingV1.model_validate(_binding_payload(now=now))
    candidate = LocalUatRuntimeBindingV1.model_validate(_binding_payload(now=now, mode="candidate"))

    assert _binding_requires_static_service_pin(None) is False
    assert _binding_requires_static_service_pin(development) is False
    assert _binding_requires_static_service_pin(candidate) is True


def test_service_metadata_keeps_case_evidence_and_uat_tool_identity_separate() -> None:
    now = datetime(2026, 8, 3, 4, 0, tzinfo=UTC)
    binding = LocalUatRuntimeBindingV1.model_validate(_binding_payload(now=now))
    metadata_runtime = ReleaseIdentityV3.model_construct(
        agent_git_sha=binding.agent.commit,
        agent_source_snapshot_sha256=binding.agent.working_tree_sha256,
        agent_code_snapshot_sha256=binding.agent.code_snapshot_sha256,
        model_id=binding.agent.model_id,
        evidence_runtime_v3_sha256=None,
        tool_registry_id="customs-agent-tools/text-v1",
        kill_switch_active=True,
    )

    assert binding.matches_service_metadata_runtime(metadata_runtime) is True
    assert binding.matches_result_runtime(metadata_runtime) is False


def test_incremental_revision_requires_exact_parent_provenance() -> None:
    parent_calls = [
        {
            "document_id": "ci-1",
            "provider": "anthropic",
            "model_id": "claude-test",
        }
    ]
    parent = {"model_calls": parent_calls, "document_results": [{"document_id": "ci-1"}]}
    event = {
        "stage": "extract_documents",
        "event": "parent_extraction_reused",
        "message": "verified parent extraction reused",
        "metadata": {
            "parent_result_sha256": "c" * 64,
            "aggregate_reuse_key_sha256": "d" * 64,
            "reused_document_count": 1,
            "inherited_model_call_count": 1,
            "actual_document_model_call_count": 0,
        },
    }
    envelope = AgentReviewResultV3.model_construct(
        audit_trail=[event],
        model_calls=parent_calls,
        document_results=[{"document_id": "ci-1"}],
    )
    job = SimpleNamespace(
        kind="revision",
        request={"parent_provider_result_sha256": "c" * 64},
    )

    assert _revision_document_model_call_counts(
        envelope=envelope,
        job=job,
        parent_result=parent,
        expected_reuse_key_sha256="d" * 64,
    ) == (0, 1)

    tampered = envelope.model_copy(deep=True)
    tampered.audit_trail[0]["metadata"]["inherited_model_call_count"] = 2
    with pytest.raises(ConflictError, match="differs from its parent"):
        _revision_document_model_call_counts(
            envelope=tampered,
            job=job,
            parent_result=parent,
        )


def test_parent_reuse_key_hashes_agent_semantic_document_defaults() -> None:
    runtime = ReleaseIdentityV3(
        agent_package_version="0.3.0",
        agent_git_sha="1" * 40,
        agent_source_snapshot_sha256="2" * 64,
        agent_code_snapshot_sha256="3" * 64,
        model_id="claude-test",
        model_revision="2026-08-03",
        extraction_prompt_sha256="4" * 64,
        extraction_schema_sha256="5" * 64,
        coordinator_prompt_sha256="6" * 64,
        narrative_prompt_sha256="7" * 64,
        chat_prompt_sha256="8" * 64,
        coordinator_schema_sha256="9" * 64,
        narrative_schema_sha256="a" * 64,
        chat_schema_sha256="b" * 64,
        result_schema_sha256="c" * 64,
        policy_id="customs-agent-policy/v1",
        tool_registry_id="customs-bounded-tools/v1",
        assertion_trust_store_sha256="d" * 64,
        assertion_trust_store_state="unreleased",
        evidence_snapshot_id="snapshot-test",
        evidence_runtime_v3_sha256="e" * 64,
        release_profile_id="unreleased-test",
        release_profile_state="unreleased",
        kill_switch_active=True,
    )
    request = AgentReviewRequest(
        review_run_id="run-parent",
        case_id="case-parent",
        documents=[
            AgentDocumentInput(
                document_id="ci-1",
                filename="invoice.txt",
                content_text="INVOICE NO: CI-2026-0001",
                declared_type="invoice",
            )
        ],
        review_as_of=datetime(2026, 8, 3, tzinfo=UTC).date(),
        assertion_cutoff_at=datetime(2026, 8, 3, 4, 0, tzinfo=UTC),
        trade_type="import",
    )
    extraction = {"document_id": "ci-1", "document_type": "invoice", "fields": []}
    parent_result = {
        "result_sha256": "f" * 64,
        "runtime_release": runtime.model_dump(mode="json"),
        "document_results": [extraction],
    }
    manifest_sha256 = "0" * 64
    semantic_document = request.to_agent_manifest_wire()["documents"][0]
    transport_document = request.documents[0].to_agent_wire()
    extraction_identity = {
        "agent_code_snapshot_sha256": runtime.agent_code_snapshot_sha256,
        "model_id": runtime.model_id,
        "model_revision": runtime.model_revision,
        "extraction_prompt_sha256": runtime.extraction_prompt_sha256,
        "extraction_schema_sha256": runtime.extraction_schema_sha256,
    }

    assert "image_pages" not in transport_document
    assert "pdf_pages" not in transport_document
    assert semantic_document["image_pages"] == []
    assert semantic_document["pdf_pages"] is None
    expected = canonical_digest(
        {
            "schema_version": "parent-extraction-reuse/v1",
            "parent_result_sha256": parent_result["result_sha256"],
            "parent_input_manifest_sha256": manifest_sha256,
            "extraction_identity": extraction_identity,
            "documents": [
                {
                    "document_id": "ci-1",
                    "input_sha256": canonical_digest(semantic_document),
                    "extraction_sha256": canonical_digest(extraction),
                }
            ],
        }
    )
    transport_only = canonical_digest(
        {
            "schema_version": "parent-extraction-reuse/v1",
            "parent_result_sha256": parent_result["result_sha256"],
            "parent_input_manifest_sha256": manifest_sha256,
            "extraction_identity": extraction_identity,
            "documents": [
                {
                    "document_id": "ci-1",
                    "input_sha256": canonical_digest(transport_document),
                    "extraction_sha256": canonical_digest(extraction),
                }
            ],
        }
    )

    assert (
        _expected_parent_extraction_reuse_key(
            parent_result=parent_result,
            parent_request=request,
            parent_input_manifest_sha256=manifest_sha256,
            child_runtime=runtime,
        )
        == expected
    )
    assert expected != transport_only


def test_full_review_reports_actual_calls_without_reuse_attestation() -> None:
    envelope = AgentReviewResultV3.model_construct(
        audit_trail=[],
        model_calls=[{"document_id": "ci-1"}, {"document_id": "pl-1"}],
        document_results=[],
    )
    assert _revision_document_model_call_counts(
        envelope=envelope,
        job=SimpleNamespace(kind="review", request={}),
        parent_result=None,
    ) == (2, 0)
