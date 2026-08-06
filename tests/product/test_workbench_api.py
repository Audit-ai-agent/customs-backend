from __future__ import annotations

import json
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import httpx
from fastapi.testclient import TestClient

from tests.product.agent_v3_fixture import build_agent_v3_result

from customs_backend.adapters import CallableAgentClient, HttpAgentClient, InMemoryObjectStore
from customs_backend.api import Container, create_app
from customs_backend.auth import StaticTokenVerifier
from customs_backend.domain import (
    AgentReviewRequest,
    ConversationMessageCreate,
    Principal,
    Role,
    canonical_digest,
)
from customs_backend.repository import InMemoryWorkbenchRepository
from customs_backend.scenario_transcript import (
    FunctionalTranscriptController,
    ScenarioTranscriptRoot,
    verify_run,
)
from customs_backend.service import WorkbenchService
from customs_backend.settings import Settings


def _client(
    *, include_finding: bool = False, review_handler=None, answer_handler=None
) -> TestClient:
    settings = Settings(_env_file=None, environment="test")
    principal = Principal(
        subject="reviewer-1",
        org_id="local-dev",
        roles=frozenset({Role.reviewer}),
    )
    approver = Principal(
        subject="approver-1",
        org_id="local-dev",
        roles=frozenset({Role.approver}),
    )
    repository = InMemoryWorkbenchRepository()
    objects = InMemoryObjectStore()
    agent = CallableAgentClient(
        review_handler
        or (lambda request: build_agent_v3_result(request, include_finding=include_finding)),
        answer=answer_handler,
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
                token_verifier=StaticTokenVerifier(
                    {
                        "test-token": principal,
                        "approver-token": approver,
                        "reviewer-approver-token": Principal(
                            subject="reviewer-1",
                            org_id="local-dev",
                            roles=frozenset({Role.reviewer, Role.approver}),
                        ),
                    }
                ),
                service=service,
            ),
        )
    )


def _auth() -> dict[str, str]:
    return {"Authorization": "Bearer test-token"}


def _approver_auth() -> dict[str, str]:
    return {"Authorization": "Bearer approver-token"}


def _reviewer_approver_auth() -> dict[str, str]:
    return {"Authorization": "Bearer reviewer-approver-token"}


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
            "parent_review_run_id",
            "applied_correction_proposal_ids",
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
        assert detail.json()["agent_schema_version"] == "3.2"
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


