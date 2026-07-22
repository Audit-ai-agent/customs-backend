from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from threading import Event
from types import SimpleNamespace

import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi.testclient import TestClient
from tests.product.agent_v3_fixture import build_agent_v3_result

from customs_backend.adapters import (
    CallableAgentClient,
    InMemoryJobPublisher,
    InMemoryObjectStore,
)
from customs_backend.api import Container, create_app
from customs_backend.auth import OidcJwtVerifier, StaticTokenVerifier
from customs_backend.domain import (
    AgentAnswerResponse,
    AgentReviewRequest,
    HumanDecisionCreate,
    Principal,
    Role,
    canonical_digest,
)
from customs_backend.ports import AuthorizationError, ConflictError
from customs_backend.repository import InMemoryWorkbenchRepository
from customs_backend.service import WorkbenchService
from customs_backend.settings import Settings


def _agent_result(
    request: AgentReviewRequest,
    *,
    validation_issues: list[dict] | None = None,
    runtime_release: dict | None = None,
) -> dict:
    result = build_agent_v3_result(request)
    if validation_issues:
        result["validation_issues"] = validation_issues
    if runtime_release:
        result["runtime_release"] = runtime_release
    result["result_sha256"] = "0" * 64
    result["result_sha256"] = canonical_digest(result)
    return result


def _rehash_agent_result(result: dict) -> dict:
    result["result_sha256"] = "0" * 64
    result["result_sha256"] = canonical_digest(result)
    return result


def _agent_result_with_audited_claude_calls(request: AgentReviewRequest) -> dict:
    result = build_agent_v3_result(request)
    runtime = result["runtime_release"]
    result["model_calls"] = [
        {
            "document_id": document.document_id,
            "provider": "anthropic",
            "model_id": runtime["model_id"],
            "prompt_id": "customs_document_analysis",
            "prompt_version": "1.2.0",
            "request_id": f"provider-request-sensitive-{index}",
            "message_id": f"provider-message-sensitive-{index}",
            "stop_reason": "end_turn",
            "input_tokens": 100 + index,
            "output_tokens": 50 + index,
            "cache_creation_input_tokens": 0,
            "cache_read_input_tokens": 0,
            "latency_ms": 10,
            "prompt_sha256": runtime["extraction_prompt_sha256"],
            "output_schema_id": "document-analysis",
            "output_schema_version": "1.0",
            "output_schema_sha256": runtime["extraction_schema_sha256"],
            "input_modality": "text",
        }
        for index, document in enumerate(request.documents)
    ]
    return _rehash_agent_result(result)


def _agent_result_with_bound_issue_and_finding(
    request: AgentReviewRequest, *, duplicate_issue_fact: bool = False
) -> dict:
    result = build_agent_v3_result(request)
    issue = {
        "issue_code": "invoice_number_mismatch",
        "severity": "medium",
        "message": "Invoice 번호가 일치하지 않습니다.",
    }
    finding = {
        "finding_code": "supporting_document_required",
        "severity": "low",
        "message": "요건서류를 확인해야 합니다.",
        "item_id": "item-1",
        "required_document_code": "IMPORT_REQUIREMENT_CERT",
        "required_document_name": "수입요건확인서",
        "evidence_ids": ["evidence-1"],
        "confidence": 1.0,
    }
    issue_reference = f"issue_{canonical_digest({'index': 0, 'issue': issue})}"
    finding_reference = f"finding_{canonical_digest({'index': 0, 'finding': finding})}"

    def fact(kind: str, reference_id: str, label: str, value: str) -> dict:
        payload = {
            "kind": kind,
            "reference_id": reference_id,
            "label": label,
            "value": value,
            "document_id": None,
        }
        return {
            "fact_id": f"fact_{canonical_digest(payload)}",
            **payload,
            "source_sha256": canonical_digest(payload),
        }

    issue_fact = fact("issue", issue_reference, issue["issue_code"], issue["message"])
    entries = [
        *result["fact_ledger"]["entries"],
        issue_fact,
        fact("finding", finding_reference, finding["finding_code"], finding["message"]),
    ]
    if duplicate_issue_fact:
        entries.append(dict(issue_fact))
    result["fact_ledger"] = {
        "ledger_id": f"ledger_{canonical_digest(entries)}",
        "entries": entries,
    }
    result["validation_issues"] = [issue]
    result["regulatory_findings"] = [finding]
    result["assessment"] = "issues_detected"
    result["status"] = "needs_review"
    result["requires_human_review"] = True
    result["review_disposition"] = "human_review_required"
    result["assurance_level"] = "production_candidate"
    result["completion_eligibility"] = {
        "eligible": False,
        "profile_id": None,
        "scope": [],
        "unmet_reason_codes": ["issues_detected"],
    }
    result["result_sha256"] = "0" * 64
    result["result_sha256"] = canonical_digest(result)
    return result


@pytest.fixture
def workbench():
    repository = InMemoryWorkbenchRepository()
    objects = InMemoryObjectStore()
    publisher = InMemoryJobPublisher()
    agent = CallableAgentClient(
        _agent_result,
        answer=lambda run, _question, question_id: AgentAnswerResponse(
            question_id=question_id,
            answer="현재 심사 결과에 문제는 없습니다.",
            status="answered",
            citations=[
                {
                    "kind": run["fact_ledger"]["entries"][0]["kind"],
                    "reference_id": run["fact_ledger"]["entries"][0]["reference_id"],
                }
            ],
            generation_mode="deterministic_fallback",
        ),
        metadata={"status": "ready", "release": "agent-v3"},
    )
    principals = {
        "reviewer-token": Principal(
            subject="reviewer-1", org_id="org-a", roles=frozenset({Role.reviewer})
        ),
        "approver-token": Principal(
            subject="approver-1", org_id="org-a", roles=frozenset({Role.approver})
        ),
        "auditor-token": Principal(
            subject="auditor-1", org_id="org-a", roles=frozenset({Role.auditor})
        ),
        "admin-token": Principal(subject="admin-1", org_id="org-a", roles=frozenset({Role.admin})),
        "other-org-token": Principal(
            subject="reviewer-2", org_id="org-b", roles=frozenset({Role.reviewer})
        ),
        "unassigned-token": Principal(
            subject="reviewer-3", org_id="org-a", roles=frozenset({Role.reviewer})
        ),
    }
    verifier = StaticTokenVerifier(principals)
    settings = Settings(
        environment="test",
        oidc_issuer="https://id.example.invalid/realms/customs",
        oidc_audience="customs-workbench-api",
        oidc_jwks_url="https://id.example.invalid/jwks",
        oidc_client_id="customs-workbench-spa",
        oidc_authorization_endpoint="https://id.example.invalid/auth",
        oidc_token_endpoint="https://id.example.invalid/token",
        oidc_end_session_endpoint="https://id.example.invalid/logout",
    )
    service = WorkbenchService(
        repository=repository,
        object_store=objects,
        publisher=publisher,
        agent=agent,
        auto_completion_enabled=False,
    )
    container = Container(
        settings=settings,
        repository=repository,
        object_store=objects,
        publisher=publisher,
        agent=agent,
        token_verifier=verifier,
        service=service,
    )
    app = create_app(container=container, settings=settings)
    return TestClient(app), container


def _auth(token: str = "reviewer-token") -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def _create_case_and_document(client: TestClient) -> tuple[dict, dict]:
    case = client.post(
        "/api/v1/cases",
        json={"title": "정상 수입 심사"},
        headers=_auth(),
    ).json()
    document = client.post(
        f"/api/v1/cases/{case['case_id']}/documents",
        json={
            "document_type": "CI",
            "filename": "invoice.txt",
            "content_text": "HS CODE: 0303540000\nTOTAL: USD 12000",
        },
        headers=_auth(),
    ).json()
    return case, document


def _create_case_and_supporting_source(client: TestClient) -> tuple[dict, dict]:
    case = client.post(
        "/api/v1/cases",
        json={"title": "supporting document boundary"},
        headers=_auth(),
    ).json()
    document = client.post(
        f"/api/v1/cases/{case['case_id']}/documents",
        json={
            "document_type": "SUPPORTING",
            "filename": "permit.txt",
            "content_text": "TEST IMPORT REQUIREMENT CERTIFICATE",
        },
        headers=_auth(),
    ).json()
    return case, document


def test_auth_config_and_session_are_explicit_pkce_resource_server_contract(workbench):
    client, _container = workbench
    config_response = client.get("/api/v1/auth/config")
    assert config_response.headers["cache-control"] == "no-store"
    assert config_response.headers["pragma"] == "no-cache"
    config = config_response.json()
    assert config["flow"] == "authorization_code_pkce_s256"
    assert config["token_endpoint"].endswith("/token")
    assert config["token_storage"] == "memory_only"
    assert client.get("/api/v1/session").status_code == 401
    session_response = client.get("/api/v1/session", headers=_auth())
    assert session_response.headers["cache-control"] == "no-store"
    assert session_response.headers["pragma"] == "no-cache"
    session = session_response.json()
    assert session == {"subject": "reviewer-1", "org_id": "org-a", "roles": ["reviewer"]}


def test_single_org_rbac_and_text_only_boundary(workbench):
    client, _container = workbench
    assert (
        client.post(
            "/api/v1/cases", json={"title": "forbidden"}, headers=_auth("auditor-token")
        ).status_code
        == 403
    )
    case, _document = _create_case_and_document(client)
    assert (
        client.get(f"/api/v1/cases/{case['case_id']}", headers=_auth("other-org-token")).status_code
        == 404
    )
    rejected = client.post(
        f"/api/v1/cases/{case['case_id']}/documents",
        json={
            "document_type": "CI",
            "filename": "invoice.pdf",
            "content_text": "not released",
        },
        headers=_auth(),
    )
    assert rejected.status_code == 422

    oversized_text = client.post(
        f"/api/v1/cases/{case['case_id']}/documents",
        json={
            "document_type": "CI",
            "filename": "oversized.txt",
            "content_text": "x" * 200_001,
        },
        headers=_auth(),
    )
    assert oversized_text.status_code == 422


def test_public_request_body_limit_matches_reverse_proxy(workbench):
    client, _container = workbench
    response = client.post(
        "/api/v1/cases",
        content=b"x" * (4 * 1024 * 1024 + 1),
        headers={**_auth(), "Content-Type": "application/json"},
    )
    assert response.status_code == 413
    assert response.headers["content-type"].startswith("application/problem+json")
    assert response.headers["cache-control"] == "no-store"
    assert response.headers["pragma"] == "no-cache"
    assert response.json()["code"] == "request_body_too_large"
    assert response.json()["status"] == 413


