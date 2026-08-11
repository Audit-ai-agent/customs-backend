from __future__ import annotations

import json
import threading
from concurrent.futures import ThreadPoolExecutor
from io import BytesIO
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient
from PIL import Image

from tests.product.agent_v3_fixture import build_agent_v3_result

from customs_backend.adapters import CallableAgentClient, HttpAgentClient, InMemoryObjectStore
from customs_backend.api import Container, _safe_case_transcript_response, create_app
from customs_backend.auth import StaticTokenVerifier
from customs_backend.case_input_boundary import CASE_DOCUMENT_CONTEXT_PLACEHOLDER
from customs_backend.domain import (
    AgentReviewRequest,
    ConversationMessageCreate,
    Principal,
    Role,
    canonical_digest,
)
from customs_backend.guidance_registry import guidance_registry_projection
from customs_backend.repository import InMemoryWorkbenchRepository
from customs_backend.scenario_transcript import (
    FunctionalTranscriptController,
    ScenarioTranscriptRoot,
    verify_run,
)
from customs_backend.service import WorkbenchService
from customs_backend.settings import Settings


def _client(
    *,
    include_finding: bool = False,
    review_handler=None,
    answer_handler=None,
    knowledge_answer_handler=None,
    extract_handler=None,
    settings: Settings | None = None,
) -> TestClient:
    settings = settings or Settings(_env_file=None, environment="test")
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
        knowledge_answer=knowledge_answer_handler,
        extract_document=extract_handler,
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


def _tiny_png() -> bytes:
    """A synthetic one-pixel fixture; it is not a user document."""

    output = BytesIO()
    Image.new("RGB", (1, 1), color=(0, 0, 0)).save(output, format="PNG")
    return output.getvalue()


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


def _v1_guidance(
    *,
    source_id: str,
    record_id: str,
    as_of: str,
    snapshot_id: str,
    coverage: str,
) -> dict[str, str]:
    """Build one live V1 citation from Backend's exact safe projection."""

    source = guidance_registry_projection("1.0").sources[source_id]
    return {
        "kind": "guidance_evidence",
        "source_id": record_id,
        "registry_source_id": source_id,
        "source_kind": source.source_kind,
        "source_title": source.title,
        "source_uri": source.official_uri,
        "as_of": as_of,
        "snapshot_id": snapshot_id,
        "coverage": coverage,
    }


def _v1_resolution(
    *,
    topic: str,
    requested_direction: str,
    as_of: str,
    snapshot_id: str,
    coverage: str,
) -> dict[str, object]:
    registry = guidance_registry_projection("1.0")
    scope = registry.scopes[(topic, "legacy_topic")]
    return {
        "topic": topic,
        "scope": "legacy_topic",
        "requested_direction": requested_direction,
        "applicable_directions": list(scope.directions),
        "coverage": coverage,
        "snapshot_id": snapshot_id,
        "as_of": as_of,
        "registry_version": "1.0",
    }


_V1_GUIDANCE_REGISTRY_SHA256 = "6fed779ee0ec1cd2358972925fe1b2788838a197d4f33e62e461fcfed2bb7d6c"