def test_functional_uat_captures_review_report_chat_correction_and_child_run(
    tmp_path: Path,
) -> None:
    settings = _functional_settings()
    principal = Principal(
        subject="reviewer-1", org_id="local-dev", roles=frozenset({Role.reviewer})
    )
    approver = Principal(subject="approver-1", org_id="local-dev", roles=frozenset({Role.approver}))
    transcript_root = tmp_path / "_local" / "state" / "scenario-runs"
    transcript = FunctionalTranscriptController(
        root=ScenarioTranscriptRoot.create(
            transcript_root, secret_values=(settings.agent_service_token.get_secret_value(),)
        )
    )

    def handler(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content)
        if request.url.path == "/internal/agent/v2/assistant/answers":
            return httpx.Response(
                200,
                json={
                    "schema_version": "1.0",
                    "case_id": payload["case_id"],
                    "review_run_id": payload["review_run_id"],
                    "conversation_id": payload["conversation_id"],
                    "answer_scope": "clarification_needed",
                    "direct_answer": "The reviewer should inspect the recorded comparison first.",
                    "citations": [],
                    "suggested_questions": [],
                    "correction_draft": None,
                    "engine": {"kind": "deterministic", "model_id": "functional-transcript"},
                },
                headers={"X-Customs-Service-Mode": "functional-uat"},
                request=request,
            )
        agent_request = AgentReviewRequest.model_validate(payload)
        return httpx.Response(
            200,
            json=build_agent_v3_result(agent_request, include_finding=True),
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
            token_verifier=StaticTokenVerifier(
                {"test-token": principal, "approver-token": approver}
            ),
            service=service,
            transcript=transcript,
        ),
    )

    def write_agent_streams(review_run_id: str) -> None:
        directory = transcript_root / review_run_id
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

    transcript_secret = "sk-ant-abcdefghijklmnop"
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
        write_agent_streams(run_id)
        assert client.get(f"/api/v1/jobs/{job['job_id']}", headers=_auth()).status_code == 200
        # This creates the initial terminal checkpoint.  All following events
        # must still remain attached to the parent run and refresh it safely.
        assert client.get(f"/api/v1/review-runs/{run_id}", headers=_auth()).status_code == 200
        conversation = client.post(
            f"/api/v1/review-runs/{run_id}/assistant/conversations",
            json={"title": "Review follow-up"},
            headers=_auth(),
        )
        assert conversation.status_code == 201
        conversation_id = conversation.json()["conversation_id"]
        assert (
            client.get(
                f"/api/v1/review-runs/{run_id}/assistant/conversations", headers=_auth()
            ).status_code
            == 200
        )
        assistant = client.post(
            f"/api/v1/assistant/conversations/{conversation_id}/messages",
            json={"content": f"ANTHROPIC_API_KEY={transcript_secret}; what should I inspect?"},
            headers={**_auth(), "Idempotency-Key": "functional-chat-001"},
        )
        assert assistant.status_code == 200, assistant.text
        assert (
            client.get(
                f"/api/v1/assistant/conversations/{conversation_id}/messages", headers=_auth()
            ).status_code
            == 200
        )
        proposal = client.post(
            f"/api/v1/review-runs/{run_id}/correction-proposals",
            json={
                "document_id": document.json()["record"]["document_id"],
                "field_name": "invoice_number",
                "new_value": "INV-001-CORRECTED",
                "finding_id": "finding_fixture_quantity",
                "reason": f"ANTHROPIC_API_KEY={transcript_secret}; reviewer checked amendment.",
            },
            headers=_auth(),
        )
        assert proposal.status_code == 201
        proposal_id = proposal.json()["correction_proposal_id"]
        assert (
            client.get(
                f"/api/v1/review-runs/{run_id}/correction-proposals", headers=_auth()
            ).status_code
            == 200
        )
        assert (
            client.put(
                f"/api/v1/correction-proposals/{proposal_id}/decision",
                json={"status": "approved", "reason": "Independent local approval."},
                headers=_approver_auth(),
            ).status_code
            == 200
        )
        child = client.post(
            f"/api/v1/review-runs/{run_id}/revisions",
            json={"proposal_ids": [proposal_id]},
            headers={**_approver_auth(), "Idempotency-Key": "functional-child-001"},
        )
        assert child.status_code == 202
        child_job = child.json()["job"]
        child_run_id = child_job["review_run_id"]
        write_agent_streams(child_run_id)
        assert (
            client.get(f"/api/v1/jobs/{child_job['job_id']}", headers=_approver_auth()).status_code
            == 200
        )
        assert (
            client.get(f"/api/v1/review-runs/{child_run_id}", headers=_approver_auth()).status_code
            == 200
        )
        # Parent retrieval after the child exists proves explicit run binding:
        # it must remain in the parent's detailed report transcript.
        assert client.get(f"/api/v1/review-runs/{run_id}", headers=_auth()).status_code == 200

    verified = verify_run(transcript_root, run_id)
    assert verified["profile"] == "functional-uat"
    assert set(verified["streams"]) == {
        "public-api.jsonl",
        "backend-agent.jsonl",
        "agent-data.jsonl",
        "claude.jsonl",
    }
    assert verify_run(transcript_root, child_run_id)["profile"] == "functional-uat"
    directory = transcript_root / run_id
    public = (directory / "public-api.jsonl").read_text(encoding="utf-8")
    backend_agent = (directory / "backend-agent.jsonl").read_text(encoding="utf-8")
    parent_paths = [json.loads(line)["payload"]["path"] for line in public.splitlines() if line]
    assert f"/api/v1/review-runs/{run_id}/assistant/conversations" in parent_paths
    assert f"/api/v1/assistant/conversations/{conversation_id}/messages" in parent_paths
    assert f"/api/v1/review-runs/{run_id}/correction-proposals" in parent_paths
    assert f"/api/v1/correction-proposals/{proposal_id}/decision" in parent_paths
    assert f"/api/v1/review-runs/{run_id}/revisions" in parent_paths
    assert f"/api/v1/jobs/{child_job['job_id']}" not in parent_paths
    child_public = (transcript_root / child_run_id / "public-api.jsonl").read_text(encoding="utf-8")
    assert f"/api/v1/review-runs/{run_id}/correction-proposals" not in child_public
    assert "Authorization" not in public
    assert settings.agent_service_token.get_secret_value() not in public
    assert transcript_secret not in public
    assert transcript_secret not in backend_agent