def test_documents_are_hash_verified_and_returned_only_through_backend(workbench):
    client, _container = workbench
    case, document = _create_case_and_document(client)
    response = client.get(f"/api/v1/cases/{case['case_id']}/documents", headers=_auth()).json()
    assert response["items"][0]["content_text"] is None
    content = client.get(
        f"/api/v1/cases/{case['case_id']}/documents/{document['record']['document_id']}/content",
        headers=_auth(),
    )
    assert content.status_code == 200
    assert content.json()["content_text"].startswith("HS CODE")
    assert response["items"][0]["record"]["content_sha256"] == document["record"]["content_sha256"]
    assert "agent_service_token" not in str(response)


def test_enqueue_is_atomic_and_idempotent_and_outbox_is_not_duplicated(workbench):
    client, container = workbench
    case, _document = _create_case_and_document(client)
    url = f"/api/v1/cases/{case['case_id']}/reviews"
    headers = {**_auth(), "Idempotency-Key": "review-key-0001"}
    first = client.post(url, json={"trade_type": "import"}, headers=headers)
    second = client.post(url, json={"trade_type": "import"}, headers=headers)
    assert first.status_code == second.status_code == 202
    assert first.json()["job"]["job_id"] == second.json()["job"]["job_id"]
    assert second.json()["idempotency_reused"] is True
    assert container.publisher.messages == []
    assert len(container.repository.list_pending_outbox()) == 1
    assert container.service.dispatch_pending_outbox() == 1
    assert len(container.publisher.messages) == 1
    assert container.repository.list_pending_outbox() == []
    changed = client.post(url, json={"trade_type": "export"}, headers=headers)
    assert changed.status_code == 409


def test_logical_request_uniqueness_reuses_active_job_and_scopes_actor(workbench):
    client, container = workbench
    case, _document = _create_case_and_document(client)
    url = f"/api/v1/cases/{case['case_id']}/reviews"
    first = client.post(
        url,
        json={},
        headers={**_auth(), "Idempotency-Key": "logical-request-first"},
    )
    same_logical = client.post(
        url,
        json={},
        headers={**_auth(), "Idempotency-Key": "logical-request-second"},
    )
    assert first.status_code == same_logical.status_code == 202
    assert same_logical.json()["idempotency_reused"] is True
    assert first.json()["job"]["job_id"] == same_logical.json()["job"]["job_id"]
    assert (
        container.service.get_case(
            Principal(subject="admin-1", org_id="org-a", roles=frozenset({Role.admin})),
            case["case_id"],
        ).status.value
        == "in_review"
    )
    other_actor = client.post(
        url,
        json={},
        headers={**_auth("approver-token"), "Idempotency-Key": "logical-request-third"},
    )
    assert other_actor.status_code == 409


def test_delivery_uncertainty_blocks_new_call_until_approver_reconciliation(workbench):
    client, container = workbench
    case, _document = _create_case_and_document(client)
    container.service.agent = CallableAgentClient(
        lambda _request: (_ for _ in ()).throw(TimeoutError("response lost"))
    )
    url = f"/api/v1/cases/{case['case_id']}/reviews"
    job = client.post(
        url,
        json={},
        headers={**_auth(), "Idempotency-Key": "uncertain-first"},
    ).json()["job"]
    with pytest.raises(TimeoutError, match="response lost"):
        container.service.execute_job(org_id="org-a", job_id=job["job_id"])
    different = client.post(
        url,
        json={"trade_type": "export"},
        headers={**_auth(), "Idempotency-Key": "uncertain-different"},
    )
    assert different.status_code == 409
    forbidden = client.post(
        f"/api/v1/jobs/{job['job_id']}/reconciliation",
        json={
            "resolution": "confirmed_not_processed",
            "evidence_reference_sha256": "a" * 64,
            "reason": "Agent 로그에서 요청 미수신을 확인했습니다.",
        },
        headers=_auth(),
    )
    assert forbidden.status_code == 403
    reconciled = client.post(
        f"/api/v1/jobs/{job['job_id']}/reconciliation",
        json={
            "resolution": "confirmed_not_processed",
            "evidence_reference_sha256": "a" * 64,
            "reason": "Agent 로그에서 요청 미수신을 확인했습니다.",
        },
        headers=_auth("approver-token"),
    )
    assert reconciled.status_code == 200
    assert reconciled.json()["agent_call_state"] == "delivery_reconciled"
    resumed = client.post(
        url,
        json={"trade_type": "export"},
        headers={**_auth(), "Idempotency-Key": "uncertain-after-reconcile"},
    )
    assert resumed.status_code == 202
    audit = client.get(f"/api/v1/cases/{case['case_id']}/audit", headers=_auth("auditor-token"))
    assert "agent_delivery.reconciled" in {item["action"] for item in audit.json()["items"]}


def test_public_issue_and_finding_references_are_exactly_ledger_bound(workbench):
    client, container = workbench
    case, _document = _create_case_and_document(client)
    container.service.agent = CallableAgentClient(_agent_result_with_bound_issue_and_finding)
    job = client.post(
        f"/api/v1/cases/{case['case_id']}/reviews",
        json={},
        headers={**_auth(), "Idempotency-Key": "public-ledger-reference"},
    ).json()["job"]
    run = container.service.execute_job(org_id="org-a", job_id=job["job_id"])
    assert run.result_detail["validation_issues"][0]["reference_id"].startswith("issue_")
    assert run.result_detail["regulatory_findings"][0]["reference_id"].startswith("finding_")

    second_case, _second_document = _create_case_and_document(client)
    container.service.agent = CallableAgentClient(
        lambda request: _agent_result_with_bound_issue_and_finding(
            request, duplicate_issue_fact=True
        )
    )
    second_job = client.post(
        f"/api/v1/cases/{second_case['case_id']}/reviews",
        json={},
        headers={**_auth(), "Idempotency-Key": "duplicate-ledger-reference"},
    ).json()["job"]
    with pytest.raises(ConflictError, match="policy boundary"):
        container.service.execute_job(org_id="org-a", job_id=second_job["job_id"])


def test_enqueue_rejects_more_than_agent_release_primary_document_limit_atomically(workbench):
    client, container = workbench
    case = client.post(
        "/api/v1/cases",
        json={"title": "document count boundary"},
        headers=_auth(),
    ).json()
    for index in range(7):
        response = client.post(
            f"/api/v1/cases/{case['case_id']}/documents",
            json={
                "document_type": "UNKNOWN",
                "filename": f"invoice-{index:02d}.txt",
                "content_text": f"document {index}",
            },
            headers=_auth(),
        )
        assert response.status_code == 201

    rejected = client.post(
        f"/api/v1/cases/{case['case_id']}/reviews",
        json={},
        headers={**_auth(), "Idempotency-Key": "too-many-documents"},
    )
    assert rejected.status_code == 409
    assert container.repository.list_pending_outbox() == []


def test_enqueue_rejects_combined_primary_text_over_release_limit(workbench):
    client, container = workbench
    case = client.post(
        "/api/v1/cases",
        json={"title": "combined text boundary"},
        headers=_auth(),
    ).json()
    for index in range(3):
        response = client.post(
            f"/api/v1/cases/{case['case_id']}/documents",
            json={
                "document_type": "UNKNOWN",
                "filename": f"large-{index}.txt",
                "content_text": "x" * 170_000,
            },
            headers=_auth(),
        )
        assert response.status_code == 201
    rejected = client.post(
        f"/api/v1/cases/{case['case_id']}/reviews",
        json={},
        headers={**_auth(), "Idempotency-Key": "combined-text-too-large"},
    )
    assert rejected.status_code == 409
    assert container.repository.list_pending_outbox() == []


def test_durable_outbox_recovers_after_publish_failure(workbench, monkeypatch):
    client, container = workbench
    case, _document = _create_case_and_document(client)
    original = container.publisher.publish
    monkeypatch.setattr(
        container.publisher,
        "publish",
        lambda **_kwargs: (_ for _ in ()).throw(RuntimeError("redis unavailable")),
    )
    queued = client.post(
        f"/api/v1/cases/{case['case_id']}/reviews",
        json={},
        headers={**_auth(), "Idempotency-Key": "review-key-outbox"},
    )
    assert queued.status_code == 202
    assert len(container.repository.list_pending_outbox()) == 1
    monkeypatch.setattr(container.publisher, "publish", original)
    assert container.service.dispatch_pending_outbox() == 1
    assert container.repository.list_pending_outbox() == []


def test_worker_restart_preserves_job_and_forces_auto_completion_gate(workbench):
    client, container = workbench
    case, _document = _create_case_and_document(client)
    queued = client.post(
        f"/api/v1/cases/{case['case_id']}/reviews",
        json={"trade_type": "import"},
        headers={**_auth(), "Idempotency-Key": "review-key-restart"},
    ).json()["job"]
    restarted = WorkbenchService(
        repository=container.repository,
        object_store=container.object_store,
        publisher=container.publisher,
        agent=container.agent,
        auto_completion_enabled=False,
    )
    run = restarted.execute_job(org_id="org-a", job_id=queued["job_id"])
    assert run.assessment.value == "no_issue_detected"
    assert run.review_disposition.value == "human_review_required"
    assert {
        "backend_auto_completion_kill_switch_or_release_gate",
        "backend_release_pin_missing",
        "backend_primary_document_profile_mismatch",
    }.issubset(run.completion_eligibility.unmet_reason_codes)
    assert "model_calls" not in run.result_detail
    assert "runtime_release" not in run.result_detail
    assert run.result_detail["model_execution_summary"] == {
        "provider": None,
        "model_id": None,
        "text_only": False,
        "call_count": 0,
        "input_tokens_total": 0,
        "output_tokens_total": 0,
        "prompt_hashes_complete": False,
        "schema_hashes_complete": False,
        "runtime_identity_match": False,
        "audit_complete": False,
        "unmet_reason_codes": ["no_model_calls"],
    }
    assert container.repository.get_provider_result("org-a", run.review_run_id) is not None
    assert (
        restarted.execute_job(org_id="org-a", job_id=queued["job_id"]).result_digest
        == run.result_digest
    )
    response = client.get(f"/api/v1/jobs/{queued['job_id']}/events/stream", headers=_auth())
    assert response.status_code == 200
    assert "event: review-stage" in response.text
    assert "review.completed" in response.text


