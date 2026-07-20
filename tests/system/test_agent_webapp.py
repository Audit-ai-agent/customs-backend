from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from customs_ai_agent.schemas.models import (
    EvidenceRetrievalResponse,
    RetrievalItemResult,
)
from integration.agent_webapp import _readiness_fetcher, create_app
from integration.review_service import ReadinessError, execute_review
from operational_db import InMemoryOperationalRepository


class CompleteNoRequirementRetriever:
    def retrieve(self, request):
        return EvidenceRetrievalResponse(
            request_id=request.request_id,
            status="ok",
            knowledge_snapshot_id="live-web-test-snapshot",
            results=[
                RetrievalItemResult(
                    item_id=item.item_id,
                    hs_resolution="unique_descendant",
                    resolved_hsk10="7318151000",
                    candidate_hsk10s=["7318151000"],
                    coverage="complete",
                    requirements_found=False,
                    evidence=[],
                    errors=[],
                )
                for item in request.items
            ],
            errors=[],
        )


@pytest.fixture
def web_context():
    application = create_app()
    repository = InMemoryOperationalRepository()
    retriever = CompleteNoRequirementRetriever()
    application.state.repository_factory = lambda: repository
    application.state.readiness_fetcher = lambda _url: {
        "status": "ready",
        "knowledge_snapshot_id": "live-web-test-snapshot",
        "supported_review_as_of": "2026-07-10",
        "law_index": {
            "expected": 1007,
            "postgres_indexed": 1007,
            "qdrant": 1007,
        },
    }
    application.state.ops_health_checker = lambda: {"status": "ready"}
    application.state.review_executor = lambda command, repository=None: execute_review(
        command,
        repository=repository,
        retriever=retriever,
    )
    return TestClient(application), application, repository


def test_root_serves_visual_agent_console_with_truthful_mode_labels(web_context):
    client, _application, _repository = web_context

    response = client.get("/")

    assert response.status_code == 200
    assert "AI Agent" in response.text
    assert "MOCK" in response.text
    assert "LIVE" in response.text
    assert "RAG" in response.text


def test_health_reports_evidence_ops_and_execution_boundaries(web_context):
    client, _application, _repository = web_context

    payload = client.get("/api/health").json()

    assert payload["status"] == "ready"
    assert payload["evidence"]["supported_review_as_of"] == "2026-07-10"
    assert payload["evidence"]["law_index"]["qdrant"] == 1007
    assert payload["operational_db"]["status"] == "ready"
    assert payload["modes"] == {
        "document_input": "MOCK",
        "extraction": "MOCK",
        "agent_workflow": "LIVE",
        "regulation_retriever": "LIVE_HTTP",
        "knowledge_db": "LIVE",
        "operational_db": "LIVE",
    }


def test_health_is_degraded_without_hiding_dependency_failure(web_context):
    client, application, _repository = web_context
    application.state.readiness_fetcher = lambda _url: (_ for _ in ()).throw(
        ReadinessError("Evidence Service unavailable")
    )

    response = client.get("/api/health")

    assert response.status_code == 200
    assert response.json()["status"] == "degraded"
    assert response.json()["evidence"]["status"] == "failed"
    assert response.json()["evidence"]["error"] == (
        "Evidence Service readiness check failed"
    )


def test_bound_readiness_fetcher_repr_never_exposes_token() -> None:
    token = "redaction-test-marker"

    fetcher = _readiness_fetcher(token)

    assert token not in repr(fetcher)


def test_scenarios_endpoint_exposes_real_agent_fixtures(web_context):
    client, _application, _repository = web_context

    response = client.get("/api/scenarios")

    assert response.status_code == 200
    scenarios = response.json()["scenarios"]
    assert scenarios == sorted(scenarios)
    assert "happy_path" in scenarios
    assert "low_evidence_needs_review" in scenarios
    assert "origin_mismatch_blocked" in scenarios


def test_create_review_returns_full_snapshot_and_persistence_proof(web_context):
    client, _application, repository = web_context

    response = client.post(
        "/api/reviews",
        json={
            "scenario_id": "happy_path",
            "review_run_id": "visual-api-run-1",
            "review_as_of": "2026-07-10",
            "trade_type": "import",
            "submitted_document_codes": [],
            "store": True,
        },
    )

    assert response.status_code == 200
    payload = response.json()
    assert set(payload) == {
        "summary",
        "snapshot",
        "reused",
        "stored",
        "persistence_verified",
        "hashes",
        "counts",
        "system_status",
    }
    assert payload["summary"]["review_run_id"] == "visual-api-run-1"
    assert payload["summary"]["final_status"] == "completed"
    assert payload["summary"]["retrieval_status"] == "ok"
    assert payload["stored"] is True
    assert payload["reused"] is False
    assert payload["persistence_verified"] is True
    assert payload["hashes"]["before"] == payload["hashes"]["after"]
    assert payload["counts"]["documents"] == 4
    assert payload["counts"]["audit_events"] >= 10
    assert payload["system_status"] == "PASS"
    assert payload["snapshot"]["request"]["llm_mode"] == "mock"
    assert repository.get_run("visual-api-run-1") is not None