def test_grounded_assistant_conversation_is_persisted_and_idempotent() -> None:
    with _client(include_finding=True) as client:
        case = client.post("/api/v1/cases", json={"title": "Assistant"}, headers=_auth())
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
        document_id = document.json()["record"]["document_id"]
        review = client.post(
            f"/api/v1/cases/{case_id}/reviews",
            json={},
            headers={**_auth(), "Idempotency-Key": "assistant-review-001"},
        )
        run_id = review.json()["job"]["review_run_id"]
        conversation = client.post(
            f"/api/v1/review-runs/{run_id}/assistant/conversations",
            json={"title": "Invoice question"},
            headers=_auth(),
        )
        assert conversation.status_code == 201, conversation.text
        assert set(conversation.json()) == {
            "conversation_id",
            "review_run_id",
            "title",
            "created_by",
            "created_at",
            "updated_at",
        }
        assert conversation.json()["review_run_id"] == run_id
        conversation_id = conversation.json()["conversation_id"]

        answer = client.post(
            f"/api/v1/assistant/conversations/{conversation_id}/messages",
            json={"content": "What should the reviewer inspect first?"},
            headers={**_auth(), "Idempotency-Key": "assistant-turn-001"},
        )
        assert answer.status_code == 200, answer.text
        exchange = answer.json()
        assert exchange["user_message"]["role"] == "user"
        assert exchange["assistant_message"]["role"] == "assistant"
        assert exchange["assistant_message"]["answer_scope"] == "clarification_needed"
        assert exchange["assistant_message"]["content"] == "Deterministic test assistant response."

        replay = client.post(
            f"/api/v1/assistant/conversations/{conversation_id}/messages",
            json={"content": "What should the reviewer inspect first?"},
            headers={**_auth(), "Idempotency-Key": "assistant-turn-001"},
        )
        assert replay.status_code == 200
        assert replay.json() == exchange
        conflict = client.post(
            f"/api/v1/assistant/conversations/{conversation_id}/messages",
            json={"content": "This is a different question."},
            headers={**_auth(), "Idempotency-Key": "assistant-turn-001"},
        )
        assert conflict.status_code == 409

        messages = client.get(
            f"/api/v1/assistant/conversations/{conversation_id}/messages", headers=_approver_auth()
        )
        assert messages.status_code == 200
        assert [item["role"] for item in messages.json()["items"]] == ["user", "assistant"]
        assert (
            client.get(
                f"/api/v1/review-runs/{run_id}/assistant/conversations", headers=_approver_auth()
            ).status_code
            == 200
        )
        assert document_id