def test_review_exposes_only_a_bounded_complete_anthropic_execution_summary(workbench):
    client, container = workbench
    case, _document = _create_case_and_document(client)
    container.service.agent = CallableAgentClient(_agent_result_with_audited_claude_calls)
    queued = client.post(
        f"/api/v1/cases/{case['case_id']}/reviews",
        json={},
        headers={**_auth(), "Idempotency-Key": "review-audited-claude"},
    ).json()["job"]
    run = container.service.execute_job(org_id="org-a", job_id=queued["job_id"])
    summary = run.result_detail["model_execution_summary"]
    assert summary == {
        "provider": "anthropic",
        "model_id": run.release_identity["model_id"],
        "text_only": True,
        "call_count": 1,
        "input_tokens_total": 100,
        "output_tokens_total": 50,
        "prompt_hashes_complete": True,
        "schema_hashes_complete": True,
        "runtime_identity_match": True,
        "audit_complete": True,
        "unmet_reason_codes": [],
    }
    public_json = run.model_dump_json()
    assert "provider-request-sensitive" not in public_json
    assert "provider-message-sensitive" not in public_json
    assert '"model_calls"' not in public_json
    tampered = run.model_dump(mode="json")
    tampered["result_detail"]["model_execution_summary"]["input_tokens_total"] += 1
    with pytest.raises(ValueError, match="result_digest"):
        type(run).model_validate(tampered)


def test_revision_staleness_forbidden_targets_decision_and_chat_binding(workbench):
    client, container = workbench
    case, document = _create_case_and_document(client)
    queued = client.post(
        f"/api/v1/cases/{case['case_id']}/reviews",
        json={},
        headers={**_auth(), "Idempotency-Key": "review-key-parent"},
    ).json()["job"]
    parent = container.service.execute_job(org_id="org-a", job_id=queued["job_id"])
    assert (
        client.post(
            f"/api/v1/review-runs/{parent.review_run_id}/revisions",
            json={},
            headers={**_auth(), "Idempotency-Key": "direct-revision-forbidden"},
        ).status_code
        == 404
    )
    proposal_url = f"/api/v1/review-runs/{parent.review_run_id}/correction-proposals"
    base = {
        "parent_result_digest": parent.result_digest,
        "parent_input_manifest_sha256": parent.agent_input_manifest_sha256,
        "case_revision": parent.case_revision,
        "corrections": [
            {
                "document_id": document["record"]["document_id"],
                "field_path": "line_items.0.quantity",
                "new_value": "1200",
                "reason": "원문 재확인",
            }
        ],
    }
    stale_digest = {**base, "parent_result_digest": "0" * 64}
    assert (
        client.post(
            proposal_url,
            json=stale_digest,
            headers={**_auth(), "Idempotency-Key": "stale-proposal-create"},
        ).status_code
        == 409
    )
    forbidden = {
        **base,
        "corrections": [
            {
                "document_id": document["record"]["document_id"],
                "field_path": "evidence.0.text",
                "new_value": "fabricated",
                "reason": "forbidden",
            }
        ],
    }
    assert (
        client.post(
            proposal_url,
            json=forbidden,
            headers={**_auth(), "Idempotency-Key": "forbidden-proposal-create"},
        ).status_code
        == 422
    )
    proposal = client.post(
        proposal_url,
        json=base,
        headers={**_auth(), "Idempotency-Key": "valid-proposal-create"},
    ).json()
    decision_url = f"/api/v1/correction-proposals/{proposal['proposal_id']}/decisions"
    assert (
        client.post(
            decision_url,
            json={
                "decision": "approve",
                "reason": "검수 완료",
                "parent_result_digest": parent.result_digest,
                "case_revision": parent.case_revision,
            },
            headers={**_auth(), "Idempotency-Key": "proposal-decision-reviewer"},
        ).status_code
        == 403
    )
    approval = client.post(
        decision_url,
        json={
            "decision": "approve",
            "reason": "수정 결과 검수 완료",
            "parent_result_digest": parent.result_digest,
            "case_revision": parent.case_revision,
        },
        headers={
            **_auth("approver-token"),
            "Idempotency-Key": "proposal-decision-approver",
        },
    )
    assert approval.status_code == 201
    revision_job = approval.json()["job"]
    replayed_approval = client.post(
        decision_url,
        json={
            "decision": "approve",
            "reason": "수정 결과 검수 완료",
            "parent_result_digest": parent.result_digest,
            "case_revision": parent.case_revision,
        },
        headers={
            **_auth("approver-token"),
            "Idempotency-Key": "proposal-decision-approver",
        },
    )
    assert replayed_approval.status_code == 201
    assert replayed_approval.json() == approval.json()
    child = container.service.execute_job(org_id="org-a", job_id=revision_job["job_id"])
    assert child.parent_run_id == parent.review_run_id
    decisions = client.get(
        f"/api/v1/review-runs/{parent.review_run_id}/decisions",
        headers=_auth("approver-token"),
    ).json()
    assert decisions["items"][0]["proposal_id"] == proposal["proposal_id"]

    answer_url = f"/api/v1/review-runs/{parent.review_run_id}/answers"
    assert (
        client.post(
            answer_url,
            json={"question": "왜 정상인가요?", "bound_result_digest": "0" * 64},
            headers={**_auth(), "Idempotency-Key": "chat-stale-binding"},
        ).status_code
        == 409
    )
    answer = client.post(
        answer_url,
        json={"question": "왜 정상인가요?", "bound_result_digest": parent.result_digest},
        headers={**_auth(), "Idempotency-Key": "chat-grounded-answer"},
    ).json()
    assert answer["answer_status"] == "answered"
    assert answer["generation_mode"] == "deterministic_fallback"
    assert answer["model_execution_summary"]["audit_complete"] is False
    assert answer["model_execution_summary"]["unmet_reason_codes"] == ["no_model_calls"]
    replayed_answer = client.post(
        answer_url,
        json={"question": "왜 정상인가요?", "bound_result_digest": parent.result_digest},
        headers={**_auth(), "Idempotency-Key": "chat-grounded-answer"},
    )
    assert replayed_answer.status_code == 201
    assert replayed_answer.json() == answer
    changed_answer = client.post(
        answer_url,
        json={"question": "다른 질문", "bound_result_digest": parent.result_digest},
        headers={**_auth(), "Idempotency-Key": "chat-grounded-answer"},
    )
    assert changed_answer.status_code == 409
    assert (
        client.post(
            answer_url,
            json={"question": "x" * 2001, "bound_result_digest": parent.result_digest},
            headers={**_auth(), "Idempotency-Key": "chat-oversized-question"},
        ).status_code
        == 422
    )
    provider_result = container.repository.get_provider_result("org-a", parent.review_run_id)
    assert provider_result is not None
    expected_fact = provider_result["fact_ledger"]["entries"][0]
    assert answer["citations"] == [
        {"kind": expected_fact["kind"], "reference_id": expected_fact["reference_id"]}
    ]
    assert (
        client.post(
            answer_url,
            json={"question": "감사자가 질문", "bound_result_digest": parent.result_digest},
            headers={**_auth("auditor-token"), "Idempotency-Key": "chat-auditor-forbidden"},
        ).status_code
        == 403
    )
    auditor_messages = client.get(
        f"/api/v1/review-runs/{parent.review_run_id}/messages",
        headers=_auth("auditor-token"),
    )
    assert auditor_messages.status_code == 200
    assert [item["message_id"] for item in auditor_messages.json()["items"]] == [
        answer["message_id"]
    ]
    audit = client.get(f"/api/v1/cases/{case['case_id']}/audit", headers=_auth()).json()
    assert {item["action"] for item in audit["items"]} >= {
        "case.created",
        "document.added",
        "review.completed",
        "correction_proposal.decided",
        "case_chat.answered",
    }


def test_case_chat_preserves_a_safe_single_claude_call_summary(workbench):
    client, container = workbench
    case, _document = _create_case_and_document(client)
    queued = client.post(
        f"/api/v1/cases/{case['case_id']}/reviews",
        json={},
        headers={**_auth(), "Idempotency-Key": "chat-claude-parent"},
    ).json()["job"]
    run = container.service.execute_job(org_id="org-a", job_id=queued["job_id"])

    def claude_answer(provider_result, _question, question_id):
        fact = provider_result["fact_ledger"]["entries"][0]
        runtime = provider_result["runtime_release"]
        return AgentAnswerResponse(
            question_id=question_id,
            answer="근거 장부에 결박된 Claude 답변입니다.",
            status="answered",
            citations=[{"kind": fact["kind"], "reference_id": fact["reference_id"]}],
            generation_mode="claude",
            model_call={
                "task_id": "customs_case_answer",
                "provider": "anthropic",
                "model_id": runtime["model_id"],
                "prompt_sha256": runtime["chat_prompt_sha256"],
                "output_schema_sha256": runtime["chat_schema_sha256"],
                "request_id_sha256": "8" * 64,
                "message_id_sha256": "9" * 64,
                "input_tokens": 123,
                "output_tokens": 45,
                "latency_ms": 12,
            },
        )

    container.service.agent = CallableAgentClient(_agent_result, answer=claude_answer)
    response = client.post(
        f"/api/v1/review-runs/{run.review_run_id}/answers",
        json={
            "question": "정상 판정의 근거는 무엇인가요?",
            "bound_result_digest": run.result_digest,
        },
        headers={**_auth(), "Idempotency-Key": "chat-claude-audit"},
    )
    assert response.status_code == 201
    record = response.json()
    assert record["generation_mode"] == "claude"
    assert record["model_execution_summary"] == {
        "provider": "anthropic",
        "model_id": run.release_identity["model_id"],
        "text_only": True,
        "call_count": 1,
        "input_tokens_total": 123,
        "output_tokens_total": 45,
        "prompt_hashes_complete": True,
        "schema_hashes_complete": True,
        "runtime_identity_match": True,
        "audit_complete": True,
        "unmet_reason_codes": [],
    }
    serialized = response.text
    assert "request_id_sha256" not in serialized
    assert "message_id_sha256" not in serialized
    assert '"model_call"' not in serialized


