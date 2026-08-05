from __future__ import annotations

from fastapi.testclient import TestClient

from tests.product.agent_v3_fixture import build_agent_v3_result

from customs_backend.adapters import CallableAgentClient, InMemoryObjectStore
from customs_backend.api import Container, create_app
from customs_backend.auth import StaticTokenVerifier
from customs_backend.domain import Principal, Role
from customs_backend.repository import InMemoryWorkbenchRepository
from customs_backend.service import WorkbenchService
from customs_backend.settings import Settings


def _client() -> TestClient:
    settings = Settings(_env_file=None, environment="test")
    principal = Principal(
        subject="reviewer-1",
        org_id="local-dev",
        roles=frozenset({Role.reviewer}),
    )
    repository = InMemoryWorkbenchRepository()
    objects = InMemoryObjectStore()
    agent = CallableAgentClient(build_agent_v3_result)
    service = WorkbenchService(
        repository=repository,
        object_store=objects,
        agent=agent,
    )
    return TestClient(
        create_app(
            settings=settings,
            container=Container(
                settings=settings,
                repository=repository,
                object_store=objects,
                agent=agent,
                token_verifier=StaticTokenVerifier({"test-token": principal}),
                service=service,
            ),
        )
    )


def _auth() -> dict[str, str]:
    return {"Authorization": "Bearer test-token"}


def test_v1_case_document_review_flow_always_needs_human_review() -> None:
    with _client() as client:
        case = client.post("/api/v1/cases", json={"title": "Invoice review"}, headers=_auth())
        assert case.status_code == 201, case.text
        case_id = case.json()["case_id"]

        document = client.post(
            f"/api/v1/cases/{case_id}/documents",
            json={
                "document_type": "CI",
                "filename": "invoice.txt",
                "content_text": "Commercial invoice INV-001",
            },
            headers=_auth(),
        )
        assert document.status_code == 201, document.text
        document_id = document.json()["record"]["document_id"]

        queued = client.post(
            f"/api/v1/cases/{case_id}/reviews",
            json={"trade_type": "import"},
            headers={**_auth(), "Idempotency-Key": "review-once-001"},
        )
        assert queued.status_code == 202, queued.text
        assert queued.json()["job"]["status"] == "succeeded"
        job_id = queued.json()["job"]["job_id"]

        retry = client.post(
            f"/api/v1/cases/{case_id}/reviews",
            json={"trade_type": "import"},
            headers={**_auth(), "Idempotency-Key": "review-once-001"},
        )
        assert retry.status_code == 202
        assert retry.json()["idempotency_reused"] is True
        assert retry.json()["job"]["job_id"] == job_id

        content = client.get(
            f"/api/v1/cases/{case_id}/documents/{document_id}/content", headers=_auth()
        )
        assert content.status_code == 200
        assert content.json()["content_text"] == "Commercial invoice INV-001"

        runs = client.get(f"/api/v1/cases/{case_id}/review-runs", headers=_auth())
        assert runs.status_code == 200, runs.text
        assert len(runs.json()["items"]) == 1
        run = runs.json()["items"][0]
        assert run["review_disposition"] == "human_review_required"
        assert run["assurance_level"] == "diagnostic"
        assert run["completion_eligibility"]["eligible"] is False
        assert "human_review_required" in run["completion_eligibility"]["unmet_reason_codes"]


def test_only_v1_browser_paths_are_public() -> None:
    with _client() as client:
        assert client.get("/api/v2/cases", headers=_auth()).status_code == 404


def test_invalid_agent_response_becomes_a_simple_failed_job() -> None:
    settings = Settings(_env_file=None, environment="test")
    principal = Principal(
        subject="reviewer-1", org_id="local-dev", roles=frozenset({Role.reviewer})
    )
    repository = InMemoryWorkbenchRepository()
    objects = InMemoryObjectStore()
    agent = CallableAgentClient(lambda _request: {"invalid": True})
    service = WorkbenchService(repository=repository, object_store=objects, agent=agent)
    app = create_app(
        settings=settings,
        container=Container(
            settings=settings,
            repository=repository,
            object_store=objects,
            agent=agent,
            token_verifier=StaticTokenVerifier({"test-token": principal}),
            service=service,
        ),
    )
    with TestClient(app) as client:
        case = client.post("/api/v1/cases", json={"title": "Invalid response"}, headers=_auth())
        case_id = case.json()["case_id"]
        client.post(
            f"/api/v1/cases/{case_id}/documents",
            json={"document_type": "CI", "filename": "invoice.txt", "content_text": "INV"},
            headers=_auth(),
        )
        response = client.post(
            f"/api/v1/cases/{case_id}/reviews",
            json={},
            headers={**_auth(), "Idempotency-Key": "invalid-agent-001"},
        )

    assert response.status_code == 202
    assert response.json()["job"]["status"] == "failed"
    assert response.json()["job"]["error_code"] == "ConflictError"