def test_approved_correction_creates_an_immutable_child_review() -> None:
    with _client(include_finding=True) as client:
        case = client.post("/api/v1/cases", json={"title": "Correction"}, headers=_auth())
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
        document_id = document.json()["record"]["document_id"]
        parent_job = client.post(
            f"/api/v1/cases/{case_id}/reviews",
            json={},
            headers={**_auth(), "Idempotency-Key": "correction-parent-001"},
        ).json()["job"]
        parent_run_id = parent_job["review_run_id"]
        parent = client.get(f"/api/v1/review-runs/{parent_run_id}", headers=_auth()).json()

        proposed = client.post(
            f"/api/v1/review-runs/{parent_run_id}/correction-proposals",
            json={
                "document_id": document_id,
                "field_name": "invoice_number",
                "new_value": "INV-001-CORRECTED",
                "reason": "Reviewer verified the supplier amendment.",
                "finding_id": "finding_fixture_quantity",
            },
            headers=_auth(),
        )
        assert proposed.status_code == 201, proposed.text
        proposal = proposed.json()
        assert proposal["status"] == "proposed"
        assert "org_id" not in proposal
        assert "parent_result_digest" not in proposal
        proposal_id = proposal["correction_proposal_id"]

        assert (
            client.put(
                f"/api/v1/correction-proposals/{proposal_id}/decision",
                json={"status": "approved", "reason": "Approve a bounded re-review."},
                headers=_auth(),
            ).status_code
            == 403
        )
        assert (
            client.put(
                f"/api/v1/correction-proposals/{proposal_id}/decision",
                json={"status": "approved", "reason": "Attempt self-approval."},
                headers=_reviewer_approver_auth(),
            ).status_code
            == 409
        )
        approved = client.put(
            f"/api/v1/correction-proposals/{proposal_id}/decision",
            json={"status": "approved", "reason": "Approve a bounded re-review."},
            headers=_approver_auth(),
        )
        assert approved.status_code == 200, approved.text
        assert approved.json()["status"] == "approved"
        assert approved.json()["decision"]["decided_by"] == "approver-1"
        assert [item["event"] for item in approved.json()["history"]] == ["proposed", "approved"]

        revision = client.post(
            f"/api/v1/review-runs/{parent_run_id}/revisions",
            json={"proposal_ids": [proposal_id]},
            headers={**_approver_auth(), "Idempotency-Key": "revision-001"},
        )
        assert revision.status_code == 202, revision.text
        assert revision.json()["job"]["status"] == "succeeded"
        child_run_id = revision.json()["job"]["review_run_id"]
        child = client.get(f"/api/v1/review-runs/{child_run_id}", headers=_approver_auth())
        assert child.status_code == 200
        assert child.json()["parent_review_run_id"] == parent_run_id
        assert child.json()["applied_correction_proposal_ids"] == [proposal_id]
        assert child.json()["input_document_manifest"] == parent["input_document_manifest"]

        summaries = client.get(f"/api/v1/cases/{case_id}/review-runs", headers=_auth())
        assert summaries.status_code == 200
        child_summary = next(
            item for item in summaries.json()["items"] if item["review_run_id"] == child_run_id
        )
        assert child_summary["parent_review_run_id"] == parent_run_id
        assert child_summary["applied_correction_proposal_ids"] == [proposal_id]

        listed = client.get(
            f"/api/v1/review-runs/{parent_run_id}/correction-proposals", headers=_auth()
        )
        assert listed.status_code == 200
        applied = listed.json()["items"][0]
        assert applied["status"] == "approved"
        assert applied["applied_review_run_id"] == child_run_id
        assert [item["event"] for item in applied["history"]] == [
            "proposed",
            "approved",
            "applied",
        ]
        replay = client.post(
            f"/api/v1/review-runs/{parent_run_id}/revisions",
            json={"proposal_ids": [proposal_id]},
            headers={**_approver_auth(), "Idempotency-Key": "revision-001"},
        )
        assert replay.status_code == 202, replay.text
        assert replay.json()["idempotency_reused"] is True
        assert replay.json()["job"] == revision.json()["job"]
        repeated = client.post(
            f"/api/v1/review-runs/{parent_run_id}/revisions",
            json={"proposal_ids": [proposal_id]},
            headers={**_approver_auth(), "Idempotency-Key": "revision-002"},
        )
        assert repeated.status_code == 409


def test_failed_child_review_leaves_approved_correction_retryable() -> None:
    def review_handler(request):
        if request.parent_review_run_id is not None:
            return {"invalid": True}
        return build_agent_v3_result(request, include_finding=True)

    with _client(review_handler=review_handler) as client:
        case_id = client.post(
            "/api/v1/cases", json={"title": "Retryable correction"}, headers=_auth()
        ).json()["case_id"]
        document_id = client.post(
            f"/api/v1/cases/{case_id}/documents",
            json={"document_type": "CI", "filename": "invoice.txt", "content_text": "INV-001"},
            headers=_auth(),
        ).json()["record"]["document_id"]
        parent_run_id = client.post(
            f"/api/v1/cases/{case_id}/reviews",
            json={},
            headers={**_auth(), "Idempotency-Key": "retry-parent-001"},
        ).json()["job"]["review_run_id"]
        proposal_id = client.post(
            f"/api/v1/review-runs/{parent_run_id}/correction-proposals",
            json={
                "document_id": document_id,
                "field_name": "invoice_number",
                "new_value": "INV-001-CORRECTED",
                "reason": "The reviewer checked the amended value.",
            },
            headers=_auth(),
        ).json()["correction_proposal_id"]
        assert (
            client.put(
                f"/api/v1/correction-proposals/{proposal_id}/decision",
                json={"status": "approved", "reason": "A child review is required."},
                headers=_approver_auth(),
            ).status_code
            == 200
        )
        child = client.post(
            f"/api/v1/review-runs/{parent_run_id}/revisions",
            json={"proposal_ids": [proposal_id]},
            headers={**_approver_auth(), "Idempotency-Key": "retry-child-001"},
        )
        assert child.status_code == 202
        assert child.json()["job"]["status"] == "failed"
        proposal = client.get(
            f"/api/v1/review-runs/{parent_run_id}/correction-proposals", headers=_auth()
        ).json()["items"][0]
        assert proposal["status"] == "approved"
        assert "applied_review_run_id" not in proposal
        assert [item["event"] for item in proposal["history"]] == ["proposed", "approved"]