def test_proposal_creation_is_durably_idempotent_and_conflict_detecting(workbench):
    client, container = workbench
    case, document = _create_case_and_document(client)
    queued = client.post(
        f"/api/v1/cases/{case['case_id']}/reviews",
        json={},
        headers={**_auth(), "Idempotency-Key": "proposal-idempotency-parent"},
    ).json()["job"]
    parent = container.service.execute_job(org_id="org-a", job_id=queued["job_id"])
    correction_payload = {
        "parent_result_digest": parent.result_digest,
        "parent_input_manifest_sha256": parent.agent_input_manifest_sha256,
        "case_revision": parent.case_revision,
        "corrections": [
            {
                "document_id": document["record"]["document_id"],
                "field_path": "fields.invoice_number",
                "new_value": "INV-IDEMPOTENT",
                "reason": "source recheck",
            }
        ],
    }
    correction_url = f"/api/v1/review-runs/{parent.review_run_id}/correction-proposals"
    headers = {**_auth(), "Idempotency-Key": "proposal-create-replay"}
    first = client.post(correction_url, json=correction_payload, headers=headers)
    replay = client.post(correction_url, json=correction_payload, headers=headers)
    assert first.status_code == replay.status_code == 201
    assert replay.json() == first.json()
    conflict_payload = {
        **correction_payload,
        "corrections": [{**correction_payload["corrections"][0], "new_value": "INV-CONFLICT"}],
    }
    assert client.post(correction_url, json=conflict_payload, headers=headers).status_code == 409

    concurrent_headers = {**_auth(), "Idempotency-Key": "proposal-create-concurrent"}

    def create_concurrently() -> tuple[int, dict]:
        response = client.post(correction_url, json=correction_payload, headers=concurrent_headers)
        return response.status_code, response.json()

    with ThreadPoolExecutor(max_workers=2) as executor:
        futures = [executor.submit(create_concurrently) for _ in range(2)]
        results = [item.result(timeout=10) for item in futures]
    assert [status for status, _payload in results] == [201, 201]
    assert results[0][1] == results[1][1]

    provider = container.repository.get_provider_result("org-a", parent.review_run_id)
    assert provider is not None
    field_reference = next(
        item["reference_id"]
        for item in provider["fact_ledger"]["entries"]
        if item["kind"] == "field_source"
    )
    revision_payload = {
        "parent_result_digest": parent.result_digest,
        "parent_input_manifest_sha256": parent.agent_input_manifest_sha256,
        "case_revision": parent.case_revision,
        "actions": [
            {
                "decision": "confirm_field",
                "reason": "source confirmed",
                "confirmed_reference_id": field_reference,
            },
            {"decision": "approve_review", "reason": "reviewed proposal"},
        ],
    }
    revision_url = f"/api/v1/review-runs/{parent.review_run_id}/revision-proposals"
    revision_headers = {**_auth(), "Idempotency-Key": "typed-proposal-create-replay"}
    revision = client.post(revision_url, json=revision_payload, headers=revision_headers)
    revision_replay = client.post(revision_url, json=revision_payload, headers=revision_headers)
    assert revision.status_code == revision_replay.status_code == 201
    assert revision_replay.json() == revision.json()
    changed_revision = {
        **revision_payload,
        "actions": [
            {**revision_payload["actions"][0], "reason": "different request"},
            revision_payload["actions"][1],
        ],
    }
    assert (
        client.post(revision_url, json=changed_revision, headers=revision_headers).status_code
        == 409
    )


def test_valid_agent_response_persistence_failure_is_not_contract_rejection(workbench, monkeypatch):
    client, container = workbench
    case, _document = _create_case_and_document(client)
    calls = 0

    def review(request: AgentReviewRequest) -> dict:
        nonlocal calls
        calls += 1
        return _agent_result(request)

    container.service.agent = CallableAgentClient(review)
    queued = client.post(
        f"/api/v1/cases/{case['case_id']}/reviews",
        json={},
        headers={**_auth(), "Idempotency-Key": "valid-response-persistence-parent"},
    ).json()["job"]
    monkeypatch.setattr(
        container.repository,
        "complete_job",
        lambda **_kwargs: (_ for _ in ()).throw(OSError("database commit unavailable")),
    )
    with pytest.raises(OSError, match="database commit unavailable"):
        container.service.execute_job(org_id="org-a", job_id=queued["job_id"])
    stored = container.repository.get_job("org-a", queued["job_id"])
    assert stored is not None
    assert stored.status.value == "response_persistence_uncertain"
    assert stored.agent_call_state.value == "response_persistence_uncertain"
    monkeypatch.setattr(
        "customs_backend.api.time.sleep",
        lambda _seconds: (_ for _ in ()).throw(
            AssertionError("terminal SSE stream attempted to poll again")
        ),
    )
    streamed = client.get(
        f"/api/v1/jobs/{queued['job_id']}/events/stream",
        headers=_auth(),
    )
    assert streamed.status_code == 200
    assert "review.response_persistence_uncertain" in streamed.text
    with pytest.raises(ConflictError):
        container.service.execute_job(org_id="org-a", job_id=queued["job_id"])
    assert calls == 1


def test_typed_revision_compiles_confirm_correct_support_and_terminal_decisions(workbench):
    client, container = workbench
    case, document = _create_case_and_document(client)
    parent_job = client.post(
        f"/api/v1/cases/{case['case_id']}/reviews",
        json={},
        headers={**_auth(), "Idempotency-Key": "typed-parent"},
    ).json()["job"]
    parent = container.service.execute_job(org_id="org-a", job_id=parent_job["job_id"])
    provider = container.repository.get_provider_result("org-a", parent.review_run_id)
    assert provider is not None
    field_reference = next(
        item["reference_id"]
        for item in provider["fact_ledger"]["entries"]
        if item["kind"] == "field_source"
    )

    support_source = client.post(
        f"/api/v1/cases/{case['case_id']}/documents",
        json={
            "document_type": "SUPPORTING",
            "filename": "permit.txt",
            "content_text": "synthetic permit",
        },
        headers=_auth(),
    ).json()["record"]
    support = client.post(
        f"/api/v1/cases/{case['case_id']}/supporting-documents",
        json={
            "document_id": support_source["document_id"],
            "document_code": "IMPORT_REQUIREMENT_CERT",
            "issuer": "TEST ISSUER",
            "document_number": "PERMIT-002",
            "issued_on": "2026-07-01",
            "valid_until": "2027-07-01",
            "subject_item_ids": ["item-1"],
            "hs_codes": ["0303540000"],
            "source_sha256": support_source["content_sha256"],
        },
        headers=_auth(),
    ).json()
    current_case = client.get(f"/api/v1/cases/{case['case_id']}", headers=_auth()).json()
    proposal = client.post(
        f"/api/v1/review-runs/{parent.review_run_id}/revision-proposals",
        json={
            "parent_result_digest": parent.result_digest,
            "parent_input_manifest_sha256": parent.agent_input_manifest_sha256,
            "case_revision": current_case["revision"],
            "actions": [
                {
                    "decision": "confirm_field",
                    "reason": "원문과 대조 확인",
                    "confirmed_reference_id": field_reference,
                },
                {
                    "decision": "correct_field",
                    "reason": "원문 재확인",
                    "correction": {
                        "document_id": document["record"]["document_id"],
                        "field_path": "line_items.0.quantity",
                        "new_value": "1200",
                        "reason": "원문 재확인",
                    },
                },
                {
                    "decision": "add_supporting_document",
                    "reason": "추가 요건서류 검토",
                    "supporting_document_id": support["document_id"],
                },
                {
                    "decision": "approve_review",
                    "reason": "제안된 변경 전체 승인",
                },
            ],
        },
        headers={**_auth(), "Idempotency-Key": "typed-revision-create"},
    )
    assert proposal.status_code == 201, proposal.text
    seen = []

    def revise(request):
        seen.append(request)
        return {
            "parent_result_sha256": request.parent_result_sha256,
            "child_result": _agent_result(request.revised_request),
            "applied_decision_ids": [item.decision_id for item in request.decisions],
        }

    container.service.agent = CallableAgentClient(_agent_result, revise=revise)
    approval = client.post(
        f"/api/v1/revision-proposals/{proposal.json()['proposal_id']}/decisions",
        json={
            "decision": "approve",
            "reason": "독립 검수 완료",
            "parent_result_digest": parent.result_digest,
            "case_revision": current_case["revision"],
        },
        headers={**_auth("approver-token"), "Idempotency-Key": "typed-approval"},
    )
    assert approval.status_code == 201, approval.text
    child = container.service.execute_job(
        org_id="org-a",
        job_id=approval.json()["job"]["job_id"],
    )
    assert child.parent_run_id == parent.review_run_id
    assert len(seen) == 1
    revision = seen[0]
    assert [item.decision for item in revision.decisions] == [
        "confirm_field",
        "correct_field",
        "add_supporting_document",
        "approve_review",
    ]
    assert revision.parent_request.supporting_documents == []
    assert [item.document_id for item in revision.revised_request.supporting_documents] == [
        support["document_id"]
    ]

    reject_proposal = client.post(
        f"/api/v1/review-runs/{child.review_run_id}/revision-proposals",
        json={
            "parent_result_digest": child.result_digest,
            "parent_input_manifest_sha256": child.agent_input_manifest_sha256,
            "case_revision": current_case["revision"],
            "actions": [{"decision": "reject_review", "reason": "사람 검토 필요"}],
        },
        headers={**_auth(), "Idempotency-Key": "typed-rejection-create"},
    )
    assert reject_proposal.status_code == 201, reject_proposal.text
    rejection = client.post(
        f"/api/v1/revision-proposals/{reject_proposal.json()['proposal_id']}/decisions",
        json={
            "decision": "approve",
            "reason": "거절 결정 독립 확인",
            "parent_result_digest": child.result_digest,
            "case_revision": current_case["revision"],
        },
        headers={**_auth("approver-token"), "Idempotency-Key": "typed-rejection"},
    )
    assert rejection.status_code == 201, rejection.text
    container.service.execute_job(org_id="org-a", job_id=rejection.json()["job"]["job_id"])
    assert seen[-1].decisions[0].decision == "reject_review"


def test_revision_transport_envelope_is_checked_before_enqueue(workbench, monkeypatch):
    client, container = workbench
    case, _document = _create_case_and_document(client)
    parent_job = client.post(
        f"/api/v1/cases/{case['case_id']}/reviews",
        json={},
        headers={**_auth(), "Idempotency-Key": "revision-size-parent"},
    ).json()["job"]
    parent = container.service.execute_job(org_id="org-a", job_id=parent_job["job_id"])
    provider = container.repository.get_provider_result("org-a", parent.review_run_id)
    assert provider is not None
    field_reference = next(
        item["reference_id"]
        for item in provider["fact_ledger"]["entries"]
        if item["kind"] == "field_source"
    )
    proposal = client.post(
        f"/api/v1/review-runs/{parent.review_run_id}/revision-proposals",
        json={
            "parent_result_digest": parent.result_digest,
            "parent_input_manifest_sha256": parent.agent_input_manifest_sha256,
            "case_revision": parent.case_revision,
            "actions": [
                {
                    "decision": "confirm_field",
                    "reason": "원문과 대조 확인",
                    "confirmed_reference_id": field_reference,
                },
                {"decision": "approve_review", "reason": "수정안 승인"},
            ],
        },
        headers={**_auth(), "Idempotency-Key": "revision-size-create"},
    )
    assert proposal.status_code == 201, proposal.text

    # The production value is asserted separately as exactly 2 MiB. Lowering it
    # here proves that the complete AgentRevisionRequest wire envelope is checked
    # before a Job, outbox message, or child Run can be persisted.
    monkeypatch.setattr("customs_backend.service.MAX_AGENT_REQUEST_BODY_BYTES", 1)
    rejected = client.post(
        f"/api/v1/revision-proposals/{proposal.json()['proposal_id']}/decisions",
        json={
            "decision": "approve",
            "reason": "독립 검수 완료",
            "parent_result_digest": parent.result_digest,
            "case_revision": parent.case_revision,
        },
        headers={**_auth("approver-token"), "Idempotency-Key": "revision-size-approval"},
    )
    assert rejected.status_code == 409
    assert rejected.json()["code"] == "stale_or_conflicting_state"
    stored = client.get(
        f"/api/v1/review-runs/{parent.review_run_id}/revision-proposals",
        headers=_auth(),
    ).json()["items"][0]
    assert stored["status"] == "pending"
    assert stored["child_job_id"] is None