def test_get_review_reads_back_stored_immutable_snapshot(web_context):
    client, _application, _repository = web_context
    request = {
        "scenario_id": "happy_path",
        "review_run_id": "visual-api-readback",
        "review_as_of": "2026-07-10",
        "store": True,
    }
    created = client.post("/api/reviews", json=request).json()

    response = client.get("/api/reviews/visual-api-readback")

    assert response.status_code == 200
    readback = response.json()
    assert readback["snapshot"] == created["snapshot"]
    assert readback["reused"] is True
    assert readback["stored"] is True
    assert readback["persistence_verified"] is True
    assert readback["hashes"]["before"] == readback["hashes"]["after"]


def test_post_is_idempotent_and_rejects_same_run_with_different_request(web_context):
    client, _application, _repository = web_context
    request = {
        "scenario_id": "happy_path",
        "review_run_id": "visual-api-idempotent",
        "review_as_of": "2026-07-10",
        "trade_type": "import",
        "store": True,
    }

    first = client.post("/api/reviews", json=request)
    reused = client.post("/api/reviews", json=request)
    conflict = client.post(
        "/api/reviews",
        json={**request, "trade_type": "export"},
    )

    assert first.status_code == 200
    assert first.json()["reused"] is False
    assert reused.status_code == 200
    assert reused.json()["reused"] is True
    assert reused.json()["hashes"] == first.json()["hashes"]
    assert conflict.status_code == 409
    assert conflict.json()["detail"] == "Review conflict or invalid state"


def test_create_review_can_run_without_claiming_database_persistence(web_context):
    client, _application, repository = web_context

    response = client.post(
        "/api/reviews",
        json={
            "scenario_id": "happy_path",
            "review_run_id": "visual-api-no-store",
            "review_as_of": "2026-07-10",
            "store": False,
        },
    )

    assert response.status_code == 200
    assert response.json()["stored"] is False
    assert response.json()["persistence_verified"] is None
    assert response.json()["hashes"]["after"] is None
    assert repository.get_run("visual-api-no-store") is None


@pytest.mark.parametrize(
    ("error", "expected_status"),
    [
        (KeyError("unknown scenario"), 400),
        (ValueError("different request"), 409),
        (ReadinessError("INTERNAL_READINESS_SENTINEL"), 503),
        (RuntimeError("unexpected"), 500),
    ],
)
def test_create_review_maps_domain_and_dependency_errors(
    web_context, error, expected_status
):
    client, application, _repository = web_context

    def fail(*_args, **_kwargs):
        raise error

    application.state.review_executor = fail
    response = client.post(
        "/api/reviews",
        json={
            "scenario_id": "happy_path",
            "review_run_id": "visual-api-error",
            "review_as_of": "2026-07-10",
            "store": False,
        },
    )

    assert response.status_code == expected_status
    expected_detail = {
        400: "Unknown Agent scenario",
        409: "Review conflict or invalid state",
        503: "Evidence Service is not ready",
        500: "Review execution failed",
    }[expected_status]
    assert response.json()["detail"] == expected_detail
    assert str(error) not in response.text


def test_request_validation_and_missing_readback_have_explicit_http_statuses(web_context):
    client, _application, _repository = web_context

    invalid = client.post("/api/reviews", json={"scenario_id": ""})
    invalid_rag_url = client.post(
        "/api/reviews",
        json={
            "scenario_id": "happy_path",
            "review_as_of": "2026-07-10",
            "rag_url": "not-an-http-url",
            "store": False,
        },
    )
    external_rag_url = client.post(
        "/api/reviews",
        json={
            "scenario_id": "happy_path",
            "review_as_of": "2026-07-10",
            "rag_url": "https://example.com",
            "store": False,
        },
    )
    missing = client.get("/api/reviews/does-not-exist")

    assert invalid.status_code == 422
    assert invalid_rag_url.status_code == 422
    assert external_rag_url.status_code == 422
    assert "must target localhost" in str(external_rag_url.json())
    assert missing.status_code == 404


def test_remote_clients_are_rejected_before_reaching_unauthenticated_routes():
    application = create_app()
    with TestClient(application, client=("198.51.100.10", 50000)) as client:
        response = client.get("/")

    assert response.status_code == 403
    assert "local machine only" in response.json()["detail"]