def test_concurrent_assistant_message_retry_invokes_agent_once() -> None:
    calls = 0
    calls_lock = threading.Lock()
    entered_agent = threading.Event()
    release_agent = threading.Event()

    def answer_handler(request):
        nonlocal calls
        with calls_lock:
            calls += 1
        entered_agent.set()
        assert release_agent.wait(timeout=3)
        return {
            "schema_version": "1.0",
            "case_id": request.case_id,
            "review_run_id": request.review_run_id,
            "conversation_id": request.conversation_id,
            "answer_scope": "clarification_needed",
            "direct_answer": "One bounded Agent answer.",
            "citations": [],
            "suggested_questions": [],
            "correction_draft": None,
            "engine": {"kind": "deterministic", "model_id": "concurrency-fixture"},
        }

    with _client(answer_handler=answer_handler) as client:
        case_id = client.post(
            "/api/v1/cases", json={"title": "Concurrent assistant"}, headers=_auth()
        ).json()["case_id"]
        client.post(
            f"/api/v1/cases/{case_id}/documents",
            json={"document_type": "CI", "filename": "invoice.txt", "content_text": "INV-001"},
            headers=_auth(),
        )
        run_id = client.post(
            f"/api/v1/cases/{case_id}/reviews",
            json={},
            headers={**_auth(), "Idempotency-Key": "concurrent-review-001"},
        ).json()["job"]["review_run_id"]
        conversation_id = client.post(
            f"/api/v1/review-runs/{run_id}/assistant/conversations",
            json={},
            headers=_auth(),
        ).json()["conversation_id"]
        service = client.app.state.container.service
        principal = Principal(
            subject="reviewer-1", org_id="local-dev", roles=frozenset({Role.reviewer})
        )
        message = ConversationMessageCreate(content="Please explain the review result.")
        with ThreadPoolExecutor(max_workers=2) as executor:
            first = executor.submit(
                service.send_conversation_message,
                principal,
                conversation_id,
                message,
                "concurrent-answer-001",
            )
            assert entered_agent.wait(timeout=3)
            second = executor.submit(
                service.send_conversation_message,
                principal,
                conversation_id,
                message,
                "concurrent-answer-001",
            )
            release_agent.set()
            first_exchange = first.result(timeout=3)
            second_exchange = second.result(timeout=3)

    assert calls == 1
    assert first_exchange == second_exchange


def test_agent_result_without_human_review_action_is_rejected_without_chat_context() -> None:
    def review_handler(request):
        result = build_agent_v3_result(request)
        result["required_actions"] = []
        result["result_sha256"] = canonical_digest(
            {key: value for key, value in result.items() if key != "result_sha256"}
        )
        return result

    with _client(review_handler=review_handler) as client:
        case_id = client.post(
            "/api/v1/cases", json={"title": "Missing human action"}, headers=_auth()
        ).json()["case_id"]
        client.post(
            f"/api/v1/cases/{case_id}/documents",
            json={"document_type": "CI", "filename": "invoice.txt", "content_text": "INV-001"},
            headers=_auth(),
        )
        review = client.post(
            f"/api/v1/cases/{case_id}/reviews",
            json={},
            headers={**_auth(), "Idempotency-Key": "missing-human-action-001"},
        )

    assert review.status_code == 202
    assert review.json()["job"]["status"] == "failed"
    assert review.json()["job"]["error_code"] == "ConflictError"