def test_same_org_reviewer_only_sees_assigned_cases(workbench):
    client, _container = workbench
    case, _document = _create_case_and_document(client)
    assert client.get("/api/v1/cases", headers=_auth("unassigned-token")).json() == {"items": []}
    assert (
        client.get(
            f"/api/v1/cases/{case['case_id']}", headers=_auth("unassigned-token")
        ).status_code
        == 404
    )
    assert (
        client.get(f"/api/v1/cases/{case['case_id']}", headers=_auth("auditor-token")).status_code
        == 200
    )


def test_supporting_document_requires_hash_binding_and_approver_assertion(workbench):
    client, _container = workbench
    case, document = _create_case_and_supporting_source(client)
    document_record = document["record"]
    payload = {
        "document_id": document_record["document_id"],
        "document_code": "IMPORT_REQUIREMENT_CERT",
        "issuer": "TEST ISSUER",
        "document_number": "CERT-001",
        "issued_on": "2026-07-01",
        "valid_until": "2027-07-01",
        "subject_item_ids": ["item-1"],
        "hs_codes": ["0303540000"],
        "source_sha256": document_record["content_sha256"],
    }
    registered = client.post(
        f"/api/v1/cases/{case['case_id']}/supporting-documents",
        json=payload,
        headers=_auth(),
    )
    assert registered.status_code == 201
    assert client.get(f"/api/v1/cases/{case['case_id']}", headers=_auth()).json()["revision"] == 2
    assertion_url = (
        f"/api/v1/cases/{case['case_id']}/supporting-documents/"
        f"{document_record['document_id']}/assertions"
    )
    assertion = {"method": "human_approver"}
    assert client.post(assertion_url, json=assertion, headers=_auth()).status_code == 403
    asserted = client.post(
        assertion_url,
        json=assertion,
        headers=_auth("approver-token"),
    )
    assert asserted.status_code == 201
    assert asserted.json()["verification_assertions"][0]["method"] == "human_approver"
    assert (
        asserted.json()["verification_assertions"][0]["source_reference_sha256"]
        == document_record["content_sha256"]
    )
    assert asserted.json()["verification_assertions"][0]["authority_id"].startswith(
        "backend:principal-sha256:"
    )
    assert (
        asserted.json()["verification_assertions"][0]["authority_release"]
        == "backend-human-decision-policy/v1"
    )
    assert client.get(f"/api/v1/cases/{case['case_id']}", headers=_auth()).json()["revision"] == 3
    mismatch = {**payload, "source_sha256": "0" * 64}
    assert (
        client.post(
            f"/api/v1/cases/{case['case_id']}/supporting-documents",
            json=mismatch,
            headers=_auth(),
        ).status_code
        == 409
    )


def test_supporting_document_case_cap_is_authoritative_and_metadata_only(workbench):
    client, _container = workbench
    case = client.post(
        "/api/v1/cases",
        json={"title": "supporting document hard cap"},
        headers=_auth(),
    ).json()

    def create_source(index: int):
        return client.post(
            f"/api/v1/cases/{case['case_id']}/documents",
            json={
                "document_type": "SUPPORTING",
                "filename": f"permit-{index:03d}.txt",
                "content_text": f"SYNTHETIC PERMIT {index}",
            },
            headers=_auth(),
        )

    def register_source(record: dict, index: int):
        return client.post(
            f"/api/v1/cases/{case['case_id']}/supporting-documents",
            json={
                "document_id": record["document_id"],
                "document_code": f"TEST_CERT_{index:03d}",
                "issuer": "TEST ISSUER",
                "document_number": f"CERT-{index:03d}",
                "issued_on": "2026-07-01",
                "subject_item_ids": ["item-1"],
                "hs_codes": ["0303540000"],
                "source_sha256": record["content_sha256"],
            },
            headers=_auth(),
        )

    for index in range(99):
        source = create_source(index)
        assert source.status_code == 201
        assert register_source(source.json()["record"], index).status_code == 201

    with ThreadPoolExecutor(max_workers=2) as executor:
        futures = [executor.submit(create_source, index) for index in (99, 100)]
        boundary = [item.result(timeout=20) for item in futures]
    assert sorted(item.status_code for item in boundary) == [201, 409]
    final_source = next(item.json()["record"] for item in boundary if item.status_code == 201)
    assert register_source(final_source, 99).status_code == 201

    listed = client.get(
        f"/api/v1/cases/{case['case_id']}/supporting-documents",
        headers=_auth(),
    )
    assert listed.status_code == 200
    assert len(listed.json()["items"]) == 100
    assert "content_text" not in listed.text
    assert create_source(101).status_code == 409


@pytest.mark.parametrize(
    "injected",
    [
        {"source_reference_sha256": "0" * 64},
        {"authority_id": "attacker:controlled"},
        {"authority_release": "attacker-policy/v1"},
        {"asserted_by": "attacker"},
    ],
)
def test_assertion_endpoint_rejects_client_supplied_trust_fields(workbench, injected):
    client, _container = workbench
    case, document = _create_case_and_supporting_source(client)
    record = document["record"]
    registered = client.post(
        f"/api/v1/cases/{case['case_id']}/supporting-documents",
        json={
            "document_id": record["document_id"],
            "document_code": "IMPORT_REQUIREMENT_CERT",
            "issuer": "TEST ISSUER",
            "document_number": "CERT-INJECTION",
            "issued_on": "2026-07-01",
            "valid_until": "2027-07-01",
            "subject_item_ids": ["item-1"],
            "hs_codes": ["0303540000"],
            "source_sha256": record["content_sha256"],
        },
        headers=_auth(),
    )
    assert registered.status_code == 201
    response = client.post(
        (
            f"/api/v1/cases/{case['case_id']}/supporting-documents/"
            f"{record['document_id']}/assertions"
        ),
        json={"method": "human_approver", **injected},
        headers=_auth("approver-token"),
    )
    assert response.status_code == 422


def test_primary_document_cannot_be_reclassified_as_supporting_to_bypass_review(workbench):
    client, _container = workbench
    case, document = _create_case_and_document(client)
    record = document["record"]
    response = client.post(
        f"/api/v1/cases/{case['case_id']}/supporting-documents",
        json={
            "document_id": record["document_id"],
            "document_code": "IMPORT_REQUIREMENT_CERT",
            "issuer": "TEST ISSUER",
            "document_number": "BYPASS-001",
            "issued_on": "2026-07-01",
            "subject_item_ids": ["item-1"],
            "hs_codes": ["0303540000"],
            "source_sha256": record["content_sha256"],
        },
        headers=_auth(),
    )
    assert response.status_code == 409


def test_admin_cannot_verify_a_supporting_source_they_created(workbench):
    client, _container = workbench
    case = client.post(
        "/api/v1/cases",
        json={"title": "supporting maker checker"},
        headers=_auth("admin-token"),
    ).json()
    source = client.post(
        f"/api/v1/cases/{case['case_id']}/documents",
        json={
            "document_type": "SUPPORTING",
            "filename": "permit.txt",
            "content_text": "maker-controlled source",
        },
        headers=_auth("admin-token"),
    ).json()["record"]
    assert (
        client.post(
            f"/api/v1/cases/{case['case_id']}/supporting-documents",
            json={
                "document_id": source["document_id"],
                "document_code": "IMPORT_REQUIREMENT_CERT",
                "issuer": "TEST ISSUER",
                "document_number": "MAKER-001",
                "issued_on": "2026-07-01",
                "subject_item_ids": ["item-1"],
                "hs_codes": ["0303540000"],
                "source_sha256": source["content_sha256"],
            },
            headers=_auth("admin-token"),
        ).status_code
        == 201
    )
    response = client.post(
        f"/api/v1/cases/{case['case_id']}/supporting-documents/{source['document_id']}/assertions",
        json={"method": "human_approver"},
        headers=_auth("admin-token"),
    )
    assert response.status_code == 409


def test_four_primary_documents_and_supporting_source_have_separate_agent_roles(workbench):
    client, container = workbench
    case = client.post(
        "/api/v1/cases",
        json={"title": "four primary plus support"},
        headers=_auth(),
    ).json()
    primary_ids: list[str] = []
    for document_type in ("CI", "PL", "BL", "CO"):
        response = client.post(
            f"/api/v1/cases/{case['case_id']}/documents",
            json={
                "document_type": document_type,
                "filename": f"{document_type.casefold()}.txt",
                "content_text": f"{document_type} synthetic primary text",
            },
            headers=_auth(),
        )
        assert response.status_code == 201
        primary_ids.append(response.json()["record"]["document_id"])
    supporting_source = client.post(
        f"/api/v1/cases/{case['case_id']}/documents",
        json={
            "document_type": "SUPPORTING",
            "filename": "permit.txt",
            "content_text": "synthetic import permit",
        },
        headers=_auth(),
    ).json()["record"]
    supporting_payload = {
        "document_id": supporting_source["document_id"],
        "document_code": "IMPORT_REQUIREMENT_CERT",
        "issuer": "TEST ISSUER",
        "document_number": "PERMIT-001",
        "issued_on": "2026-07-01",
        "valid_until": "2027-07-01",
        "subject_item_ids": ["item-1"],
        "hs_codes": ["0303540000"],
        "source_sha256": supporting_source["content_sha256"],
    }
    assert (
        client.post(
            f"/api/v1/cases/{case['case_id']}/supporting-documents",
            json=supporting_payload,
            headers=_auth(),
        ).status_code
        == 201
    )
    assert (
        client.post(
            f"/api/v1/cases/{case['case_id']}/supporting-documents/"
            f"{supporting_source['document_id']}/assertions",
            json={"method": "human_approver"},
            headers=_auth("approver-token"),
        ).status_code
        == 201
    )
    captured: list[AgentReviewRequest] = []

    def review(request: AgentReviewRequest) -> dict:
        captured.append(request)
        return _agent_result(request)

    container.service.agent = CallableAgentClient(review)
    queued = client.post(
        f"/api/v1/cases/{case['case_id']}/reviews",
        json={},
        headers={**_auth(), "Idempotency-Key": "four-primary-one-support"},
    )
    assert queued.status_code == 202
    internal = container.repository.get_job("org-a", queued.json()["job"]["job_id"])
    assert internal is not None
    assert len(internal.request["documents"]) == 5
    container.service.execute_job(org_id="org-a", job_id=internal.job_id)
    assert len(captured) == 1
    assert [item.document_id for item in captured[0].documents] == primary_ids
    assert len(captured[0].supporting_documents) == 1
    assert captured[0].supporting_documents[0].source_sha256 == supporting_source["content_sha256"]