def _local_development_settings() -> Settings:
    return Settings(
        _env_file=None,
        environment="local-development",
        agent_service_token="a" * 32,
        dev_reviewer_session_token="reviewer-session-token",
        dev_approver_session_token="approver-session-token",
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
        assert detail.json()["temporal_context"] is None
        assert detail.json()["workflow"]["status"] == "completed"
        assert detail.json()["document_analyses"]


def test_binary_preview_stays_outside_text_review_until_human_confirmation() -> None:
    observed: list[tuple[object, bytes]] = []

    def extraction_handler(request, content: bytes) -> dict[str, object]:
        observed.append((request, content))
        return {
            "schema_version": "1.0",
            "document_asset_id": request.document_asset_id,
            "extraction_request_id": request.extraction_request_id,
            "status": "preview_ready",
            "media_type": request.media_type,
            "byte_size": request.byte_size,
            "source_sha256": request.source_sha256,
            "canonical_text_sha256": "a" * 64,
            "extractor_revision": "local-preview-test-r1",
            "pages": [
                {
                    "page_number": 1,
                    "width": 1,
                    "height": 1,
                    "coordinate_unit": "pixels",
                    "spans": [
                        {
                            "span_id": "span-preview-1",
                            "ordinal": 1,
                            "text": "candidate preview text",
                            "bbox": {"x": 0.0, "y": 0.0, "width": 1.0, "height": 1.0},
                            "reading_order": 1,
                        }
                    ],
                }
            ],
            "warnings": [{"code": "human_confirmation_required"}],
        }

    with _client(extract_handler=extraction_handler) as client:
        case = client.post("/api/v1/cases", json={"title": "Preview only"}, headers=_auth())
        assert case.status_code == 201
        case_id = case.json()["case_id"]
        payload = _tiny_png()

        uploaded = client.post(
            f"/api/v1/cases/{case_id}/document-assets",
            params={"document_type": "CI", "filename": "invoice.png"},
            content=payload,
            headers={**_auth(), "Content-Type": "image/png"},
        )
        assert uploaded.status_code == 201, uploaded.text
        asset = uploaded.json()
        assert asset["media_type"] == "image/png"
        assert "source_sha256" not in asset
        assert "object_key" not in asset

        # Uploading an asset must not alter the ordinary text-review inputs.
        documents = client.get(f"/api/v1/cases/{case_id}/documents", headers=_auth())
        assert documents.status_code == 200
        assert documents.json() == {"items": []}

        preview = client.post(
            f"/api/v1/cases/{case_id}/document-assets/{asset['asset_id']}/extractions",
            headers=_auth(),
        )
        assert preview.status_code == 202, preview.text
        job = preview.json()["job"]
        assert job["status"] == "preview_ready"
        assert job["result"]["pages"][0]["spans"][0]["text"] == "candidate preview text"
        assert "source_sha256" not in job["result"]
        assert "canonical_text_sha256" not in job["result"]
        assert len(observed) == 1
        assert observed[0][1] == payload

        confirmed = client.post(
            f"/api/v1/cases/{case_id}/document-assets/{asset['asset_id']}/confirmed-document",
            json={
                "extraction_job_id": job["extraction_job_id"],
                "filename": "invoice-confirmed.txt",
                "content_text": "human-confirmed review text",
            },
            headers=_auth(),
        )
        assert confirmed.status_code == 201, confirmed.text
        # The review document remains text-only; binary provenance stays on
        # the operational asset/extraction records rather than this public DTO.
        assert "source_asset_id" not in confirmed.json()["record"]
        documents = client.get(f"/api/v1/cases/{case_id}/documents", headers=_auth())
        assert len(documents.json()["items"]) == 1


def test_binary_asset_magic_mismatch_is_rejected_before_storage() -> None:
    with _client() as client:
        case = client.post("/api/v1/cases", json={"title": "Reject mismatch"}, headers=_auth())
        assert case.status_code == 201
        case_id = case.json()["case_id"]
        response = client.post(
            f"/api/v1/cases/{case_id}/document-assets",
            params={"document_type": "CI", "filename": "wrong.pdf"},
            content=_tiny_png(),
            headers={**_auth(), "Content-Type": "application/pdf"},
        )
        assert response.status_code == 422
        assert response.json()["code"] == "invalid_operation"
        assets = client.get(f"/api/v1/cases/{case_id}/document-assets", headers=_auth())
        assert assets.json() == {"items": []}


def test_temporal_context_is_persisted_and_forwarded_to_case_assistant() -> None:
    observed_review_contexts: list[dict[str, object]] = []

    def review_handler(request: AgentReviewRequest) -> dict[str, object]:
        result = build_agent_v3_result(request)
        context = {
            "law_as_of": request.review_as_of.isoformat(),
            "hsk_snapshot_as_of": "2026-01-01",
            "requirements_snapshot_as_of": "2026-07-26",
        }
        result["temporal_context"] = context
        result["result_sha256"] = canonical_digest(
            {key: value for key, value in result.items() if key != "result_sha256"}
        )
        return result

    def answer_handler(request):
        observed_review_contexts.append(request.review_result)
        return {
            "schema_version": "1.0",
            "case_id": request.case_id,
            "review_run_id": request.review_run_id,
            "conversation_id": request.conversation_id,
            "answer_scope": "clarification_needed",
            "direct_answer": "검토 결과의 시점 문맥을 확인했습니다.",
            "citations": [],
            "suggested_questions": [],
            "correction_draft": None,
            "engine": {"kind": "deterministic", "model_id": "temporal-fixture"},
        }

    expected_context = {
        "law_as_of": "2025-12-30",
        "hsk_snapshot_as_of": "2026-01-01",
        "requirements_snapshot_as_of": "2026-07-26",
    }
    with _client(review_handler=review_handler, answer_handler=answer_handler) as client:
        case_id = client.post(
            "/api/v1/cases", json={"title": "Temporal context"}, headers=_auth()
        ).json()["case_id"]
        client.post(
            f"/api/v1/cases/{case_id}/documents",
            json={"document_type": "CI", "filename": "invoice.txt", "content_text": "INV-001"},
            headers=_auth(),
        )
        review = client.post(
            f"/api/v1/cases/{case_id}/reviews",
            json={"review_as_of": "2025-12-30"},
            headers={**_auth(), "Idempotency-Key": "temporal-context-review-001"},
        )
        assert review.status_code == 202, review.text
        run_id = review.json()["job"]["review_run_id"]
        detail = client.get(f"/api/v1/review-runs/{run_id}", headers=_auth())
        assert detail.status_code == 200, detail.text
        assert detail.json()["temporal_context"] == expected_context

        conversation_id = client.post(
            f"/api/v1/review-runs/{run_id}/assistant/conversations", json={}, headers=_auth()
        ).json()["conversation_id"]
        answer = client.post(
            f"/api/v1/assistant/conversations/{conversation_id}/messages",
            json={"content": "이 심사의 시점 기준을 설명해 주세요."},
            headers={**_auth(), "Idempotency-Key": "temporal-context-answer-001"},
        )

    assert answer.status_code == 200, answer.text
    assert observed_review_contexts[0]["temporal_context"] == expected_context


def test_functional_uat_review_preserves_explicit_law_history_date_and_defaults_baseline() -> None:
    observed_dates: list[str] = []

    def review_handler(request: AgentReviewRequest) -> dict[str, object]:
        observed_dates.append(request.review_as_of.isoformat())
        return build_agent_v3_result(request)

    def enqueue(client: TestClient, *, title: str, body: dict[str, object], key: str) -> None:
        case = client.post("/api/v1/cases", json={"title": title}, headers=_auth())
        assert case.status_code == 201, case.text
        case_id = case.json()["case_id"]
        document = client.post(
            f"/api/v1/cases/{case_id}/documents",
            json={
                "document_type": "CI",
                "filename": "invoice.txt",
                "content_text": "INV-001",
            },
            headers=_auth(),
        )
        assert document.status_code == 201, document.text
        review = client.post(
            f"/api/v1/cases/{case_id}/reviews",
            json=body,
            headers={**_auth(), "Idempotency-Key": key},
        )
        assert review.status_code == 202, review.text

    with _client(settings=_functional_settings(), review_handler=review_handler) as client:
        enqueue(
            client,
            title="Functional law history date",
            body={"review_as_of": "2026-08-06"},
            key="functional-law-history-date-001",
        )
        enqueue(
            client,
            title="Functional default date",
            body={},
            key="functional-law-history-date-002",
        )

    assert observed_dates == ["2026-08-06", "2026-08-05"]


def test_null_temporal_context_remains_compatible_with_legacy_result_hash() -> None:
    observed_review_contexts: list[dict[str, object]] = []

    def review_handler(request: AgentReviewRequest) -> dict[str, object]:
        result = build_agent_v3_result(request)
        # The current Agent writes explicit null, but canonicalizes it to the
        # historical hash payload where this additive field is absent.
        result["temporal_context"] = None
        return result

    def answer_handler(request):
        observed_review_contexts.append(request.review_result)
        return {
            "schema_version": "1.0",
            "case_id": request.case_id,
            "review_run_id": request.review_run_id,
            "conversation_id": request.conversation_id,
            "answer_scope": "clarification_needed",
            "direct_answer": "기존 심사 결과입니다.",
            "citations": [],
            "suggested_questions": [],
            "correction_draft": None,
            "engine": {"kind": "deterministic", "model_id": "legacy-fixture"},
        }

    with _client(review_handler=review_handler, answer_handler=answer_handler) as client:
        case_id = client.post(
            "/api/v1/cases", json={"title": "Legacy temporal context"}, headers=_auth()
        ).json()["case_id"]
        client.post(
            f"/api/v1/cases/{case_id}/documents",
            json={"document_type": "CI", "filename": "invoice.txt", "content_text": "INV-001"},
            headers=_auth(),
        )
        review = client.post(
            f"/api/v1/cases/{case_id}/reviews",
            json={},
            headers={**_auth(), "Idempotency-Key": "legacy-temporal-review-001"},
        )
        run_id = review.json()["job"]["review_run_id"]
        assert client.get(f"/api/v1/review-runs/{run_id}", headers=_auth()).json()[
            "temporal_context"
        ] is None
        conversation_id = client.post(
            f"/api/v1/review-runs/{run_id}/assistant/conversations", json={}, headers=_auth()
        ).json()["conversation_id"]
        answer = client.post(
            f"/api/v1/assistant/conversations/{conversation_id}/messages",
            json={"content": "기존 결과를 설명해 주세요."},
            headers={**_auth(), "Idempotency-Key": "legacy-temporal-answer-001"},
        )

    assert answer.status_code == 200, answer.text
    assert observed_review_contexts[0]["temporal_context"] is None


def test_temporal_context_law_date_must_match_the_immutable_review_date() -> None:
    def review_handler(request: AgentReviewRequest) -> dict[str, object]:
        result = build_agent_v3_result(request)
        result["temporal_context"] = {
            "law_as_of": "2025-12-29",
            "hsk_snapshot_as_of": "2026-01-01",
            "requirements_snapshot_as_of": "2026-07-26",
        }
        result["result_sha256"] = canonical_digest(
            {key: value for key, value in result.items() if key != "result_sha256"}
        )
        return result

    with _client(review_handler=review_handler) as client:
        case_id = client.post(
            "/api/v1/cases", json={"title": "Temporal mismatch"}, headers=_auth()
        ).json()["case_id"]
        client.post(
            f"/api/v1/cases/{case_id}/documents",
            json={"document_type": "CI", "filename": "invoice.txt", "content_text": "INV-001"},
            headers=_auth(),
        )
        review = client.post(
            f"/api/v1/cases/{case_id}/reviews",
            json={"review_as_of": "2025-12-30"},
            headers={**_auth(), "Idempotency-Key": "temporal-context-mismatch-001"},
        )

    assert review.status_code == 202, review.text
    assert review.json()["job"]["status"] == "failed"
    assert review.json()["job"]["error_code"] == "ConflictError"


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


def test_public_openapi_excludes_internal_guidance_registry_provenance() -> None:
    with _client() as client:
        specification = client.get("/openapi.json").json()

    serialized = json.dumps(specification)
    assert "registry_source_id" not in serialized
    assert "guidance_registry_sha256" not in serialized
    schemas = specification["components"]["schemas"]
    resolution = schemas["GuidanceResolution"]["properties"]
    assert set(resolution) == {
        "topic",
        "scope",
        "requested_direction",
        "applicable_directions",
        "coverage",
        "snapshot_id",
        "as_of",
        "registry_version",
    }


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


def test_case_assistant_persists_additive_case_and_guidance_sections() -> None:
    def answer_handler(request):
        guidance = _v1_guidance(
            source_id="kcs_customs_guidance",
            record_id="guide-export-001",
            as_of="2026-08-05",
            snapshot_id="kr-trade-2026-08-05",
            coverage="covered",
        )
        return {
            "schema_version": "1.0",
            "case_id": request.case_id,
            "review_run_id": request.review_run_id,
            "conversation_id": request.conversation_id,
            "answer_scope": "case_grounded",
            "direct_answer": "현재 사건의 수량 차이와 관련 수출 절차를 함께 확인해야 합니다.",
            "citations": [
                {"kind": "comparison", "reference_id": "cmp_fixture_quantity"},
                guidance,
            ],
            "answer_sections": [
                {
                    "kind": "case_analysis",
                    "title": "현재 심사 결과",
                    "content": "상업송장과 포장명세서의 수량 차이를 먼저 확인해야 합니다.",
                    "citations": [{"kind": "comparison", "reference_id": "cmp_fixture_quantity"}],
                },
                {
                    "kind": "general_guidance",
                    "title": "일반 수출 안내",
                    "content": "수출 신고 전에는 문서 간 수량과 품목 정보를 함께 확인합니다.",
                    "citations": [guidance],
                },
            ],
            "suggested_questions": [],
            "correction_draft": None,
            "guidance_resolution": _v1_resolution(
                topic="customs_clearance",
                requested_direction="export",
                as_of="2026-08-05",
                snapshot_id="kr-trade-2026-08-05",
                coverage="covered",
            ),
            "guidance_registry_sha256": _V1_GUIDANCE_REGISTRY_SHA256,
            "engine": {"kind": "deterministic", "model_id": "section-fixture"},
        }

    with _client(include_finding=True, answer_handler=answer_handler) as client:
        case_id = client.post("/api/v1/cases", json={"title": "혼합 안내"}, headers=_auth()).json()[
            "case_id"
        ]
        client.post(
            f"/api/v1/cases/{case_id}/documents",
            json={"document_type": "CI", "filename": "invoice.txt", "content_text": "INV-001"},
            headers=_auth(),
        )
        run_id = client.post(
            f"/api/v1/cases/{case_id}/reviews",
            json={"trade_type": "export", "review_as_of": "2026-08-05"},
            headers={**_auth(), "Idempotency-Key": "section-review-001"},
        ).json()["job"]["review_run_id"]
        conversation_id = client.post(
            f"/api/v1/review-runs/{run_id}/assistant/conversations",
            json={},
            headers=_auth(),
        ).json()["conversation_id"]
        response = client.post(
            f"/api/v1/assistant/conversations/{conversation_id}/messages",
            json={"content": "현재 사건과 일반 수출 절차를 함께 알려 주세요."},
            headers={**_auth(), "Idempotency-Key": "section-answer-001"},
        )

    assert response.status_code == 200, response.text
    assistant = response.json()["assistant_message"]
    assert assistant["content"] == "현재 사건의 수량 차이와 관련 수출 절차를 함께 확인해야 합니다."
    assert [section["kind"] for section in assistant["answer_sections"]] == [
        "case_analysis",
        "general_guidance",
    ]
    assert assistant["answer_sections"][1]["citations"][0]["source_id"] == "guide-export-001"
    assert assistant["answer_sections"][1]["citations"][0]["coverage"] == "covered"


def test_case_assistant_rejects_guidance_from_another_review_date() -> None:
    def answer_handler(request):
        return {
            "schema_version": "1.0",
            "case_id": request.case_id,
            "review_run_id": request.review_run_id,
            "conversation_id": request.conversation_id,
            "answer_scope": "case_grounded",
            "direct_answer": "현재 사건의 수량 차이를 먼저 확인해야 합니다.",
            "citations": [
                {"kind": "comparison", "reference_id": "cmp_fixture_quantity"},
                _v1_guidance(
                    source_id="kcs_customs_guidance",
                    record_id="guide-wrong-date",
                    as_of="2026-08-06",
                    snapshot_id="kr-trade-2026-08-06",
                    coverage="covered",
                ),
            ],
            "suggested_questions": [],
            "correction_draft": None,
            "guidance_resolution": _v1_resolution(
                topic="customs_clearance",
                requested_direction="export",
                as_of="2026-08-06",
                snapshot_id="kr-trade-2026-08-06",
                coverage="covered",
            ),
            "guidance_registry_sha256": _V1_GUIDANCE_REGISTRY_SHA256,
            "engine": {"kind": "deterministic", "model_id": "date-fixture"},
        }

    with _client(include_finding=True, answer_handler=answer_handler) as client:
        case_id = client.post(
            "/api/v1/cases", json={"title": "기준일 일치"}, headers=_auth()
        ).json()["case_id"]
        client.post(
            f"/api/v1/cases/{case_id}/documents",
            json={"document_type": "CI", "filename": "invoice.txt", "content_text": "INV-001"},
            headers=_auth(),
        )
        run_id = client.post(
            f"/api/v1/cases/{case_id}/reviews",
            json={"review_as_of": "2026-08-05"},
            headers={**_auth(), "Idempotency-Key": "guidance-date-review-001"},
        ).json()["job"]["review_run_id"]
        conversation_id = client.post(
            f"/api/v1/review-runs/{run_id}/assistant/conversations",
            json={},
            headers=_auth(),
        ).json()["conversation_id"]
        response = client.post(
            f"/api/v1/assistant/conversations/{conversation_id}/messages",
            json={"content": "현재 사건의 수량을 설명해 주세요."},
            headers={**_auth(), "Idempotency-Key": "guidance-date-answer-001"},
        )

    assert response.status_code == 409


def test_official_knowledge_conversation_is_separate_and_snapshot_grounded() -> None:
    observed: dict[str, object] = {}

    def knowledge_handler(request):
        observed.update(request.to_agent_wire())
        guidance = _v1_guidance(
            source_id="kcs_customs_guidance",
            record_id="official-export-guide-2026",
            as_of="2026-08-05",
            snapshot_id="kr-trade-2026-08-05",
            coverage="covered",
        )
        return {
            "schema_version": "1.0",
            "conversation_id": request.conversation_id,
            "answer_scope": "general_customs_guidance",
            "direct_answer": "수출 신고 전에는 품목, 수량, 거래 조건과 필요한 증빙을 함께 확인해야 합니다.",
            "citations": [guidance],
            "answer_sections": [
                {
                    "kind": "general_guidance",
                    "title": "공식 수출 통관 안내",
                    "content": "수출 신고 전에는 품목, 수량, 거래 조건과 필요한 증빙을 함께 확인해야 합니다.",
                    "citations": [guidance],
                }
            ],
            "suggested_questions": ["수출 품목의 HS 코드를 함께 확인할까요?"],
            "coverage": "covered",
            "snapshot_id": "kr-trade-2026-08-05",
            "as_of": "2026-08-05",
            "guidance_resolution": _v1_resolution(
                topic="customs_clearance",
                requested_direction="export",
                as_of="2026-08-05",
                snapshot_id="kr-trade-2026-08-05",
                coverage="covered",
            ),
            "guidance_registry_sha256": _V1_GUIDANCE_REGISTRY_SHA256,
            "engine": {"kind": "deterministic", "model_id": "knowledge-fixture"},
        }

    with _client(knowledge_answer_handler=knowledge_handler) as client:
        created = client.post(
            "/api/v1/knowledge/conversations",
            json={"title": "수출 기본 절차", "trade_type": "export", "as_of": "2026-08-05"},
            headers=_auth(),
        )
        assert created.status_code == 201, created.text
        conversation = created.json()
        assert set(conversation) == {
            "conversation_id",
            "title",
            "trade_type",
            "as_of",
            "created_by",
            "created_at",
            "updated_at",
        }
        assert conversation["trade_type"] == "export"
        assert "review_run_id" not in conversation
        conversation_id = conversation["conversation_id"]

        answer = client.post(
            f"/api/v1/knowledge/conversations/{conversation_id}/messages",
            json={"content": "수출 신고 전에 무엇을 확인해야 하나요?"},
            headers={**_auth(), "Idempotency-Key": "knowledge-answer-001"},
        )
        assert answer.status_code == 200, answer.text
        exchange = answer.json()
        assistant = exchange["assistant_message"]
        assert assistant["answer_scope"] == "general_customs_guidance"
        assert assistant["citations"][0]["kind"] == "guidance_evidence"
        assert assistant["citations"][0]["source_id"] == "official-export-guide-2026"
        assert assistant["citations"][0]["source_kind"] == "official_agency_guidance"
        assert assistant["answer_sections"][0]["kind"] == "general_guidance"
        assert assistant["knowledge_coverage"] == "covered"
        assert assistant["knowledge_snapshot_id"] == "kr-trade-2026-08-05"
        assert assistant["knowledge_as_of"] == "2026-08-05"

        replay = client.post(
            f"/api/v1/knowledge/conversations/{conversation_id}/messages",
            json={"content": "수출 신고 전에 무엇을 확인해야 하나요?"},
            headers={**_auth(), "Idempotency-Key": "knowledge-answer-001"},
        )
        assert replay.status_code == 200
        assert replay.json() == exchange
        conflict = client.post(
            f"/api/v1/knowledge/conversations/{conversation_id}/messages",
            json={"content": "다른 질문입니다."},
            headers={**_auth(), "Idempotency-Key": "knowledge-answer-001"},
        )
        assert conflict.status_code == 409
        messages = client.get(
            f"/api/v1/knowledge/conversations/{conversation_id}/messages",
            headers=_auth(),
        )
        assert messages.status_code == 200
        assert [item["role"] for item in messages.json()["items"]] == ["user", "assistant"]
        listed = client.get("/api/v1/knowledge/conversations", headers=_auth())
        assert listed.status_code == 200
        assert [item["conversation_id"] for item in listed.json()["items"]] == [conversation_id]
        assert listed.json()["items"][0]["snapshot_id"] == "kr-trade-2026-08-05"

    assert observed["trade_type"] == "export"
    assert observed["as_of"] == "2026-08-05"
    assert "case_id" not in observed
    assert "review_run_id" not in observed


def test_knowledge_conversation_resolves_an_omitted_public_as_of_before_agent() -> None:
    observed: dict[str, object] = {}

    def knowledge_handler(request):
        observed.update(request.to_agent_wire())
        return {
            "schema_version": "1.0",
            "conversation_id": request.conversation_id,
            "answer_scope": "clarification_needed",
            "direct_answer": "품목과 수출입 방향을 알려 주세요.",
            "citations": [],
            "suggested_questions": [],
            "coverage": "unavailable",
            "snapshot_id": None,
            "as_of": request.as_of.isoformat(),
            "engine": {"kind": "deterministic", "model_id": "knowledge-fixture"},
        }

    with _client(knowledge_answer_handler=knowledge_handler) as client:
        created = client.post(
            "/api/v1/knowledge/conversations",
            json={"title": "기본 안내"},
            headers=_auth(),
        )
        assert created.status_code == 201, created.text
        conversation = created.json()
        assert conversation["as_of"] == "2026-08-05"
        answer = client.post(
            f"/api/v1/knowledge/conversations/{conversation['conversation_id']}/messages",
            json={"content": "통관 절차를 알려 주세요."},
            headers={**_auth(), "Idempotency-Key": "knowledge-default-date-001"},
        )

    assert answer.status_code == 200, answer.text
    assert observed["as_of"] == "2026-08-05"
    assert answer.json()["assistant_message"]["answer_sections"] == []
    assert answer.json()["assistant_message"]["knowledge_coverage"] == "unavailable"
    assert answer.json()["assistant_message"]["knowledge_as_of"] == "2026-08-05"
    assert "knowledge_snapshot_id" not in answer.json()["assistant_message"]


def test_knowledge_answer_rejects_a_different_resolved_snapshot_date() -> None:
    def knowledge_handler(request):
        return {
            "schema_version": "1.0",
            "conversation_id": request.conversation_id,
            "answer_scope": "clarification_needed",
            "direct_answer": "품목을 알려 주세요.",
            "citations": [],
            "suggested_questions": [],
            "coverage": "unavailable",
            "snapshot_id": None,
            "as_of": "2026-08-06",
            "engine": {"kind": "deterministic", "model_id": "knowledge-fixture"},
        }

    with _client(knowledge_answer_handler=knowledge_handler) as client:
        conversation_id = client.post(
            "/api/v1/knowledge/conversations",
            json={"as_of": "2026-08-05"},
            headers=_auth(),
        ).json()["conversation_id"]
        response = client.post(
            f"/api/v1/knowledge/conversations/{conversation_id}/messages",
            json={"content": "통관 절차를 알려 주세요."},
            headers={**_auth(), "Idempotency-Key": "knowledge-date-mismatch-001"},
        )

    assert response.status_code == 409


def test_knowledge_answer_rejects_section_citation_metadata_that_differs_from_canonical() -> None:
    def knowledge_handler(request):
        canonical = {
            "kind": "guidance_evidence",
            "source_id": "official-guide-001",
            "source_kind": "official_law",
            "source_title": "관세청 수출 통관 안내",
            "source_uri": "https://www.customs.go.kr/guide/export",
            "as_of": request.as_of.isoformat(),
            "snapshot_id": "kr-trade-2026-08-05",
            "coverage": "covered",
        }
        changed = {
            **canonical,
            "source_uri": "https://untrusted.example/altered",
            "snapshot_id": "untrusted-snapshot",
        }
        return {
            "schema_version": "1.0",
            "conversation_id": request.conversation_id,
            "answer_scope": "general_customs_guidance",
            "direct_answer": "공식 안내를 확인해야 합니다.",
            "citations": [canonical],
            "answer_sections": [
                {
                    "kind": "general_guidance",
                    "title": "공식 안내",
                    "content": "공식 안내를 확인해야 합니다.",
                    "citations": [changed],
                }
            ],
            "suggested_questions": [],
            "coverage": "covered",
            "snapshot_id": "kr-trade-2026-08-05",
            "as_of": request.as_of.isoformat(),
            "engine": {"kind": "deterministic", "model_id": "knowledge-fixture"},
        }

    with _client(knowledge_answer_handler=knowledge_handler) as client:
        conversation_id = client.post(
            "/api/v1/knowledge/conversations", json={}, headers=_auth()
        ).json()["conversation_id"]
        response = client.post(
            f"/api/v1/knowledge/conversations/{conversation_id}/messages",
            json={"content": "수출 신고 전에 무엇을 확인해야 하나요?"},
            headers={**_auth(), "Idempotency-Key": "canonical-citation-001"},
        )

    assert response.status_code == 409


def test_knowledge_answer_rejects_an_uncited_display_section() -> None:
    def knowledge_handler(request):
        guidance = {
            "kind": "guidance_evidence",
            "source_id": "official-guide-001",
            "source_kind": "official_law",
            "source_title": "관세청 수출 통관 안내",
            "source_uri": "https://www.customs.go.kr/guide/export",
            "as_of": request.as_of.isoformat(),
            "snapshot_id": "kr-trade-2026-08-05",
            "coverage": "covered",
        }
        return {
            "schema_version": "1.0",
            "conversation_id": request.conversation_id,
            "answer_scope": "general_customs_guidance",
            "direct_answer": "공식 안내를 확인해야 합니다.",
            "citations": [guidance],
            "answer_sections": [
                {
                    "kind": "general_guidance",
                    "title": "공식 안내",
                    "content": "공식 안내를 확인해야 합니다.",
                    "citations": [],
                }
            ],
            "suggested_questions": [],
            "coverage": "covered",
            "snapshot_id": "kr-trade-2026-08-05",
            "as_of": request.as_of.isoformat(),
            "engine": {"kind": "deterministic", "model_id": "knowledge-fixture"},
        }

    with _client(knowledge_answer_handler=knowledge_handler) as client:
        conversation_id = client.post(
            "/api/v1/knowledge/conversations", json={}, headers=_auth()
        ).json()["conversation_id"]
        response = client.post(
            f"/api/v1/knowledge/conversations/{conversation_id}/messages",
            json={"content": "수출 신고 전에 무엇을 확인해야 하나요?"},
            headers={**_auth(), "Idempotency-Key": "-".join(("uncited", "section", "001"))},
        )

    assert response.status_code == 409


def test_knowledge_conversation_rejects_snapshot_drift_and_preserves_answer_provenance() -> None:
    answer_count = 0

    def knowledge_handler(request):
        nonlocal answer_count
        answer_count += 1
        snapshot_id = "kr-trade-2026-08-05-a" if answer_count == 1 else "kr-trade-2026-08-05-b"
        guidance = _v1_guidance(
            source_id="kcs_customs_guidance",
            record_id="official-guide-001",
            as_of=request.as_of.isoformat(),
            snapshot_id=snapshot_id,
            coverage="partial",
        )
        return {
            "schema_version": "1.0",
            "conversation_id": request.conversation_id,
            "answer_scope": "general_customs_guidance",
            "direct_answer": "공식 안내를 확인해야 합니다.",
            "citations": [guidance],
            "answer_sections": [],
            "suggested_questions": [],
            "coverage": "partial",
            "snapshot_id": snapshot_id,
            "as_of": request.as_of.isoformat(),
            "guidance_resolution": _v1_resolution(
                topic="customs_clearance",
                requested_direction="import",
                as_of=request.as_of.isoformat(),
                snapshot_id=snapshot_id,
                coverage="partial",
            ),
            "guidance_registry_sha256": _V1_GUIDANCE_REGISTRY_SHA256,
            "engine": {"kind": "deterministic", "model_id": "knowledge-fixture"},
        }

    with _client(knowledge_answer_handler=knowledge_handler) as client:
        conversation_id = client.post(
            "/api/v1/knowledge/conversations", json={}, headers=_auth()
        ).json()["conversation_id"]
        first = client.post(
            f"/api/v1/knowledge/conversations/{conversation_id}/messages",
            json={"content": "첫 질문"},
            headers={**_auth(), "Idempotency-Key": "snapshot-drift-001"},
        )
        second = client.post(
            f"/api/v1/knowledge/conversations/{conversation_id}/messages",
            json={"content": "둘째 질문"},
            headers={**_auth(), "Idempotency-Key": "snapshot-drift-002"},
        )
        history = client.get(
            f"/api/v1/knowledge/conversations/{conversation_id}/messages", headers=_auth()
        ).json()["items"]
        conversation = client.get("/api/v1/knowledge/conversations", headers=_auth()).json()[
            "items"
        ][0]

    assert first.status_code == 200, first.text
    assert first.json()["assistant_message"]["knowledge_coverage"] == "partial"
    assert first.json()["assistant_message"]["knowledge_snapshot_id"] == "kr-trade-2026-08-05-a"
    assert first.json()["assistant_message"]["knowledge_as_of"] == "2026-08-05"
    assert second.status_code == 409
    assert [item["role"] for item in history] == ["user", "assistant"]
    assert history[-1]["knowledge_snapshot_id"] == "kr-trade-2026-08-05-a"
    assert conversation["snapshot_id"] == "kr-trade-2026-08-05-a"


@pytest.mark.parametrize(
    ("registry_source_id", "topic", "requested_direction"),
    (
        ("law_001556", "customs_clearance", "import"),
        ("law_kr_trade_fta_scope", "fta_procedure", "import"),
        ("kcs_customs_guidance", "customs_clearance", "import"),
        ("ccct_public_requirements", "import_requirements", "import"),
        ("kotra_trade_guidance", "trade_terms", "export"),
    ),
    ids=(
        "official_law",
        "official_law_catalog",
        "official_agency_guidance",
        "official_requirement_reference",
        "official_public_guidance",
    ),
)
def test_functional_uat_accepts_every_official_guidance_registry_source_kind(
    registry_source_id: str,
    topic: str,
    requested_direction: str,
) -> None:
    """Official registry categories remain distinct but all work in Functional-UAT."""

    def knowledge_handler(request):
        guidance = _v1_guidance(
            source_id=registry_source_id,
            record_id=f"guide-{registry_source_id}",
            as_of=request.as_of.isoformat(),
            snapshot_id="kr-trade-2026-08-05",
            coverage="covered",
        )
        return {
            "schema_version": "1.0",
            "conversation_id": request.conversation_id,
            "answer_scope": "general_customs_guidance",
            "direct_answer": "공식 자료 기준으로 확인할 수 있습니다.",
            "citations": [guidance],
            "answer_sections": [
                {
                    "kind": "general_guidance",
                    "title": "공식 통관 안내",
                    "content": "공식 자료 기준으로 확인할 수 있습니다.",
                    "citations": [guidance],
                }
            ],
            "suggested_questions": [],
            "coverage": "covered",
            "snapshot_id": "kr-trade-2026-08-05",
            "as_of": request.as_of.isoformat(),
            "guidance_resolution": _v1_resolution(
                topic=topic,
                requested_direction=requested_direction,
                as_of=request.as_of.isoformat(),
                snapshot_id="kr-trade-2026-08-05",
                coverage="covered",
            ),
            "guidance_registry_sha256": _V1_GUIDANCE_REGISTRY_SHA256,
            "engine": {"kind": "deterministic", "model_id": "knowledge-fixture"},
        }

    with _client(
        knowledge_answer_handler=knowledge_handler, settings=_functional_settings()
    ) as client:
        conversation_id = client.post(
            "/api/v1/knowledge/conversations", json={}, headers=_auth()
        ).json()["conversation_id"]
        response = client.post(
            f"/api/v1/knowledge/conversations/{conversation_id}/messages",
            json={"content": "공식 통관 안내를 알려 주세요."},
            headers={
                "Authorization": "Bearer test-token",
                "Idempotency-Key": f"official-{registry_source_id}",
            },
        )

    assert response.status_code == 200, response.text
    citation = response.json()["assistant_message"]["citations"][0]
    assert citation["source_kind"] == guidance_registry_projection("1.0").sources[
        registry_source_id
    ].source_kind
    assert citation["snapshot_id"] == "kr-trade-2026-08-05"


def test_local_development_allows_its_deterministic_guidance_fixture() -> None:
    """The fixture remains useful for local development but cannot cross into UAT."""

    def knowledge_handler(request):
        guidance = {
            "kind": "guidance_evidence",
            "source_id": "development-fixture-guide",
            "source_kind": "development_fixture",
            "source_title": "개발용 통관 안내",
            "source_uri": "https://fixture.invalid/guide",
            "as_of": request.as_of.isoformat(),
            "snapshot_id": "dev-fixture-2026-08-05",
            "coverage": "covered",
        }
        return {
            "schema_version": "1.0",
            "conversation_id": request.conversation_id,
            "answer_scope": "general_customs_guidance",
            "direct_answer": "개발용 공식 지식 fixture에서 확인했습니다.",
            "citations": [guidance],
            "answer_sections": [],
            "suggested_questions": [],
            "coverage": "covered",
            "snapshot_id": "dev-fixture-2026-08-05",
            "as_of": request.as_of.isoformat(),
            "engine": {"kind": "deterministic", "model_id": "knowledge-fixture"},
        }

    with _client(
        knowledge_answer_handler=knowledge_handler, settings=_local_development_settings()
    ) as client:
        conversation_id = client.post(
            "/api/v1/knowledge/conversations", json={}, headers=_auth()
        ).json()["conversation_id"]
        response = client.post(
            f"/api/v1/knowledge/conversations/{conversation_id}/messages",
            json={"content": "개발용 통관 안내를 알려 주세요."},
            headers={**_auth(), "Idempotency-Key": "development-fixture-source-001"},
        )

    assert response.status_code == 200, response.text
    assert (
        response.json()["assistant_message"]["citations"][0]["source_kind"] == "development_fixture"
    )


def test_functional_uat_rejects_nonofficial_or_uncovered_global_guidance() -> None:
    def knowledge_handler(request):
        fixture_source = request.question.startswith("fixture")
        guidance = {
            "kind": "guidance_evidence",
            "source_id": "trade-guide-001",
            "source_kind": "development_fixture" if fixture_source else "official_law",
            "source_title": "통관 안내",
            "source_uri": "https://example.invalid/guide",
            "as_of": request.as_of.isoformat(),
            "snapshot_id": "fixture-snapshot",
            "coverage": "covered" if fixture_source else "no_coverage",
        }
        return {
            "schema_version": "1.0",
            "conversation_id": request.conversation_id,
            "answer_scope": "general_customs_guidance",
            "direct_answer": "통관 안내입니다.",
            "citations": [guidance],
            "answer_sections": [],
            "suggested_questions": [],
            "coverage": guidance["coverage"],
            "snapshot_id": "fixture-snapshot",
            "as_of": request.as_of.isoformat(),
            "engine": {"kind": "deterministic", "model_id": "knowledge-fixture"},
        }

    with _client(
        knowledge_answer_handler=knowledge_handler, settings=_functional_settings()
    ) as client:
        conversation_id = client.post(
            "/api/v1/knowledge/conversations", json={}, headers=_auth()
        ).json()["conversation_id"]
        fixture = client.post(
            f"/api/v1/knowledge/conversations/{conversation_id}/messages",
            json={"content": "fixture source"},
            headers={**_auth(), "Idempotency-Key": "functional-source-001"},
        )
        uncovered = client.post(
            f"/api/v1/knowledge/conversations/{conversation_id}/messages",
            json={"content": "uncovered source"},
            headers={**_auth(), "Idempotency-Key": "functional-source-002"},
        )

    assert fixture.status_code == 409
    assert uncovered.status_code == 409


def test_functional_uat_rejects_nonofficial_guidance_in_case_assistant() -> None:
    def answer_handler(request):
        guidance = {
            "kind": "guidance_evidence",
            "source_id": "fixture-guide-001",
            "source_kind": "development_fixture",
            "source_title": "개발용 안내",
            "source_uri": "https://fixture.invalid/guide",
            "as_of": "2026-08-05",
            "snapshot_id": "fixture-snapshot",
            "coverage": "covered",
        }
        return {
            "schema_version": "1.0",
            "case_id": request.case_id,
            "review_run_id": request.review_run_id,
            "conversation_id": request.conversation_id,
            "answer_scope": "general_customs_guidance",
            "direct_answer": "일반 안내입니다.",
            "citations": [guidance],
            "answer_sections": [],
            "suggested_questions": [],
            "correction_draft": None,
            "engine": {"kind": "deterministic", "model_id": "case-fixture"},
        }

    with _client(answer_handler=answer_handler, settings=_functional_settings()) as client:
        case_id = client.post(
            "/api/v1/cases", json={"title": "기능 UAT 사건"}, headers=_auth()
        ).json()["case_id"]
        document = client.post(
            f"/api/v1/cases/{case_id}/documents",
            json={"document_type": "CI", "filename": "invoice.txt", "content_text": "INV-001"},
            headers=_auth(),
        )
        assert document.status_code == 201
        review_run_id = client.post(
            f"/api/v1/cases/{case_id}/reviews",
            json={"review_as_of": "2026-08-05"},
            headers={**_auth(), "Idempotency-Key": "functional-case-review-001"},
        ).json()["job"]["review_run_id"]
        conversation_id = client.post(
            f"/api/v1/review-runs/{review_run_id}/assistant/conversations",
            json={},
            headers=_auth(),
        ).json()["conversation_id"]
        response = client.post(
            f"/api/v1/assistant/conversations/{conversation_id}/messages",
            json={"content": "일반 수출 절차를 알려 주세요."},
            headers={**_auth(), "Idempotency-Key": "functional-case-answer-001"},
        )

    assert response.status_code == 409


def test_new_text_document_types_and_export_review_remain_within_existing_case_boundary() -> None:
    with _client() as client:
        case_id = client.post(
            "/api/v1/cases", json={"title": "수출 문서 기반"}, headers=_auth()
        ).json()["case_id"]
        for document_type, filename in (
            ("AWB", "air-waybill.txt"),
            ("EXPORT_DECLARATION", "export-declaration.txt"),
            ("SUPPORTING_EVIDENCE", "supporting-evidence.txt"),
        ):
            document = client.post(
                f"/api/v1/cases/{case_id}/documents",
                json={
                    "document_type": document_type,
                    "filename": filename,
                    "content_text": f"Synthetic {document_type} text.",
                },
                headers=_auth(),
            )
            assert document.status_code == 201, document.text
            assert document.json()["record"]["document_type"] == document_type
        review = client.post(
            f"/api/v1/cases/{case_id}/reviews",
            json={"trade_type": "export"},
            headers={**_auth(), "Idempotency-Key": "export-document-types-001"},
        )
        assert review.status_code == 202, review.text
        assert review.json()["job"]["status"] == "succeeded"
        review_run_id = review.json()["job"]["review_run_id"]
        run = client.get(f"/api/v1/review-runs/{review_run_id}", headers=_auth())

    assert run.status_code == 200, run.text
    assert {
        (item["declared_type"], item["document_type"]) for item in run.json()["document_analyses"]
    } == {
        ("air_waybill", "air_waybill"),
        ("export_declaration", "export_declaration"),
        ("supporting_evidence", "supporting_evidence"),
    }


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


def test_case_assistant_persists_v1_guidance_application_without_internal_source_id() -> None:
    def answer_handler(request):
        guidance = {
            "kind": "guidance_evidence",
            "source_id": "record-trade-terms-001",
            "registry_source_id": "kotra_trade_guidance",
            "source_kind": "official_public_guidance",
            "source_title": "KOTRA 공개 무역 실무 안내",
            "source_uri": "https://dream.kotra.or.kr/dream/cms/com/index.do?MENU_ID=4070",
            "as_of": "2026-08-05",
            "snapshot_id": "kr-trade-2026-08-05",
            "coverage": "covered",
        }
        return {
            "schema_version": "1.0",
            "case_id": request.case_id,
            "review_run_id": request.review_run_id,
            "conversation_id": request.conversation_id,
            "answer_scope": "case_grounded",
            "direct_answer": "현재 사건의 수량 비교와 거래조건 확인을 함께 진행해야 합니다.",
            "citations": [
                {"kind": "comparison", "reference_id": "cmp_fixture_quantity"},
                guidance,
            ],
            "answer_sections": [
                {
                    "kind": "case_analysis",
                    "title": "현재 사건 기준",
                    "content": "문서 간 수량 차이를 먼저 확인해야 합니다.",
                    "citations": [
                        {"kind": "comparison", "reference_id": "cmp_fixture_quantity"}
                    ],
                },
                {
                    "kind": "general_guidance",
                    "title": "일반 통관 안내",
                    "content": "공개 무역 실무 안내의 거래조건 범위를 확인할 수 있습니다.",
                    "citations": [guidance],
                },
                {
                    "kind": "case_application",
                    "title": "사건 적용·다음 확인",
                    "content": "현재 수량 차이 확인과 거래조건 문서 확인을 분리해 검토해 주세요.",
                    "citations": [
                        {"kind": "comparison", "reference_id": "cmp_fixture_quantity"},
                        guidance,
                    ],
                },
            ],
            "suggested_questions": [],
            "correction_draft": None,
            "guidance_resolution": {
                "topic": "trade_terms",
                "scope": "legacy_topic",
                "requested_direction": "export",
                "applicable_directions": ["import", "export"],
                "coverage": "covered",
                "snapshot_id": "kr-trade-2026-08-05",
                "as_of": "2026-08-05",
                "registry_version": "1.0",
            },
            "guidance_registry_sha256": (
                "6fed779ee0ec1cd2358972925fe1b2788838a197d4f33e62e461fcfed2bb7d6c"
            ),
            "engine": {"kind": "deterministic", "model_id": "v1-application-fixture"},
        }

    with _client(include_finding=True, answer_handler=answer_handler) as client:
        case_id = client.post("/api/v1/cases", json={"title": "적용 요약"}, headers=_auth()).json()[
            "case_id"
        ]
        client.post(
            f"/api/v1/cases/{case_id}/documents",
            json={"document_type": "CI", "filename": "invoice.txt", "content_text": "INV-001"},
            headers=_auth(),
        )
        run_id = client.post(
            f"/api/v1/cases/{case_id}/reviews",
            json={"trade_type": "export", "review_as_of": "2026-08-05"},
            headers={**_auth(), "Idempotency-Key": "application-v1-review-001"},
        ).json()["job"]["review_run_id"]
        conversation_id = client.post(
            f"/api/v1/review-runs/{run_id}/assistant/conversations", json={}, headers=_auth()
        ).json()["conversation_id"]
        response = client.post(
            f"/api/v1/assistant/conversations/{conversation_id}/messages",
            json={"content": "현재 사건에 거래조건 안내를 적용하면 무엇을 확인해야 하나요?"},
            headers={**_auth(), "Idempotency-Key": "application-v1-answer-001"},
        )

    assert response.status_code == 200, response.text
    assistant = response.json()["assistant_message"]
    assert [section["kind"] for section in assistant["answer_sections"]] == [
        "case_analysis",
        "general_guidance",
        "case_application",
    ]
    assert assistant["guidance_resolution"] == {
        "topic": "trade_terms",
        "scope": "legacy_topic",
        "requested_direction": "export",
        "applicable_directions": ["import", "export"],
        "coverage": "covered",
        "snapshot_id": "kr-trade-2026-08-05",
        "as_of": "2026-08-05",
        "registry_version": "1.0",
    }
    assert "registry_source_id" not in json.dumps(assistant)
    assert "guidance_registry_sha256" not in json.dumps(assistant)


def test_case_document_shaped_chat_is_persisted_but_never_forwarded_to_agent() -> None:
    observed: list[object] = []

    def answer_handler(request):
        observed.append(request)
        return {
            "schema_version": "1.0",
            "case_id": request.case_id,
            "review_run_id": request.review_run_id,
            "conversation_id": request.conversation_id,
            "answer_scope": "case_grounded",
            "direct_answer": "현재 사건의 수량 비교를 확인해 주세요.",
            "citations": [{"kind": "comparison", "reference_id": "cmp_fixture_quantity"}],
            "suggested_questions": [],
            "correction_draft": None,
            "engine": {"kind": "deterministic", "model_id": "case-input-boundary"},
        }

    pasted_document = "상업송장\n수량: 10\n금액: 100"
    with _client(include_finding=True, answer_handler=answer_handler) as client:
        case_id = client.post("/api/v1/cases", json={"title": "문서형 입력"}, headers=_auth()).json()[
            "case_id"
        ]
        client.post(
            f"/api/v1/cases/{case_id}/documents",
            json={"document_type": "CI", "filename": "invoice.txt", "content_text": "INV-001"},
            headers=_auth(),
        )
        run_id = client.post(
            f"/api/v1/cases/{case_id}/reviews",
            json={"review_as_of": "2026-08-05"},
            headers={**_auth(), "Idempotency-Key": "document-input-review-001"},
        ).json()["job"]["review_run_id"]
        conversation_id = client.post(
            f"/api/v1/review-runs/{run_id}/assistant/conversations", json={}, headers=_auth()
        ).json()["conversation_id"]
        first = client.post(
            f"/api/v1/assistant/conversations/{conversation_id}/messages",
            json={"content": pasted_document},
            headers={**_auth(), "Idempotency-Key": "document-input-answer-001"},
        )
        second = client.post(
            f"/api/v1/assistant/conversations/{conversation_id}/messages",
            json={"content": "현재 사건의 수량 차이를 설명해 주세요."},
            headers={**_auth(), "Idempotency-Key": "document-input-answer-002"},
        )

    assert first.status_code == 200, first.text
    first_exchange = first.json()
    assert observed and len(observed) == 1
    assert first_exchange["user_message"]["content"] == pasted_document
    assert first_exchange["assistant_message"]["case_input_notice"] == "unregistered_document_text"
    assert first_exchange["assistant_message"]["answer_scope"] == "insufficient_evidence"
    assert first_exchange["assistant_message"]["citations"] == []
    assert first_exchange["assistant_message"]["answer_sections"] == []
    assert "새 근거로 사용하지 않았습니다" in first_exchange["assistant_message"]["content"]
    assert second.status_code == 200, second.text
    recent_turns = observed[0].recent_turns
    assert pasted_document not in json.dumps([turn.model_dump() for turn in recent_turns])
    assert any(turn.content == CASE_DOCUMENT_CONTEXT_PLACEHOLDER for turn in recent_turns)
    transcript_value = _safe_case_transcript_response(first_exchange)
    assert pasted_document not in json.dumps(transcript_value)
    assert CASE_DOCUMENT_CONTEXT_PLACEHOLDER in json.dumps(transcript_value, ensure_ascii=False)


@pytest.mark.parametrize(
    "pasted_document",
    [
        pytest.param("상업송장\n수량: 10\n금액: 100", id="heading-and-fields"),
        pytest.param("품명: 냉동 고등어\n단가: USD 10", id="product-and-unit-price-fields"),
    ],
)
def test_functional_case_document_paste_skips_public_transcript_dispatch(
    tmp_path: Path, pasted_document: str
) -> None:
    """The raw Case audit record must not create any transcript event."""

    assistant_calls = 0

    def answer_handler(_request):
        nonlocal assistant_calls
        assistant_calls += 1
        raise AssertionError("a document-shaped Case paste must not call Agent")

    settings = _functional_settings()
    principal = Principal(
        subject="reviewer-1", org_id="local-dev", roles=frozenset({Role.reviewer})
    )
    repository = InMemoryWorkbenchRepository()
    objects = InMemoryObjectStore()
    agent = CallableAgentClient(
        lambda request: build_agent_v3_result(request, include_finding=True),
        answer=answer_handler,
    )
    service = WorkbenchService(repository=repository, object_store=objects, agent=agent)
    transcript_root = tmp_path / "_local" / "state" / "scenario-runs"
    transcript = FunctionalTranscriptController(
        root=ScenarioTranscriptRoot.create(transcript_root)
    )
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
        case_id = client.post(
            "/api/v1/cases", json={"title": "문서형 전사 경계"}, headers=_auth()
        ).json()["case_id"]
        assert client.post(
            f"/api/v1/cases/{case_id}/documents",
            json={"document_type": "CI", "filename": "invoice.txt", "content_text": "INV-001"},
            headers=_auth(),
        ).status_code == 201
        run_id = client.post(
            f"/api/v1/cases/{case_id}/reviews",
            json={"review_as_of": "2026-08-05"},
            headers={**_auth(), "Idempotency-Key": "document-transcript-review-001"},
        ).json()["job"]["review_run_id"]
        conversation_id = client.post(
            f"/api/v1/review-runs/{run_id}/assistant/conversations",
            json={},
            headers=_auth(),
        ).json()["conversation_id"]
        public_transcript = transcript_root / run_id / "public-api.jsonl"
        before = public_transcript.read_bytes()
        response = client.post(
            f"/api/v1/assistant/conversations/{conversation_id}/messages",
            json={"content": pasted_document},
            headers={**_auth(), "Idempotency-Key": "document-transcript-answer-001"},
        )

    assert response.status_code == 200, response.text
    assert response.json()["assistant_message"]["case_input_notice"] == "unregistered_document_text"
    assert assistant_calls == 0
    assert public_transcript.read_bytes() == before


@pytest.mark.parametrize(
    ("source_uri", "registry_sha256", "expected_status"),
    [
        (
            "https://www2.customs.go.kr/kcs/cm/cntnts/cntntsView.do?cntntsId=818&mi=2820",
            "a450ae6286d9a12729c5459543a35fd9c1c3df2ff59977700f72887c3df6543e",
            200,
        ),
        (
            "https://www2.customs.go.kr/kcs/cm/cntnts/cntntsView.do?cntntsId=818",
            "a450ae6286d9a12729c5459543a35fd9c1c3df2ff59977700f72887c3df6543e",
            409,
        ),
        (
            "https://www2.customs.go.kr/kcs/cm/cntnts/cntntsView.do?cntntsId=818&mi=2820",
            "0" * 64,
            409,
        ),
    ],
)
def test_knowledge_answer_validates_v2_registry_source_projection(
    source_uri: str, registry_sha256: str, expected_status: int
) -> None:
    def knowledge_handler(request):
        guidance = {
            "kind": "guidance_evidence",
            "source_id": "record-v2-export-clearance-001",
            "registry_source_id": "v2_kcs_export_clearance",
            "source_kind": "official_agency_guidance",
            "source_title": "수출 통관",
            "source_uri": source_uri,
            "as_of": request.as_of.isoformat(),
            "snapshot_id": "kr-trade-v2-2026-08-05",
            "coverage": "covered",
        }
        return {
            "schema_version": "1.0",
            "conversation_id": request.conversation_id,
            "answer_scope": "general_customs_guidance",
            "direct_answer": "공식 수출 통관 안내를 확인할 수 있습니다.",
            "citations": [guidance],
            "answer_sections": [
                {
                    "kind": "general_guidance",
                    "title": "일반 수출 통관 안내",
                    "content": "공식 수출 통관 안내를 확인할 수 있습니다.",
                    "citations": [guidance],
                }
            ],
            "suggested_questions": [],
            "coverage": "covered",
            "snapshot_id": "kr-trade-v2-2026-08-05",
            "as_of": request.as_of.isoformat(),
            "guidance_resolution": {
                "topic": "customs_clearance",
                "scope": "export_clearance_flow",
                "requested_direction": "export",
                "applicable_directions": ["export"],
                "coverage": "covered",
                "snapshot_id": "kr-trade-v2-2026-08-05",
                "as_of": request.as_of.isoformat(),
                "registry_version": "2.0",
            },
            "guidance_registry_sha256": registry_sha256,
            "engine": {"kind": "deterministic", "model_id": "v2-projection-fixture"},
        }

    with _client(knowledge_answer_handler=knowledge_handler) as client:
        conversation_id = client.post(
            "/api/v1/knowledge/conversations",
            json={"trade_type": "export", "as_of": "2026-08-05"},
            headers=_auth(),
        ).json()["conversation_id"]
        response = client.post(
            f"/api/v1/knowledge/conversations/{conversation_id}/messages",
            json={"content": "수출 통관 흐름을 알려 주세요."},
            headers={**_auth(), "Idempotency-Key": f"v2-projection-{expected_status}"},
        )

    assert response.status_code == expected_status, response.text
    if expected_status == 200:
        assistant = response.json()["assistant_message"]
        assert assistant["guidance_resolution"]["registry_version"] == "2.0"
        assert assistant["guidance_resolution"]["scope"] == "export_clearance_flow"
        assert "registry_source_id" not in json.dumps(assistant)
        assert "guidance_registry_sha256" not in json.dumps(assistant)


def test_v2_no_coverage_with_null_snapshot_preserves_an_existing_knowledge_pin() -> None:
    answer_count = 0

    def knowledge_handler(request):
        nonlocal answer_count
        answer_count += 1
        if answer_count == 2:
            return {
                "schema_version": "1.0",
                "conversation_id": request.conversation_id,
                "answer_scope": "insufficient_evidence",
                "direct_answer": "현재 공식 지식 스냅샷 범위에서는 수출 과세가격 기초를 확인할 수 없습니다.",
                "citations": [],
                "answer_sections": [],
                "suggested_questions": [],
                "coverage": "no_coverage",
                "snapshot_id": None,
                "as_of": request.as_of.isoformat(),
                "guidance_resolution": {
                    "topic": "customs_duty",
                    "scope": "customs_value_basics",
                    "requested_direction": "export",
                    "applicable_directions": ["import"],
                    "coverage": "no_coverage",
                    "snapshot_id": None,
                    "as_of": request.as_of.isoformat(),
                    "registry_version": "2.0",
                },
                "guidance_registry_sha256": (
                    "a450ae6286d9a12729c5459543a35fd9c1c3df2ff59977700f72887c3df6543e"
                ),
                "engine": {"kind": "deterministic", "model_id": "v2-no-coverage"},
            }
        guidance = {
            "kind": "guidance_evidence",
            "source_id": "record-v2-export-clearance-pin",
            "registry_source_id": "v2_kcs_export_clearance",
            "source_kind": "official_agency_guidance",
            "source_title": "수출 통관",
            "source_uri": (
                "https://www2.customs.go.kr/kcs/cm/cntnts/cntntsView.do?cntntsId=818&mi=2820"
            ),
            "as_of": request.as_of.isoformat(),
            "snapshot_id": "kr-trade-v2-2026-08-05",
            "coverage": "covered",
        }
        return {
            "schema_version": "1.0",
            "conversation_id": request.conversation_id,
            "answer_scope": "general_customs_guidance",
            "direct_answer": "공식 수출 통관 안내를 확인할 수 있습니다.",
            "citations": [guidance],
            "answer_sections": [
                {
                    "kind": "general_guidance",
                    "title": "일반 수출 통관 안내",
                    "content": "공식 수출 통관 안내를 확인할 수 있습니다.",
                    "citations": [guidance],
                }
            ],
            "suggested_questions": [],
            "coverage": "covered",
            "snapshot_id": "kr-trade-v2-2026-08-05",
            "as_of": request.as_of.isoformat(),
            "guidance_resolution": {
                "topic": "customs_clearance",
                "scope": "export_clearance_flow",
                "requested_direction": "export",
                "applicable_directions": ["export"],
                "coverage": "covered",
                "snapshot_id": "kr-trade-v2-2026-08-05",
                "as_of": request.as_of.isoformat(),
                "registry_version": "2.0",
            },
            "guidance_registry_sha256": (
                "a450ae6286d9a12729c5459543a35fd9c1c3df2ff59977700f72887c3df6543e"
            ),
            "engine": {"kind": "deterministic", "model_id": "v2-pin"},
        }

    with _client(knowledge_answer_handler=knowledge_handler) as client:
        conversation_id = client.post(
            "/api/v1/knowledge/conversations",
            json={"trade_type": "export", "as_of": "2026-08-05"},
            headers=_auth(),
        ).json()["conversation_id"]
        first = client.post(
            f"/api/v1/knowledge/conversations/{conversation_id}/messages",
            json={"content": "수출 통관 흐름을 알려 주세요."},
            headers={**_auth(), "Idempotency-Key": "v2-null-pin-first-001"},
        )
        second = client.post(
            f"/api/v1/knowledge/conversations/{conversation_id}/messages",
            json={"content": "수출 과세가격의 기본을 알려 주세요."},
            headers={**_auth(), "Idempotency-Key": "case-turn-02"},
        )
        listed = client.get("/api/v1/knowledge/conversations", headers=_auth())

    assert first.status_code == 200, first.text
    assert second.status_code == 200, second.text
    no_coverage = second.json()["assistant_message"]
    assert no_coverage["knowledge_coverage"] == "no_coverage"
    assert "knowledge_snapshot_id" not in no_coverage
    assert no_coverage["guidance_resolution"]["coverage"] == "no_coverage"
    assert no_coverage["guidance_resolution"].get("snapshot_id") is None
    assert "guidance_registry_sha256" not in json.dumps(no_coverage)
    assert listed.json()["items"][0]["snapshot_id"] == "kr-trade-v2-2026-08-05"
