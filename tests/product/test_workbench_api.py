from __future__ import annotations

import json
from pathlib import Path

import httpx
from fastapi.testclient import TestClient

from tests.product.agent_v3_fixture import build_agent_v3_result

from customs_backend.adapters import CallableAgentClient, HttpAgentClient, InMemoryObjectStore
from customs_backend.api import Container, create_app
from customs_backend.auth import StaticTokenVerifier
from customs_backend.domain import AgentReviewRequest, Principal, Role
from customs_backend.repository import InMemoryWorkbenchRepository
from customs_backend.scenario_transcript import (
    FunctionalTranscriptController,
    ScenarioTranscriptRoot,
    verify_run,
)
from customs_backend.service import WorkbenchService
from customs_backend.settings import Settings


def _client(*, include_finding: bool = False) -> TestClient:
    settings = Settings(_env_file=None, environment="test")
    principal = Principal(
        subject="reviewer-1",
        org_id="local-dev",
        roles=frozenset({Role.reviewer}),
    )
    repository = InMemoryWorkbenchRepository()
    objects = InMemoryObjectStore()
    agent = CallableAgentClient(
        lambda request: build_agent_v3_result(request, include_finding=include_finding)
    )
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


def _functional_settings() -> Settings:
    return Settings(
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
        assert set(run) == {
            "review_run_id",
            "case_id",
            "job_id",
            "review_as_of",
            "trade_type",
            "execution_status",
            "assessment",
            "review_disposition",
            "result_digest",
            "finding_count",
            "reviewer_decision_count",
            "created_at",
        }
        assert run["review_disposition"] == "human_review_required"
        detail = client.get(f"/api/v1/review-runs/{run['review_run_id']}", headers=_auth())
        assert detail.status_code == 200, detail.text
        assert detail.json()["assurance_level"] == "diagnostic"
        assert detail.json()["completion_eligibility"]["eligible"] is False
        assert (
            "human_review_required" in detail.json()["completion_eligibility"]["unmet_reason_codes"]
        )
        assert detail.json()["agent_schema_version"] == "3.1"
        assert detail.json()["agent_jurisdiction"] == "KR"
        assert detail.json()["agent_requires_human_review"] is True
        assert detail.json()["workflow"]["status"] == "completed"
        assert detail.json()["document_analyses"]


def test_document_replacement_preserves_old_run_and_excludes_superseded_input() -> None:
    with _client() as client:
        case = client.post("/api/v1/cases", json={"title": "Replacement"}, headers=_auth())
        case_id = case.json()["case_id"]
        original = client.post(
            f"/api/v1/cases/{case_id}/documents",
            json={
                "document_type": "CI",
                "filename": "invoice-v1.txt",
                "content_text": "Commercial invoice version one",
            },
            headers=_auth(),
        )
        assert original.status_code == 201, original.text
        original_id = original.json()["record"]["document_id"]
        first = client.post(
            f"/api/v1/cases/{case_id}/reviews",
            json={},
            headers={**_auth(), "Idempotency-Key": "replacement-first-001"},
        )
        assert first.status_code == 202
        first_run_id = first.json()["job"]["review_run_id"]

        rejected = client.post(
            f"/api/v1/cases/{case_id}/documents",
            json={
                "document_type": "PL",
                "filename": "packing.txt",
                "content_text": "Packing list",
                "replaces_document_id": original_id,
            },
            headers=_auth(),
        )
        assert rejected.status_code == 409

        replacement = client.post(
            f"/api/v1/cases/{case_id}/documents",
            json={
                "document_type": "CI",
                "filename": "invoice-v2.txt",
                "content_text": "Commercial invoice corrected version two",
                "replaces_document_id": original_id,
            },
            headers=_auth(),
        )
        assert replacement.status_code == 201, replacement.text
        replacement_id = replacement.json()["record"]["document_id"]

        documents = client.get(f"/api/v1/cases/{case_id}/documents", headers=_auth())
        assert documents.status_code == 200
        listed = {
            item["record"]["document_id"]: item["record"] for item in documents.json()["items"]
        }
        assert listed[original_id]["superseded_by_document_id"] == replacement_id
        assert listed[replacement_id]["replaces_document_id"] == original_id

        second = client.post(
            f"/api/v1/cases/{case_id}/reviews",
            json={},
            headers={**_auth(), "Idempotency-Key": "test-key"},
        )
        assert second.status_code == 202
        second_run_id = second.json()["job"]["review_run_id"]
        first_detail = client.get(f"/api/v1/review-runs/{first_run_id}", headers=_auth()).json()
        second_detail = client.get(f"/api/v1/review-runs/{second_run_id}", headers=_auth()).json()
        assert [item["document_id"] for item in first_detail["input_document_manifest"]] == [
            original_id
        ]
        assert [item["document_id"] for item in second_detail["input_document_manifest"]] == [
            replacement_id
        ]


def test_reviewer_decisions_are_append_only_and_do_not_change_agent_result_digest() -> None:
    with _client(include_finding=True) as client:
        case = client.post("/api/v1/cases", json={"title": "Decision"}, headers=_auth())
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
        assert document.status_code == 201
        review = client.post(
            f"/api/v1/cases/{case_id}/reviews",
            json={},
            headers={**_auth(), "Idempotency-Key": "decision-review-001"},
        )
        assert review.status_code == 202
        run_id = review.json()["job"]["review_run_id"]
        before = client.get(f"/api/v1/review-runs/{run_id}", headers=_auth()).json()
        digest = before["result_digest"]

        first = client.put(
            f"/api/v1/review-runs/{run_id}/findings/finding_fixture_quantity/decision",
            json={"status": "confirmed", "reason": "Invoice quantity was verified."},
            headers=_auth(),
        )
        assert first.status_code == 200, first.text
        assert first.json()["status"] == "confirmed"
        assert first.json()["decided_by"] == "reviewer-1"
        assert len(first.json()["history"]) == 1

        second = client.put(
            f"/api/v1/review-runs/{run_id}/findings/finding_fixture_quantity/decision",
            json={"status": "resolved", "reason": "Corrected invoice was attached."},
            headers=_auth(),
        )
        assert second.status_code == 200, second.text
        assert second.json()["status"] == "resolved"
        assert len(second.json()["history"]) == 2

        after = client.get(f"/api/v1/review-runs/{run_id}", headers=_auth()).json()
        assert after["result_digest"] == digest
        assert after["completion_eligibility"]["eligible"] is False
        assert after["reviewer_decisions"][0]["status"] == "resolved"
        assert len(after["reviewer_decisions"][0]["history"]) == 2
        summary = client.get(f"/api/v1/cases/{case_id}/review-runs", headers=_auth()).json()[
            "items"
        ][0]
        assert summary["finding_count"] == 1
        assert summary["reviewer_decision_count"] == 1

        blank = client.put(
            f"/api/v1/review-runs/{run_id}/findings/finding_fixture_quantity/decision",
            json={"status": "resolved", "reason": "   "},
            headers=_auth(),
        )
        assert blank.status_code == 422
        missing = client.put(
            f"/api/v1/review-runs/{run_id}/findings/unknown/decision",
            json={"status": "resolved", "reason": "No matching finding."},
            headers=_auth(),
        )
        assert missing.status_code == 404


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


def test_functional_uat_automatically_completes_the_six_local_transcript_files(
    tmp_path: Path,
) -> None:
    settings = _functional_settings()
    principal = Principal(
        subject="reviewer-1", org_id="local-dev", roles=frozenset({Role.reviewer})
    )
    transcript_root = tmp_path / "_local" / "state" / "scenario-runs"
    transcript = FunctionalTranscriptController(
        root=ScenarioTranscriptRoot.create(
            transcript_root, secret_values=(settings.agent_service_token.get_secret_value(),)
        )
    )

    def handler(request: httpx.Request) -> httpx.Response:
        agent_request = AgentReviewRequest.model_validate(json.loads(request.content))
        return httpx.Response(
            200,
            json=build_agent_v3_result(agent_request),
            headers={"X-Customs-Service-Mode": "functional-uat"},
            request=request,
        )

    agent = HttpAgentClient(
        base_url="http://127.0.0.1:8090",
        service_token=settings.agent_service_token.get_secret_value(),
        service_mode="functional-uat",
        review_trace=transcript.record_backend_agent,
        transport=httpx.MockTransport(handler),
    )
    repository = InMemoryWorkbenchRepository()
    objects = InMemoryObjectStore()
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
            transcript=transcript,
        ),
    )
    with TestClient(app) as client:
        case = client.post(
            "/api/v1/cases", json={"title": "Functional transcript"}, headers=_auth()
        )
        case_id = case.json()["case_id"]
        document = client.post(
            f"/api/v1/cases/{case_id}/documents",
            json={
                "document_type": "CI",
                "filename": "invoice.txt",
                "content_text": "actual service document",
            },
            headers=_auth(),
        )
        assert document.status_code == 201
        queued = client.post(
            f"/api/v1/cases/{case_id}/reviews",
            json={"trade_type": "import"},
            headers={**_auth(), "Idempotency-Key": "functional-transcript-001"},
        )
        assert queued.status_code == 202
        job = queued.json()["job"]
        run_id = job["review_run_id"]
        directory = transcript_root / run_id
        for stream, event in (("agent-data", "http_exchange"), ("claude", "response_received")):
            (directory / f"{stream}.jsonl").write_text(
                json.dumps(
                    {
                        "schema_version": "scenario-transcript/v1",
                        "event": event,
                        "payload": {"status": "agent-fixture"},
                    }
                )
                + "\n",
                encoding="utf-8",
            )
        assert client.get(f"/api/v1/jobs/{job['job_id']}", headers=_auth()).status_code == 200
        assert client.get(f"/api/v1/review-runs/{run_id}", headers=_auth()).status_code == 200

    verified = verify_run(transcript_root, run_id)
    assert verified["profile"] == "functional-uat"
    assert set(verified["streams"]) == {
        "public-api.jsonl",
        "backend-agent.jsonl",
        "agent-data.jsonl",
        "claude.jsonl",
    }
    public = (directory / "public-api.jsonl").read_text(encoding="utf-8")
    assert "Authorization" not in public
    assert settings.agent_service_token.get_secret_value() not in public