def test_supporting_source_hash_is_reverified_before_agent_call(workbench):
    client, container = workbench
    case, supporting = _create_case_and_supporting_source(client)
    assert (
        client.post(
            f"/api/v1/cases/{case['case_id']}/documents",
            json={
                "document_type": "CI",
                "filename": "invoice.txt",
                "content_text": "COMMERCIAL INVOICE",
            },
            headers=_auth(),
        ).status_code
        == 201
    )
    source = supporting["record"]
    assert (
        client.post(
            f"/api/v1/cases/{case['case_id']}/supporting-documents",
            json={
                "document_id": source["document_id"],
                "document_code": "IMPORT_REQUIREMENT_CERT",
                "issuer": "TEST ISSUER",
                "document_number": "HASH-CHECK-001",
                "issued_on": "2026-07-01",
                "subject_item_ids": ["item-1"],
                "hs_codes": ["0303540000"],
                "source_sha256": source["content_sha256"],
            },
            headers=_auth(),
        ).status_code
        == 201
    )
    queued = client.post(
        f"/api/v1/cases/{case['case_id']}/reviews",
        json={},
        headers={**_auth(), "Idempotency-Key": "support-source-readback"},
    ).json()["job"]
    job = container.repository.get_job("org-a", queued["job_id"])
    assert job is not None
    support_snapshot = next(
        item for item in job.request["documents"] if item["document_id"] == source["document_id"]
    )
    container.object_store._objects[support_snapshot["object_key"]] = "tampered"  # noqa: SLF001
    calls = 0

    def review(request: AgentReviewRequest) -> dict:
        nonlocal calls
        calls += 1
        return _agent_result(request)

    container.service.agent = CallableAgentClient(review)
    with pytest.raises(ValueError, match="object readback hash mismatch"):
        container.service.execute_job(org_id="org-a", job_id=job.job_id)
    after = container.repository.get_job("org-a", job.job_id)
    assert after is not None
    assert after.status.value == "queued"
    assert after.agent_call_state.value == "not_started"
    assert calls == 0


def test_enqueue_snapshot_is_immutable_and_public_job_hides_private_request(workbench):
    client, container = workbench
    case, first_document = _create_case_and_document(client)
    observed_document_ids: list[list[str]] = []
    observed_assertion_cutoffs: list[datetime] = []

    def observe(request: AgentReviewRequest) -> dict:
        observed_document_ids.append([item.document_id for item in request.documents])
        observed_assertion_cutoffs.append(request.assertion_cutoff_at)
        return _agent_result(request)

    container.service.agent = CallableAgentClient(observe)
    queued_response = client.post(
        f"/api/v1/cases/{case['case_id']}/reviews",
        json={},
        headers={**_auth(), "Idempotency-Key": "immutable-snapshot"},
    )
    queued = queued_response.json()["job"]
    assert "request" not in queued
    assert "request_digest" not in queued
    internal_job = container.repository.get_job("org-a", queued["job_id"])
    assert internal_job is not None
    snapshot_revision = internal_job.request["case_revision"]
    snapshot_assertion_cutoff = datetime.fromisoformat(internal_job.request["assertion_cutoff_at"])
    assert snapshot_assertion_cutoff.tzinfo is not None
    second = client.post(
        f"/api/v1/cases/{case['case_id']}/documents",
        json={
            "document_type": "PL",
            "filename": "packing-list.txt",
            "content_text": "PACKING LIST\nQTY: 10",
        },
        headers=_auth(),
    )
    assert second.status_code == 201
    run = container.service.execute_job(org_id="org-a", job_id=queued["job_id"])
    assert observed_document_ids == [[first_document["record"]["document_id"]]]
    assert observed_assertion_cutoffs == [snapshot_assertion_cutoff]
    assert run.case_revision == snapshot_revision
    public_run = client.get(f"/api/v1/review-runs/{run.review_run_id}", headers=_auth()).json()
    assert "model_calls" not in public_run["result_detail"]
    assert "runtime_release" not in public_run["result_detail"]


def test_atomic_job_lease_prevents_duplicate_agent_call(workbench):
    client, container = workbench
    case, _document = _create_case_and_document(client)
    entered = Event()
    release = Event()
    calls: list[str] = []

    def slow_review(request: AgentReviewRequest) -> dict:
        calls.append(request.review_run_id)
        entered.set()
        assert release.wait(timeout=5)
        return _agent_result(request)

    container.service.agent = CallableAgentClient(slow_review)
    job = client.post(
        f"/api/v1/cases/{case['case_id']}/reviews",
        json={},
        headers={**_auth(), "Idempotency-Key": "concurrent-job"},
    ).json()["job"]
    with ThreadPoolExecutor(max_workers=2) as executor:
        first = executor.submit(
            container.service.execute_job,
            org_id="org-a",
            job_id=job["job_id"],
        )
        assert entered.wait(timeout=2)
        duplicate = executor.submit(
            container.service.execute_job,
            org_id="org-a",
            job_id=job["job_id"],
        )
        with pytest.raises(ConflictError):
            duplicate.result(timeout=2)
        release.set()
        run = first.result(timeout=5)
    assert calls == [run.review_run_id]
    assert len(container.repository.list_runs("org-a", case["case_id"])) == 1


def test_correction_proposal_becomes_stale_after_case_change(workbench):
    client, container = workbench
    case, document = _create_case_and_document(client)
    job = client.post(
        f"/api/v1/cases/{case['case_id']}/reviews",
        json={},
        headers={**_auth(), "Idempotency-Key": "stale-proposal-parent"},
    ).json()["job"]
    parent = container.service.execute_job(org_id="org-a", job_id=job["job_id"])
    proposal = client.post(
        f"/api/v1/review-runs/{parent.review_run_id}/correction-proposals",
        json={
            "parent_result_digest": parent.result_digest,
            "parent_input_manifest_sha256": parent.agent_input_manifest_sha256,
            "case_revision": parent.case_revision,
            "corrections": [
                {
                    "document_id": document["record"]["document_id"],
                    "field_path": "fields.invoice_number",
                    "new_value": "INV-CORRECTED",
                    "reason": "원문 확인",
                }
            ],
        },
        headers={**_auth(), "Idempotency-Key": "stale-case-proposal-create"},
    ).json()
    assert (
        client.post(
            f"/api/v1/cases/{case['case_id']}/documents",
            json={
                "document_type": "PL",
                "filename": "later.txt",
                "content_text": "later material change",
            },
            headers=_auth(),
        ).status_code
        == 201
    )
    stale = client.post(
        f"/api/v1/correction-proposals/{proposal['proposal_id']}/decisions",
        json={
            "decision": "approve",
            "reason": "too late",
            "parent_result_digest": parent.result_digest,
            "case_revision": parent.case_revision,
        },
        headers={
            **_auth("approver-token"),
            "Idempotency-Key": "stale-proposal-decision",
        },
    )
    assert stale.status_code == 409


def test_external_agent_failure_closes_durable_call_once_barrier(workbench):
    client, container = workbench
    case, _document = _create_case_and_document(client)
    calls = 0

    def fail(_request: AgentReviewRequest) -> dict:
        nonlocal calls
        calls += 1
        raise RuntimeError("provider unavailable")

    container.service.agent = CallableAgentClient(fail)
    job = client.post(
        f"/api/v1/cases/{case['case_id']}/reviews",
        json={},
        headers={**_auth(), "Idempotency-Key": "bounded-retries"},
    ).json()["job"]
    with pytest.raises(RuntimeError, match="provider unavailable"):
        container.service.execute_job(org_id="org-a", job_id=job["job_id"])
    stored = container.repository.get_job("org-a", job["job_id"])
    assert stored is not None
    assert stored.status.value == "delivery_uncertain"
    assert stored.agent_call_state.value == "delivery_uncertain"
    assert stored.agent_call_endpoint == "review"
    assert stored.agent_call_request_sha256 is not None
    with pytest.raises(ConflictError):
        container.service.execute_job(org_id="org-a", job_id=job["job_id"])
    assert calls == 1


def test_external_revision_failure_uses_the_same_call_once_barrier(workbench):
    client, container = workbench
    case, document = _create_case_and_document(client)
    parent_job = client.post(
        f"/api/v1/cases/{case['case_id']}/reviews",
        json={},
        headers={**_auth(), "Idempotency-Key": "revision-call-once-parent"},
    ).json()["job"]
    parent = container.service.execute_job(org_id="org-a", job_id=parent_job["job_id"])
    proposal = client.post(
        f"/api/v1/review-runs/{parent.review_run_id}/correction-proposals",
        json={
            "parent_result_digest": parent.result_digest,
            "parent_input_manifest_sha256": parent.agent_input_manifest_sha256,
            "case_revision": parent.case_revision,
            "corrections": [
                {
                    "document_id": document["record"]["document_id"],
                    "field_path": "fields.invoice_number",
                    "new_value": "INV-REVISED",
                    "reason": "source recheck",
                }
            ],
        },
        headers={**_auth(), "Idempotency-Key": "revision-call-once-create"},
    ).json()
    child_job = client.post(
        f"/api/v1/correction-proposals/{proposal['proposal_id']}/decisions",
        json={
            "decision": "approve",
            "reason": "independent review",
            "parent_result_digest": parent.result_digest,
            "case_revision": parent.case_revision,
        },
        headers={**_auth("approver-token"), "Idempotency-Key": "revision-call-once-child"},
    ).json()["job"]
    calls = 0

    def fail_revision(_request):
        nonlocal calls
        calls += 1
        raise TimeoutError("revision delivery response lost")

    container.service.agent = CallableAgentClient(_agent_result, revise=fail_revision)
    with pytest.raises(TimeoutError, match="revision delivery response lost"):
        container.service.execute_job(org_id="org-a", job_id=child_job["job_id"])
    stored = container.repository.get_job("org-a", child_job["job_id"])
    assert stored is not None
    assert stored.status.value == "delivery_uncertain"
    assert stored.agent_call_endpoint == "revision"
    with pytest.raises(ConflictError):
        container.service.execute_job(org_id="org-a", job_id=child_job["job_id"])
    assert calls == 1


def test_pre_delivery_failure_can_retry_without_recalling_agent(workbench):
    client, container = workbench
    case, _document = _create_case_and_document(client)
    job = client.post(
        f"/api/v1/cases/{case['case_id']}/reviews",
        json={},
        headers={**_auth(), "Idempotency-Key": "pre-delivery-retry"},
    ).json()["job"]
    original_get = container.object_store.get_text
    reads = 0
    calls = 0

    def flaky_get(*, key: str, expected_sha256: str) -> str:
        nonlocal reads
        reads += 1
        if reads < 3:
            raise OSError("temporary local object-store failure")
        return original_get(key=key, expected_sha256=expected_sha256)

    def review(request: AgentReviewRequest) -> dict:
        nonlocal calls
        calls += 1
        return _agent_result(request)

    container.object_store.get_text = flaky_get
    container.service.agent = CallableAgentClient(review)
    for _ in range(2):
        with pytest.raises(OSError, match="temporary local object-store failure"):
            container.service.execute_job(org_id="org-a", job_id=job["job_id"])
        stored = container.repository.get_job("org-a", job["job_id"])
        assert stored is not None
        assert stored.status.value == "queued"
        assert stored.agent_call_state.value == "not_started"
    container.service.execute_job(org_id="org-a", job_id=job["job_id"])
    assert calls == 1


def test_enqueue_resolves_review_date_and_binds_complete_atomic_snapshot(workbench):
    client, container = workbench
    case, _document = _create_case_and_document(client)
    response = client.post(
        f"/api/v1/cases/{case['case_id']}/reviews",
        json={},
        headers={**_auth(), "Idempotency-Key": "atomic-snapshot-digest"},
    )
    assert response.status_code == 202
    job = container.repository.get_job("org-a", response.json()["job"]["job_id"])
    assert job is not None
    assert job.request["review_as_of"] == job.created_at.date().isoformat()
    assert job.request_digest == canonical_digest(job.request)
    assert job.request["case_revision"] == 1
    assert len(job.request["documents"]) == 1


def test_missing_review_date_in_immutable_snapshot_never_reaches_agent(workbench):
    client, container = workbench
    case, _document = _create_case_and_document(client)
    response = client.post(
        f"/api/v1/cases/{case['case_id']}/reviews",
        json={},
        headers={**_auth(), "Idempotency-Key": "missing-review-date"},
    )
    job_id = response.json()["job"]["job_id"]
    stored = container.repository.get_job("org-a", job_id)
    assert stored is not None
    tampered_request = dict(stored.request)
    tampered_request.pop("review_as_of")
    container.repository._jobs[job_id] = stored.model_copy(  # noqa: SLF001 - adapter corruption test
        update={
            "request": tampered_request,
            "request_digest": canonical_digest(tampered_request),
        }
    )
    calls = 0

    def review(request: AgentReviewRequest) -> dict:
        nonlocal calls
        calls += 1
        return _agent_result(request)

    container.service.agent = CallableAgentClient(review)
    with pytest.raises(ConflictError, match="missing review_as_of"):
        container.service.execute_job(org_id="org-a", job_id=job_id)
    after = container.repository.get_job("org-a", job_id)
    assert after is not None
    assert after.status.value == "queued"
    assert after.agent_call_state.value == "not_started"
    assert calls == 0


def test_case_revision_drift_downgrades_production_assurance_and_completion(workbench):
    client, container = workbench
    case, _document = _create_case_and_document(client)
    job = client.post(
        f"/api/v1/cases/{case['case_id']}/reviews",
        json={},
        headers={**_auth(), "Idempotency-Key": "revision-drift-gate"},
    ).json()["job"]
    assert (
        client.post(
            f"/api/v1/cases/{case['case_id']}/documents",
            json={
                "document_type": "PL",
                "filename": "changed.txt",
                "content_text": "material Case change",
            },
            headers=_auth(),
        ).status_code
        == 201
    )
    run = container.service.execute_job(org_id="org-a", job_id=job["job_id"])
    assert run.review_disposition.value == "human_review_required"
    assert run.assurance_level.value == "production_candidate"
    assert "case_revision_drift" in run.completion_eligibility.unmet_reason_codes
    assert {item.action_code for item in run.required_actions} >= {
        "rerun_after_case_revision_change",
        "confirm_backend_release_pin",
    }
    synthetic_production = run.model_copy(update={"assurance_level": "production"})
    drifted = WorkbenchService._case_drift_run(synthetic_production)
    assert drifted.assurance_level.value == "production_candidate"


def test_case_change_during_agent_call_is_checked_atomically_at_commit(workbench):
    client, container = workbench
    case, _document = _create_case_and_document(client)
    job = client.post(
        f"/api/v1/cases/{case['case_id']}/reviews",
        json={},
        headers={**_auth(), "Idempotency-Key": "commit-time-revision-drift"},
    ).json()["job"]
    entered = Event()
    release = Event()

    def slow_review(request: AgentReviewRequest) -> dict:
        entered.set()
        assert release.wait(timeout=5)
        return _agent_result(request)

    container.service.agent = CallableAgentClient(slow_review)
    with ThreadPoolExecutor(max_workers=1) as executor:
        future = executor.submit(
            container.service.execute_job,
            org_id="org-a",
            job_id=job["job_id"],
        )
        assert entered.wait(timeout=2)
        assert (
            client.post(
                f"/api/v1/cases/{case['case_id']}/documents",
                json={
                    "document_type": "PL",
                    "filename": "during-agent.txt",
                    "content_text": "concurrent Case mutation",
                },
                headers=_auth(),
            ).status_code
            == 201
        )
        release.set()
        run = future.result(timeout=5)
    assert run.review_disposition.value == "human_review_required"
    assert "case_revision_drift" in run.completion_eligibility.unmet_reason_codes
    assert "rerun_after_case_revision_change" in {item.action_code for item in run.required_actions}


def test_run_decision_is_atomic_and_auditor_can_read_it(workbench):
    client, container = workbench
    case, _document = _create_case_and_document(client)
    job = client.post(
        f"/api/v1/cases/{case['case_id']}/reviews",
        json={},
        headers={**_auth(), "Idempotency-Key": "atomic-run-decision"},
    ).json()["job"]
    run = container.service.execute_job(org_id="org-a", job_id=job["job_id"])
    approver = Principal(subject="approver-1", org_id="org-a", roles=frozenset({Role.approver}))

    def decide(value: str):
        return container.service.decide(
            approver,
            run.review_run_id,
            HumanDecisionCreate(decision=value, reason=f"concurrent {value}"),
        )

    with ThreadPoolExecutor(max_workers=2) as executor:
        futures = [executor.submit(decide, value) for value in ("approve", "reject")]
    results = []
    failures = []
    for future in futures:
        try:
            results.append(future.result())
        except ConflictError as exc:
            failures.append(exc)
    assert len(results) == 1
    assert len(failures) == 1
    response = client.get(
        f"/api/v1/review-runs/{run.review_run_id}/decisions",
        headers=_auth("auditor-token"),
    )
    assert response.status_code == 200
    assert len(response.json()["items"]) == 1


def test_correction_maker_checker_has_no_admin_self_approval_exception(workbench):
    client, container = workbench
    case = client.post(
        "/api/v1/cases",
        json={"title": "maker-checker"},
        headers=_auth("admin-token"),
    ).json()
    document = client.post(
        f"/api/v1/cases/{case['case_id']}/documents",
        json={
            "document_type": "CI",
            "filename": "invoice.txt",
            "content_text": "HS CODE: 0303540000",
        },
        headers=_auth("admin-token"),
    ).json()
    job = client.post(
        f"/api/v1/cases/{case['case_id']}/reviews",
        json={},
        headers={**_auth("admin-token"), "Idempotency-Key": "maker-checker-parent"},
    ).json()["job"]
    run = container.service.execute_job(org_id="org-a", job_id=job["job_id"])
    proposal = client.post(
        f"/api/v1/review-runs/{run.review_run_id}/correction-proposals",
        json={
            "parent_result_digest": run.result_digest,
            "parent_input_manifest_sha256": run.agent_input_manifest_sha256,
            "case_revision": run.case_revision,
            "corrections": [
                {
                    "document_id": document["record"]["document_id"],
                    "field_path": "fields.invoice_number",
                    "new_value": "INV-CORRECTED",
                    "reason": "source check",
                }
            ],
        },
        headers={
            **_auth("admin-token"),
            "Idempotency-Key": "maker-checker-proposal-create",
        },
    ).json()
    response = client.post(
        f"/api/v1/correction-proposals/{proposal['proposal_id']}/decisions",
        json={
            "decision": "approve",
            "reason": "self approval must fail",
            "parent_result_digest": run.result_digest,
            "case_revision": run.case_revision,
        },
        headers={**_auth("admin-token"), "Idempotency-Key": "admin-self-approval"},
    )
    assert response.status_code == 409


def test_readiness_fails_when_any_private_dependency_is_unavailable(workbench, monkeypatch):
    client, container = workbench
    assert client.get("/health/ready").status_code == 200
    monkeypatch.setattr(container.object_store, "health", lambda: False)
    assert client.get("/health/ready").status_code == 503


@pytest.mark.parametrize(
    ("mutator", "binding_key"),
    [
        (lambda result: result.__setitem__("review_as_of", "1999-01-01"), "review-date"),
        (lambda result: result.__setitem__("trade_type", "export"), "trade-type"),
        (
            lambda result: result.__setitem__("knowledge_snapshot_id", "attacker-generation"),
            "snapshot",
        ),
    ],
    ids=["review-as-of", "trade-type", "knowledge-snapshot"],
)
def test_agent_result_policy_and_snapshot_are_bound_before_storage(workbench, mutator, binding_key):
    client, container = workbench
    case, _document = _create_case_and_document(client)
    queued = client.post(
        f"/api/v1/cases/{case['case_id']}/reviews",
        json={"review_as_of": "2026-07-22", "trade_type": "import"},
        headers={**_auth(), "Idempotency-Key": f"agent-binding-{binding_key}"},
    ).json()["job"]

    def malicious(request: AgentReviewRequest) -> dict:
        result = _agent_result(request)
        mutator(result)
        return _rehash_agent_result(result)

    container.service.agent = CallableAgentClient(malicious)
    with pytest.raises(ConflictError):
        container.service.execute_job(org_id="org-a", job_id=queued["job_id"])
    stored = container.repository.get_job("org-a", queued["job_id"])
    assert stored is not None
    assert stored.status.value == "response_received_rejected"
    assert stored.agent_call_state.value == "response_received_rejected"
    assert container.repository.get_provider_result("org-a", queued["review_run_id"]) is None


def test_revision_child_result_cannot_change_parent_bound_review_policy(workbench):
    client, container = workbench
    case, document = _create_case_and_document(client)
    parent_job = client.post(
        f"/api/v1/cases/{case['case_id']}/reviews",
        json={"review_as_of": "2026-07-22", "trade_type": "import"},
        headers={**_auth(), "Idempotency-Key": "revision-binding-parent"},
    ).json()["job"]
    parent = container.service.execute_job(org_id="org-a", job_id=parent_job["job_id"])
    proposal = client.post(
        f"/api/v1/review-runs/{parent.review_run_id}/correction-proposals",
        json={
            "parent_result_digest": parent.result_digest,
            "parent_input_manifest_sha256": parent.agent_input_manifest_sha256,
            "case_revision": parent.case_revision,
            "corrections": [
                {
                    "document_id": document["record"]["document_id"],
                    "field_path": "fields.invoice_number",
                    "new_value": "INV-CORRECTED",
                    "reason": "source rechecked",
                    "confidence": 1.0,
                }
            ],
        },
        headers={**_auth(), "Idempotency-Key": "revision-binding-create"},
    ).json()
    approval = client.post(
        f"/api/v1/correction-proposals/{proposal['proposal_id']}/decisions",
        json={
            "decision": "approve",
            "reason": "independent source review completed",
            "parent_result_digest": parent.result_digest,
            "case_revision": parent.case_revision,
        },
        headers={**_auth("approver-token"), "Idempotency-Key": "revision-binding-approval"},
    )
    assert approval.status_code == 201, approval.text
    child_job = approval.json()["job"]

    def malicious_revision(request):
        child_result = _agent_result(request.revised_request)
        child_result["review_as_of"] = "1999-01-01"
        _rehash_agent_result(child_result)
        return {
            "parent_result_sha256": request.parent_result_sha256,
            "child_result": child_result,
            "applied_decision_ids": [item.decision_id for item in request.decisions],
        }

    container.service.agent = CallableAgentClient(
        _agent_result,
        revise=malicious_revision,
    )
    with pytest.raises(ConflictError):
        container.service.execute_job(org_id="org-a", job_id=child_job["job_id"])
    stored = container.repository.get_job("org-a", child_job["job_id"])
    assert stored is not None
    assert stored.status.value == "response_received_rejected"
    assert container.repository.get_provider_result("org-a", child_job["review_run_id"]) is None


def test_case_chat_is_idempotent_and_rate_bounded(workbench):
    client, container = workbench
    case, _document = _create_case_and_document(client)
    queued = client.post(
        f"/api/v1/cases/{case['case_id']}/reviews",
        json={"review_as_of": "2026-07-22"},
        headers={**_auth(), "Idempotency-Key": "chat-rate-parent"},
    ).json()["job"]
    run = container.service.execute_job(org_id="org-a", job_id=queued["job_id"])
    url = f"/api/v1/review-runs/{run.review_run_id}/answers"
    for index in range(10):
        response = client.post(
            url,
            json={
                "question": f"근거 질문 {index}",
                "bound_result_digest": run.result_digest,
            },
            headers={**_auth(), "Idempotency-Key": f"chat-rate-{index}"},
        )
        assert response.status_code == 201, response.text
    limited = client.post(
        url,
        json={"question": "한도 초과", "bound_result_digest": run.result_digest},
        headers={**_auth(), "Idempotency-Key": "chat-rate-over-limit"},
    )
    assert limited.status_code == 429
    assert limited.headers["retry-after"] == "60"
    assert limited.json()["code"] == "interaction_quota_exceeded"


def test_case_chat_has_a_durable_single_call_barrier_under_concurrency(workbench):
    client, container = workbench
    case, _document = _create_case_and_document(client)
    queued = client.post(
        f"/api/v1/cases/{case['case_id']}/reviews",
        json={},
        headers={**_auth(), "Idempotency-Key": "chat-call-once-parent"},
    ).json()["job"]
    run = container.service.execute_job(org_id="org-a", job_id=queued["job_id"])
    started = Event()
    release = Event()
    calls = 0

    def answer(_result, _question, question_id):
        nonlocal calls
        calls += 1
        started.set()
        assert release.wait(timeout=5)
        fact = _result["fact_ledger"]["entries"][0]
        return AgentAnswerResponse(
            question_id=question_id,
            answer="한 번만 생성된 근거 기반 답변입니다.",
            status="answered",
            citations=[{"kind": fact["kind"], "reference_id": fact["reference_id"]}],
            generation_mode="deterministic_fallback",
        )

    container.service.agent = CallableAgentClient(_agent_result, answer=answer)
    url = f"/api/v1/review-runs/{run.review_run_id}/answers"
    payload = {"question": "근거를 설명해 주세요.", "bound_result_digest": run.result_digest}
    headers = {**_auth(), "Idempotency-Key": "chat-call-once"}
    with ThreadPoolExecutor(max_workers=2) as executor:
        first = executor.submit(client.post, url, json=payload, headers=headers)
        assert started.wait(timeout=5)
        duplicate = executor.submit(client.post, url, json=payload, headers=headers)
        duplicate_response = duplicate.result(timeout=5)
        assert duplicate_response.status_code == 409
        release.set()
        first_response = first.result(timeout=5)
    assert first_response.status_code == 201
    replay = client.post(url, json=payload, headers=headers)
    assert replay.status_code == 201
    assert replay.json() == first_response.json()
    assert calls == 1


def test_case_chat_response_persistence_failure_closes_the_call_barrier(workbench, monkeypatch):
    client, container = workbench
    case, _document = _create_case_and_document(client)
    queued = client.post(
        f"/api/v1/cases/{case['case_id']}/reviews",
        json={},
        headers={**_auth(), "Idempotency-Key": "chat-persistence-parent"},
    ).json()["job"]
    run = container.service.execute_job(org_id="org-a", job_id=queued["job_id"])
    calls = 0

    def answer(_result, _question, question_id):
        nonlocal calls
        calls += 1
        fact = _result["fact_ledger"]["entries"][0]
        return AgentAnswerResponse(
            question_id=question_id,
            answer="영속화 직전까지 유효한 답변입니다.",
            status="answered",
            citations=[{"kind": fact["kind"], "reference_id": fact["reference_id"]}],
            generation_mode="deterministic_fallback",
        )

    container.service.agent = CallableAgentClient(_agent_result, answer=answer)
    original_complete = container.repository.complete_chat_interaction
    monkeypatch.setattr(
        container.repository,
        "complete_chat_interaction",
        lambda **_kwargs: (_ for _ in ()).throw(OSError("chat commit unavailable")),
    )
    url = f"/api/v1/review-runs/{run.review_run_id}/answers"
    payload = {"question": "영속화 경계를 확인합니다.", "bound_result_digest": run.result_digest}
    headers = {**_auth(), "Idempotency-Key": "chat-persistence-once"}
    with pytest.raises(OSError, match="chat commit unavailable"):
        client.post(url, json=payload, headers=headers)
    monkeypatch.setattr(container.repository, "complete_chat_interaction", original_complete)
    replay = client.post(url, json=payload, headers=headers)
    assert replay.status_code == 409
    interaction = next(iter(container.repository._chat_interactions.values()))
    assert interaction.state.value == "response_persistence_uncertain"
    assert calls == 1


def test_oidc_verifier_checks_signature_issuer_audience_exp_org_and_roles(monkeypatch):
    private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    verifier = OidcJwtVerifier(
        issuer="https://issuer.example.invalid",
        audience="workbench-api",
        jwks_url="https://issuer.example.invalid/jwks",
    )
    monkeypatch.setattr(
        verifier,
        "_jwks",
        SimpleNamespace(
            get_signing_keys=lambda refresh=False: [
                SimpleNamespace(key_id="key-1", key=private_key.public_key())
            ]
        ),
    )
    now = datetime.now(UTC)
    claims = {
        "iss": "https://issuer.example.invalid",
        "aud": "workbench-api",
        "sub": "reviewer-1",
        "org_id": "org-a",
        "roles": ["reviewer"],
        "iat": now,
        "exp": now + timedelta(minutes=5),
    }
    token = jwt.encode(claims, private_key, algorithm="RS256", headers={"kid": "key-1"})
    assert verifier.verify(token).roles == frozenset({Role.reviewer})

    monkeypatch.setattr(
        verifier,
        "_jwks",
        SimpleNamespace(
            get_signing_keys=lambda refresh=False: [
                SimpleNamespace(key_id="key-1", key=private_key.public_key())
            ],
        ),
    )
    assert verifier.health() is True

    wrong_audience = jwt.encode(
        {**claims, "aud": "wrong"},
        private_key,
        algorithm="RS256",
        headers={"kid": "key-1"},
    )
    with pytest.raises(AuthorizationError):
        verifier.verify(wrong_audience)
    expired = jwt.encode(
        {**claims, "iat": now - timedelta(minutes=10), "exp": now - timedelta(minutes=5)},
        private_key,
        algorithm="RS256",
        headers={"kid": "key-1"},
    )
    with pytest.raises(AuthorizationError):
        verifier.verify(expired)
    for malformed_claims in (
        {**claims, "sub": ["reviewer-1"]},
        {**claims, "org_id": {"value": "org-a"}},
        {**claims, "sub": "../invalid"},
        {**claims, "org_id": "org a"},
    ):
        malformed = jwt.encode(
            malformed_claims,
            private_key,
            algorithm="RS256",
            headers={"kid": "key-1"},
        )
        with pytest.raises(AuthorizationError):
            verifier.verify(malformed)

    non_string_roles = jwt.encode(
        {**claims, "roles": ["reviewer", {"role": "admin"}, ["admin"]]},
        private_key,
        algorithm="RS256",
        headers={"kid": "key-1"},
    )
    assert verifier.verify(non_string_roles).roles == frozenset({Role.reviewer})


def test_oidc_unknown_kid_is_negative_cached_and_refresh_is_singleflight(monkeypatch):
    private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    verifier = OidcJwtVerifier(
        issuer="https://issuer.example.invalid",
        audience="workbench-api",
        jwks_url="https://issuer.example.invalid/jwks",
    )
    calls: list[bool] = []

    def keys(*, refresh: bool = False):
        calls.append(refresh)
        return [SimpleNamespace(key_id="known-key", key=private_key.public_key())]

    monkeypatch.setattr(verifier, "_jwks", SimpleNamespace(get_signing_keys=keys))
    now = datetime.now(UTC)
    token = jwt.encode(
        {
            "iss": "https://issuer.example.invalid",
            "aud": "workbench-api",
            "sub": "reviewer-1",
            "org_id": "org-a",
            "roles": ["reviewer"],
            "iat": now,
            "exp": now + timedelta(minutes=5),
        },
        private_key,
        algorithm="RS256",
        headers={"kid": "unknown-key"},
    )
    for _ in range(5):
        with pytest.raises(AuthorizationError):
            verifier.verify(token)
    assert calls.count(True) == 1


def test_production_settings_require_all_secret_and_identity_boundaries():
    with pytest.raises(ValueError, match="production configuration is incomplete"):
        Settings(environment="production")
