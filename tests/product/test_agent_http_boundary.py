from __future__ import annotations

import hashlib
import json
from datetime import date
from io import BytesIO

import httpx
import pytest
from PIL import Image

from customs_backend.adapters import HttpAgentClient
from customs_backend.agent_contract import AgentContractPayloadError, validate_agent_v3_payload
from customs_backend.domain import (
    AgentAssistantAnswerRequest,
    AgentDocumentExtractionRequest,
    AgentRasterDocumentExtractionRequest,
    AgentRasterTextDocumentExtractionRequest,
    AgentDocumentInput,
    AgentKnowledgeAnswerRequest,
    AgentReviewRequest,
)
from customs_backend.ports import AgentResponseRejectedError, KnowledgeInputRejectedError
from customs_backend.raster_limits import (
    RASTER_AGENT_EXECUTION_TIMEOUT_SECONDS,
    RASTER_PAGE_RENDER_TIMEOUT_SECONDS,
    RASTER_PROFILE_STATUS_TIMEOUT_SECONDS,
    RASTER_WORKER_LEASE_GRACE_SECONDS,
    RASTER_WORKER_LEASE_SECONDS,
)
from tests.product.agent_v3_fixture import build_agent_v3_result


_V15_TEXT_PROFILE_REVISION = "synthetic-raster-text-v1:12f95aa0bd932311dd7e40fc"


def _tiny_png() -> bytes:
    output = BytesIO()
    Image.new("RGB", (1, 1), color=(0, 0, 0)).save(output, format="PNG")
    return output.getvalue()


def _request() -> AgentReviewRequest:
    return AgentReviewRequest(
        review_run_id="run-boundary",
        case_id="case-boundary",
        documents=[
            AgentDocumentInput(
                document_id="document-boundary",
                filename="invoice.txt",
                content_text="COMMERCIAL INVOICE",
            )
        ],
        review_as_of=date(2026, 8, 5),
        trade_type="import",
    )


def test_packaged_agent_v3_request_schema_accepts_backend_projection() -> None:
    request = _request()

    validate_agent_v3_payload("review_request", request.to_agent_wire())


def test_packaged_document_extraction_schemas_accept_only_header_metadata_and_preview() -> None:
    source_sha256 = "a" * 64
    metadata = {
        "document_asset_id": "asset-boundary",
        "extraction_request_id": "extract-boundary",
        "source_sha256": source_sha256,
        "media_type": "image/png",
    }
    validate_agent_v3_payload("document_extraction_request", metadata)
    validate_agent_v3_payload(
        "document_extraction_result",
        {
            "schema_version": "1.0",
            **metadata,
            "status": "preview_ready",
            "byte_size": 32,
            "canonical_text_sha256": "b" * 64,
            "extractor_revision": "local-preview-r1",
            "pages": [
                {
                    "page_number": 1,
                    "width": 100,
                    "height": 200,
                    "coordinate_unit": "pixels",
                    "spans": [
                        {
                            "span_id": "span-1",
                            "ordinal": 1,
                            "text": "candidate",
                            "bbox": {"x": 0, "y": 0, "width": 1, "height": 1},
                            "reading_order": 1,
                        }
                    ],
                }
            ],
            "warnings": [{"code": "human_confirmation_required"}],
        },
    )


def test_packaged_raster_extraction_schemas_accept_only_header_metadata_and_preview() -> None:
    source_sha256 = "a" * 64
    metadata = {
        "document_asset_id": "asset-boundary",
        "extraction_request_id": "raster-extract-boundary",
        "source_sha256": source_sha256,
        "media_type": "image/png",
        "raster_profile_revision": "synthetic-raster-v1",
    }
    validate_agent_v3_payload("raster_document_extraction_request", metadata)
    validate_agent_v3_payload(
        "raster_document_extraction_result",
        {
            "schema_version": "2.0",
            **metadata,
            "status": "unavailable",
            "byte_size": 32,
            "canonical_text_sha256": None,
            "extractor_revision": None,
            "document_type": "unknown",
            "classification_confidence": None,
            "pages": [],
            "field_candidates": [],
            "tables": [],
            "warnings": [{"code": "raster_engine_unavailable"}],
            "needs_review_reasons": [{"code": "raster_engine_unavailable"}],
        },
    )


def test_packaged_raster_preview_status_schema_has_no_activation_receipt_fields() -> None:
    validate_agent_v3_payload(
        "raster_document_preview_status",
        {
            "schema_version": "1.0",
            "availability": "available",
            "raster_profile_revision": "synthetic-raster-v1-fingerprint",
        },
    )
    with pytest.raises(AgentContractPayloadError):
        validate_agent_v3_payload(
            "raster_document_preview_status",
            {
                "schema_version": "1.0",
                "availability": "available",
                "raster_profile_revision": "synthetic-raster-v1-fingerprint",
                "pointer_sha256": "a" * 64,
            },
        )


def test_packaged_raster_text_schemas_reject_layout_and_bind_visible_text() -> None:
    source_sha256 = "a" * 64
    text = "Invoice No. INV-TEXT-001"
    metadata = {
        "document_asset_id": "asset-boundary",
        "extraction_request_id": "raster-text-extract-boundary",
        "source_sha256": source_sha256,
        "media_type": "image/png",
        "raster_text_profile_revision": _V15_TEXT_PROFILE_REVISION,
    }
    validate_agent_v3_payload("raster_text_extraction_request", metadata)
    valid = {
        "schema_version": "1.0",
        **metadata,
        "status": "needs_review",
        "byte_size": 32,
        "canonical_text": text,
        "canonical_text_sha256": hashlib.sha256(text.encode()).hexdigest(),
        "extractor_revision": "fixture-raster-text-r1",
        "pages": [{"page_number": 1, "text": text, "confidence": 0.91}],
        "warnings": [],
        "needs_review_reasons": [{"code": "human_confirmation_required"}],
    }
    validate_agent_v3_payload("raster_text_extraction_result", valid)
    with pytest.raises(AgentContractPayloadError):
        validate_agent_v3_payload(
            "raster_text_extraction_result",
            {**valid, "tables": []},
        )
    validate_agent_v3_payload(
        "raster_text_preview_status",
        {
            "schema_version": "1.0",
            "availability": "available",
            "raster_text_profile_revision": _V15_TEXT_PROFILE_REVISION,
        },
    )


def test_packaged_review_schema_accepts_nullable_temporal_context_and_coverage_additively() -> None:
    request = _request()
    legacy = build_agent_v3_result(request)

    # Existing Agent outputs omitted this additive field.  Keep them valid.
    validate_agent_v3_payload("review_result", legacy)

    context = {
        "law_as_of": request.review_as_of.isoformat(),
        "hsk_snapshot_as_of": "2026-01-01",
        "requirements_snapshot_as_of": "2026-07-26",
    }
    coverage = {"law": "covered", "hsk": "unavailable", "requirements": "unavailable"}
    current = {**legacy, "temporal_context": context, "temporal_coverage": coverage}
    current["result_sha256"] = "0" * 64
    validate_agent_v3_payload("review_result", current)

    null_context = {**legacy, "temporal_context": None}
    validate_agent_v3_payload("review_result", null_context)
    null_coverage = {**legacy, "temporal_context": context, "temporal_coverage": None}
    validate_agent_v3_payload("review_result", null_coverage)

    invalid = {**current, "temporal_context": {"law_as_of": "not-a-date"}}
    with pytest.raises(AgentContractPayloadError):
        validate_agent_v3_payload("review_result", invalid)
    invalid_coverage = {**current, "temporal_coverage": {"law": "partial"}}
    with pytest.raises(AgentContractPayloadError):
        validate_agent_v3_payload("review_result", invalid_coverage)


def test_packaged_assistant_schemas_accept_the_bounded_backend_projection() -> None:
    review = _request()
    request = AgentAssistantAnswerRequest(
        case_id=review.case_id,
        review_run_id=review.review_run_id,
        conversation_id="conversation-boundary",
        review_result=build_agent_v3_result(review),
        question="What should be reviewed?",
        recent_turns=[],
        expected_snapshot_id="kr-trade-v2-2026-08-05",
    )
    assistant_wire = request.to_agent_wire()
    assert assistant_wire["expected_snapshot_id"] == "kr-trade-v2-2026-08-05"
    validate_agent_v3_payload("assistant_answer_request", assistant_wire)
    validate_agent_v3_payload(
        "assistant_answer_response",
        {
            "schema_version": "1.0",
            "case_id": review.case_id,
            "review_run_id": review.review_run_id,
            "conversation_id": "conversation-boundary",
            "answer_scope": "case_grounded",
            "direct_answer": "Review the documented facts first.",
            "citations": [],
            "suggested_questions": [],
            "correction_draft": None,
            "engine": {"kind": "deterministic", "model_id": "fixture"},
        },
    )


def test_packaged_knowledge_schemas_accept_a_snapshot_grounded_projection() -> None:
    request = AgentKnowledgeAnswerRequest(
        conversation_id="knowledge-boundary",
        question="수출 신고 전에 무엇을 확인해야 하나요?",
        trade_type="export",
        as_of=date(2026, 8, 5),
        recent_turns=[],
        expected_snapshot_id="kr-trade-v2-2026-08-05",
    )
    citation = {
        "kind": "guidance_evidence",
        "source_id": "official-export-guide-2026",
        "source_kind": "official_law",
        "source_title": "관세청 수출 통관 안내",
        "source_uri": "https://www.customs.go.kr/guide/export",
        "as_of": "2026-08-05",
        "snapshot_id": "kr-trade-2026-08-05",
        "coverage": "covered",
    }
    knowledge_wire = request.to_agent_wire()
    assert knowledge_wire["expected_snapshot_id"] == "kr-trade-v2-2026-08-05"
    validate_agent_v3_payload("knowledge_answer_request", knowledge_wire)
    validate_agent_v3_payload(
        "knowledge_answer_response",
        {
            "schema_version": "1.0",
            "conversation_id": request.conversation_id,
            "answer_scope": "general_customs_guidance",
            "direct_answer": "수출 신고 전에는 필요한 증빙을 확인해야 합니다.",
            "citations": [citation],
            "answer_sections": [
                {
                    "kind": "general_guidance",
                    "title": "공식 수출 통관 안내",
                    "content": "수출 신고 전에는 필요한 증빙을 확인해야 합니다.",
                    "citations": [citation],
                }
            ],
            "suggested_questions": [],
            "coverage": "covered",
            "snapshot_id": "kr-trade-2026-08-05",
            "as_of": "2026-08-05",
            "engine": {"kind": "deterministic", "model_id": "fixture"},
        },
    )


def test_packaged_answer_schemas_accept_two_indexed_v2_guidance_resolutions() -> None:
    citation_0 = {
        "kind": "guidance_evidence",
        "source_id": "official-duty-guide-2026",
        "source_kind": "official_agency_guidance",
        "source_title": "납세신고 및 납부방법",
        "source_uri": "https://www.customs.go.kr/guide/duty",
        "as_of": "2026-08-05",
        "snapshot_id": "kr-trade-v2-2026-08-05",
        "coverage": "covered",
        "resolution_index": 0,
    }
    citation_1 = {
        "kind": "guidance_evidence",
        "source_id": "official-classification-guide-2026",
        "source_kind": "official_agency_guidance",
        "source_title": "품목분류 사전심사 신청방법",
        "source_uri": "https://www.customs.go.kr/guide/classification",
        "as_of": "2026-08-05",
        "snapshot_id": "kr-trade-v2-2026-08-05",
        "coverage": "covered",
        "resolution_index": 1,
    }
    resolutions = [
        {
            "topic": "customs_duty",
            "scope": "duty_payment_basics",
            "requested_direction": "import",
            "applicable_directions": ["import"],
            "coverage": "covered",
            "snapshot_id": "kr-trade-v2-2026-08-05",
            "as_of": "2026-08-05",
            "registry_version": "2.0",
        },
        {
            "topic": "hs_classification",
            "scope": "classification_principles",
            "requested_direction": "import",
            "applicable_directions": ["import", "export"],
            "coverage": "covered",
            "snapshot_id": "kr-trade-v2-2026-08-05",
            "as_of": "2026-08-05",
            "registry_version": "2.0",
        },
    ]
    shared = {
        "schema_version": "1.0",
        "answer_scope": "general_customs_guidance",
        "direct_answer": "두 공식 안내를 범위별로 분리했습니다.",
        "citations": [citation_0, citation_1],
        "answer_sections": [
            {
                "kind": "general_guidance",
                "resolution_index": 0,
                "title": "주제 1 공식 안내",
                "content": "납세·납부 기준을 확인합니다.",
                "citations": [citation_0],
            },
            {
                "kind": "general_guidance",
                "resolution_index": 1,
                "title": "주제 2 공식 안내",
                "content": "품목분류 기준을 확인합니다.",
                "citations": [citation_1],
            },
        ],
        "suggested_questions": [],
        "guidance_resolutions": resolutions,
        "answer_format": "multi_scope_checklist",
        "guidance_registry_sha256": "a450ae6286d9a12729c5459543a35fd9c1c3df2ff59977700f72887c3df6543e",
        "engine": {"kind": "deterministic", "model_id": "fixture"},
    }
    validate_agent_v3_payload(
        "knowledge_answer_response",
        {
            **shared,
            "conversation_id": "knowledge-boundary",
            "coverage": "covered",
            "snapshot_id": "kr-trade-v2-2026-08-05",
            "as_of": "2026-08-05",
        },
    )
    validate_agent_v3_payload(
        "assistant_answer_response",
        {
            **shared,
            "case_id": "case-boundary",
            "review_run_id": "run-boundary",
            "conversation_id": "conversation-boundary",
            "correction_draft": None,
        },
    )


def test_packaged_assistant_and_knowledge_schemas_accept_every_registry_source_kind() -> None:
    """The consumer contract must not collapse official registry categories.

    Data distinguishes official law text from its official catalog, agency
    guidance, requirement references, and public guidance.  The wire contract
    carries that distinction even though Functional-UAT later rejects the one
    deterministic development-only kind.
    """

    source_kinds = (
        "official_law",
        "official_law_catalog",
        "official_agency_guidance",
        "official_requirement_reference",
        "official_public_guidance",
        "development_fixture",
    )
    for source_kind in source_kinds:
        citation = {
            "kind": "guidance_evidence",
            "source_id": "guide-registry-source",
            "source_kind": source_kind,
            "source_title": "공식 통관 안내",
            "source_uri": "https://www.customs.go.kr/guide",
            "as_of": "2026-08-05",
            "snapshot_id": "kr-trade-2026-08-05",
            "coverage": "covered",
        }
        validate_agent_v3_payload(
            "assistant_answer_response",
            {
                "schema_version": "1.0",
                "case_id": "case-boundary",
                "review_run_id": "run-boundary",
                "conversation_id": "conversation-boundary",
                "answer_scope": "general_customs_guidance",
                "direct_answer": "공식 안내를 확인해야 합니다.",
                "citations": [citation],
                "answer_sections": [
                    {
                        "kind": "general_guidance",
                        "title": "공식 안내",
                        "content": "공식 안내를 확인해야 합니다.",
                        "citations": [citation],
                    }
                ],
                "suggested_questions": [],
                "correction_draft": None,
                "engine": {"kind": "deterministic", "model_id": "fixture"},
            },
        )
        validate_agent_v3_payload(
            "knowledge_answer_response",
            {
                "schema_version": "1.0",
                "conversation_id": "knowledge-boundary",
                "answer_scope": "general_customs_guidance",
                "direct_answer": "공식 안내를 확인해야 합니다.",
                "citations": [citation],
                "answer_sections": [
                    {
                        "kind": "general_guidance",
                        "title": "공식 안내",
                        "content": "공식 안내를 확인해야 합니다.",
                        "citations": [citation],
                    }
                ],
                "suggested_questions": [],
                "coverage": "covered",
                "snapshot_id": "kr-trade-2026-08-05",
                "as_of": "2026-08-05",
                "engine": {"kind": "deterministic", "model_id": "fixture"},
            },
        )


def test_review_uses_the_one_bearer_and_mode_bound_http_endpoint() -> None:
    seen: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content)
        seen.update(payload)
        assert request.method == "POST"
        assert request.url.path == "/internal/agent/v2/reviews"
        assert request.headers["Authorization"] == "Bearer test-token"
        assert request.headers["X-Customs-Service-Mode"] == "local-development"
        assert "image_pages" not in payload["documents"][0]
        assert "pdf_pages" not in payload["documents"][0]
        return httpx.Response(
            200,
            json={"accepted": True},
            headers={"X-Customs-Service-Mode": "local-development"},
            request=request,
        )

    client = HttpAgentClient(
        base_url="http://127.0.0.1:8090",
        service_token="test-token",
        service_mode="local-development",
        transport=httpx.MockTransport(handler),
    )
    try:
        assert client.review(_request()) == {"accepted": True}
    finally:
        client.close()

    assert set(seen) == {
        "case_id",
        "documents",
        "jurisdiction",
        "review_as_of",
        "review_run_id",
        "trade_type",
    }


def test_document_extraction_uses_raw_body_without_review_trace_or_filename() -> None:
    source = b"\x89PNG\r\n\x1a\nsynthetic-preview-source"
    request = AgentDocumentExtractionRequest(
        document_asset_id="asset-boundary",
        extraction_request_id="extract-boundary",
        media_type="image/png",
        source_sha256=hashlib.sha256(source).hexdigest(),
        byte_size=len(source),
    )
    events: list[tuple[str, str, dict[str, object]]] = []

    def handler(http_request: httpx.Request) -> httpx.Response:
        assert http_request.method == "POST"
        assert http_request.url.path == "/internal/agent/v2/document-extractions"
        assert http_request.content == source
        assert http_request.headers["Authorization"] == "Bearer test-token"
        assert http_request.headers["X-Customs-Service-Mode"] == "local-development"
        assert http_request.headers["Content-Type"] == "image/png"
        assert http_request.headers["X-Document-Asset-ID"] == request.document_asset_id
        assert http_request.headers["X-Extraction-Request-ID"] == request.extraction_request_id
        assert http_request.headers["X-Document-SHA256"] == request.source_sha256
        assert "filename" not in http_request.headers
        return httpx.Response(
            200,
            json={
                "schema_version": "1.0",
                "document_asset_id": request.document_asset_id,
                "extraction_request_id": request.extraction_request_id,
                "status": "unavailable",
                "media_type": request.media_type,
                "byte_size": request.byte_size,
                "source_sha256": request.source_sha256,
                "canonical_text_sha256": None,
                "extractor_revision": None,
                "pages": [],
                "warnings": [{"code": "preview_engine_unavailable"}],
            },
            headers={"X-Customs-Service-Mode": "local-development"},
            request=http_request,
        )

    client = HttpAgentClient(
        base_url="http://127.0.0.1:8090",
        service_token="test-token",
        service_mode="local-development",
        review_trace=lambda run_id, event, payload: events.append((run_id, event, payload)),
        transport=httpx.MockTransport(handler),
    )
    try:
        assert client.extract_document(request, source)["status"] == "unavailable"
    finally:
        client.close()

    assert events == []


def test_raster_document_extraction_uses_raw_body_and_profile_header_without_trace() -> None:
    source = b"\x89PNG\r\n\x1a\nsynthetic-raster-source"
    request = AgentRasterDocumentExtractionRequest(
        document_asset_id="asset-boundary",
        extraction_request_id="raster-extract-boundary",
        media_type="image/png",
        source_sha256=hashlib.sha256(source).hexdigest(),
        byte_size=len(source),
        raster_profile_revision="synthetic-raster-v1",
    )
    events: list[tuple[str, str, dict[str, object]]] = []

    def handler(http_request: httpx.Request) -> httpx.Response:
        assert http_request.method == "POST"
        assert http_request.url.path == "/internal/agent/v3/raster-document-extractions"
        assert http_request.content == source
        assert http_request.headers["Authorization"] == "Bearer test-token"
        assert http_request.headers["X-Customs-Service-Mode"] == "local-development"
        assert http_request.headers["Content-Type"] == "image/png"
        assert http_request.headers["X-Document-Asset-ID"] == request.document_asset_id
        assert http_request.headers["X-Extraction-Request-ID"] == request.extraction_request_id
        assert http_request.headers["X-Document-SHA256"] == request.source_sha256
        assert http_request.headers["X-Raster-Profile-Revision"] == request.raster_profile_revision
        assert "filename" not in http_request.headers
        assert http_request.extensions["timeout"] == {
            "connect": RASTER_AGENT_EXECUTION_TIMEOUT_SECONDS,
            "read": RASTER_AGENT_EXECUTION_TIMEOUT_SECONDS,
            "write": RASTER_AGENT_EXECUTION_TIMEOUT_SECONDS,
            "pool": RASTER_AGENT_EXECUTION_TIMEOUT_SECONDS,
        }
        return httpx.Response(
            200,
            json={
                "schema_version": "2.0",
                "document_asset_id": request.document_asset_id,
                "extraction_request_id": request.extraction_request_id,
                "media_type": request.media_type,
                "byte_size": request.byte_size,
                "source_sha256": request.source_sha256,
                "raster_profile_revision": request.raster_profile_revision,
                "status": "unavailable",
                "canonical_text_sha256": None,
                "extractor_revision": None,
                "document_type": "unknown",
                "classification_confidence": None,
                "pages": [],
                "field_candidates": [],
                "tables": [],
                "warnings": [{"code": "raster_engine_unavailable"}],
                "needs_review_reasons": [{"code": "raster_engine_unavailable"}],
            },
            headers={"X-Customs-Service-Mode": "local-development"},
            request=http_request,
        )

    client = HttpAgentClient(
        base_url="http://127.0.0.1:8090",
        service_token="test-token",
        service_mode="local-development",
        review_trace=lambda run_id, event, payload: events.append((run_id, event, payload)),
        transport=httpx.MockTransport(handler),
    )
    try:
        assert client.extract_raster_document(request, source)["status"] == "unavailable"
    finally:
        client.close()

    assert events == []


def test_raster_text_document_extraction_uses_a_distinct_raw_boundary_without_trace() -> None:
    source = b"\x89PNG\r\n\x1a\nsynthetic-raster-text-source"
    request = AgentRasterTextDocumentExtractionRequest(
        document_asset_id="asset-boundary",
        extraction_request_id="raster-text-extract-boundary",
        media_type="image/png",
        source_sha256=hashlib.sha256(source).hexdigest(),
        byte_size=len(source),
        raster_text_profile_revision=_V15_TEXT_PROFILE_REVISION,
    )
    events: list[tuple[str, str, dict[str, object]]] = []

    def handler(http_request: httpx.Request) -> httpx.Response:
        assert http_request.method == "POST"
        assert http_request.url.path == "/internal/agent/v3/raster-text-extractions"
        assert http_request.content == source
        assert http_request.headers["Authorization"] == "Bearer test-token"
        assert http_request.headers["X-Customs-Service-Mode"] == "local-development"
        assert http_request.headers["Content-Type"] == "image/png"
        assert http_request.headers["X-Document-Asset-ID"] == request.document_asset_id
        assert http_request.headers["X-Extraction-Request-ID"] == request.extraction_request_id
        assert http_request.headers["X-Document-SHA256"] == request.source_sha256
        assert (
            http_request.headers["X-Raster-Text-Profile-Revision"]
            == request.raster_text_profile_revision
        )
        assert "filename" not in http_request.headers
        assert http_request.extensions["timeout"] == {
            "connect": RASTER_AGENT_EXECUTION_TIMEOUT_SECONDS,
            "read": RASTER_AGENT_EXECUTION_TIMEOUT_SECONDS,
            "write": RASTER_AGENT_EXECUTION_TIMEOUT_SECONDS,
            "pool": RASTER_AGENT_EXECUTION_TIMEOUT_SECONDS,
        }
        return httpx.Response(
            200,
            json={
                "schema_version": "1.0",
                "document_asset_id": request.document_asset_id,
                "extraction_request_id": request.extraction_request_id,
                "media_type": request.media_type,
                "byte_size": request.byte_size,
                "source_sha256": request.source_sha256,
                "raster_text_profile_revision": request.raster_text_profile_revision,
                "status": "unavailable",
                "canonical_text": None,
                "canonical_text_sha256": None,
                "extractor_revision": None,
                "pages": [],
                "warnings": [{"code": "raster_text_engine_unavailable"}],
                "needs_review_reasons": [{"code": "raster_text_engine_unavailable"}],
            },
            headers={"X-Customs-Service-Mode": "local-development"},
            request=http_request,
        )

    client = HttpAgentClient(
        base_url="http://127.0.0.1:8090",
        service_token="test-token",
        service_mode="local-development",
        review_trace=lambda run_id, event, payload: events.append((run_id, event, payload)),
        transport=httpx.MockTransport(handler),
    )
    try:
        assert client.extract_raster_text_document(request, source)["status"] == "unavailable"
    finally:
        client.close()

    assert events == []


def test_raster_preview_status_uses_the_authenticated_no_body_agent_boundary() -> None:
    def handler(http_request: httpx.Request) -> httpx.Response:
        assert http_request.method == "GET"
        assert http_request.url.path == "/internal/agent/v3/raster-document-preview-status"
        assert http_request.content == b""
        assert http_request.headers["Authorization"] == "Bearer test-token"
        assert http_request.headers["X-Customs-Service-Mode"] == "local-development"
        assert http_request.extensions["timeout"] == {
            "connect": RASTER_PROFILE_STATUS_TIMEOUT_SECONDS,
            "read": RASTER_PROFILE_STATUS_TIMEOUT_SECONDS,
            "write": RASTER_PROFILE_STATUS_TIMEOUT_SECONDS,
            "pool": RASTER_PROFILE_STATUS_TIMEOUT_SECONDS,
        }
        return httpx.Response(
            200,
            json={
                "schema_version": "1.0",
                "availability": "available",
                "raster_profile_revision": "synthetic-raster-v1-fingerprint",
            },
            headers={"X-Customs-Service-Mode": "local-development"},
            request=http_request,
        )

    client = HttpAgentClient(
        base_url="http://127.0.0.1:8090",
        service_token="test-token",
        service_mode="local-development",
        transport=httpx.MockTransport(handler),
    )
    try:
        status = client.raster_document_preview_status()
    finally:
        client.close()

    assert status.availability == "available"
    assert status.raster_profile_revision == "synthetic-raster-v1-fingerprint"


def test_raster_preview_status_rejects_an_incoherent_or_unexpected_agent_payload() -> None:
    def handler(http_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "schema_version": "1.0",
                "availability": "available",
                "raster_profile_revision": None,
            },
            headers={"X-Customs-Service-Mode": "local-development"},
            request=http_request,
        )

    client = HttpAgentClient(
        base_url="http://127.0.0.1:8090",
        service_token="test-token",
        service_mode="local-development",
        transport=httpx.MockTransport(handler),
    )
    try:
        with pytest.raises(AgentResponseRejectedError):
            client.raster_document_preview_status()
    finally:
        client.close()


def test_raster_pdf_page_uses_raw_pdf_and_verifies_every_renderer_binding() -> None:
    source = b"%PDF-1.7\nsynthetic-source\n%%EOF\n"
    page = _tiny_png()
    request = AgentRasterDocumentExtractionRequest(
        document_asset_id="asset-boundary",
        extraction_request_id="raster-page-boundary",
        media_type="application/pdf",
        source_sha256=hashlib.sha256(source).hexdigest(),
        byte_size=len(source),
        raster_profile_revision="synthetic-raster-v1",
    )

    def handler(http_request: httpx.Request) -> httpx.Response:
        assert http_request.method == "POST"
        assert http_request.url.path == "/internal/agent/v3/raster-document-pages/1"
        assert http_request.content == source
        assert http_request.headers["Authorization"] == "Bearer test-token"
        assert http_request.headers["X-Customs-Service-Mode"] == "local-development"
        assert http_request.headers["Content-Type"] == "application/pdf"
        assert http_request.headers["X-Document-Asset-ID"] == request.document_asset_id
        assert http_request.headers["X-Extraction-Request-ID"] == request.extraction_request_id
        assert http_request.headers["X-Document-SHA256"] == request.source_sha256
        assert http_request.headers["X-Raster-Profile-Revision"] == request.raster_profile_revision
        assert http_request.extensions["timeout"] == {
            "connect": RASTER_PAGE_RENDER_TIMEOUT_SECONDS,
            "read": RASTER_PAGE_RENDER_TIMEOUT_SECONDS,
            "write": RASTER_PAGE_RENDER_TIMEOUT_SECONDS,
            "pool": RASTER_PAGE_RENDER_TIMEOUT_SECONDS,
        }
        return httpx.Response(
            200,
            content=page,
            headers={
                "Content-Type": "image/png",
                "X-Customs-Service-Mode": "local-development",
                "X-Document-SHA256": request.source_sha256,
                "X-Raster-Profile-Revision": request.raster_profile_revision,
                "X-Raster-Page-Number": "1",
                "X-Raster-Page-Width": "1",
                "X-Raster-Page-Height": "1",
                "X-Raster-Page-SHA256": hashlib.sha256(page).hexdigest(),
                "X-Raster-Renderer-Revision": "pdfium-fixture-dpi-216",
            },
            request=http_request,
        )

    client = HttpAgentClient(
        base_url="http://127.0.0.1:8090",
        service_token="test-token",
        service_mode="local-development",
        transport=httpx.MockTransport(handler),
    )
    try:
        rendered = client.render_raster_document_page(request, source, 1)
    finally:
        client.close()

    assert rendered.content == page
    assert rendered.width == 1
    assert rendered.height == 1
    assert rendered.renderer_revision == "pdfium-fixture-dpi-216"


def test_raster_timeout_and_lease_contract_stays_within_the_one_minute_ocr_budget() -> None:
    """Prevent the OCR transport and durable queue lease from drifting apart."""

    assert RASTER_AGENT_EXECUTION_TIMEOUT_SECONDS == 60.0
    assert RASTER_PAGE_RENDER_TIMEOUT_SECONDS < RASTER_AGENT_EXECUTION_TIMEOUT_SECONDS
    assert RASTER_PROFILE_STATUS_TIMEOUT_SECONDS < RASTER_PAGE_RENDER_TIMEOUT_SECONDS
    assert RASTER_WORKER_LEASE_SECONDS == (
        RASTER_AGENT_EXECUTION_TIMEOUT_SECONDS + RASTER_WORKER_LEASE_GRACE_SECONDS
    )
    assert RASTER_WORKER_LEASE_SECONDS > RASTER_AGENT_EXECUTION_TIMEOUT_SECONDS


@pytest.mark.parametrize(
    ("page", "declared_width", "declared_height"),
    [
        (b"\x00" + _tiny_png()[1:], "1", "1"),
        (
            b"\x89PNG\r\n\x1a\n"
            + (12).to_bytes(4, byteorder="big")
            + b"IHDR"
            + (1).to_bytes(4, byteorder="big")
            + (1).to_bytes(4, byteorder="big")
            + b"\x08\x02\x00\x00\x00\x00\x00\x00\x00",
            "1",
            "1",
        ),
        (
            b"\x89PNG\r\n\x1a\n"
            + (13).to_bytes(4, byteorder="big")
            + b"JHDR"
            + (1).to_bytes(4, byteorder="big")
            + (1).to_bytes(4, byteorder="big")
            + b"\x08\x02\x00\x00\x00\x00\x00\x00\x00",
            "1",
            "1",
        ),
        (_tiny_png(), "2", "1"),
    ],
    ids=["signature", "ihdr-length", "ihdr-type", "header-dimensions"],
)
def test_raster_pdf_page_rejects_invalid_png_ihdr_or_header_dimensions(
    page: bytes, declared_width: str, declared_height: str
) -> None:
    source = b"%PDF-1.7\nsynthetic-source\n%%EOF\n"
    request = AgentRasterDocumentExtractionRequest(
        document_asset_id="asset-boundary",
        extraction_request_id="raster-page-boundary",
        media_type="application/pdf",
        source_sha256=hashlib.sha256(source).hexdigest(),
        byte_size=len(source),
        raster_profile_revision="synthetic-raster-v1",
    )

    def handler(http_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            content=page,
            headers={
                "Content-Type": "image/png",
                "X-Customs-Service-Mode": "local-development",
                "X-Document-SHA256": request.source_sha256,
                "X-Raster-Profile-Revision": request.raster_profile_revision,
                "X-Raster-Page-Number": "1",
                "X-Raster-Page-Width": declared_width,
                "X-Raster-Page-Height": declared_height,
                "X-Raster-Page-SHA256": hashlib.sha256(page).hexdigest(),
                "X-Raster-Renderer-Revision": "pdfium-fixture-dpi-216",
            },
            request=http_request,
        )

    client = HttpAgentClient(
        base_url="http://127.0.0.1:8090",
        service_token="test-token",
        service_mode="local-development",
        transport=httpx.MockTransport(handler),
    )
    try:
        with pytest.raises(AgentResponseRejectedError):
            client.render_raster_document_page(request, source, 1)
    finally:
        client.close()


def test_assistant_uses_the_same_bearer_and_mode_bound_http_endpoint() -> None:
    review = _request()
    assistant = AgentAssistantAnswerRequest(
        case_id=review.case_id,
        review_run_id=review.review_run_id,
        conversation_id="conversation-boundary",
        review_result=build_agent_v3_result(review),
        question="What should be reviewed?",
        recent_turns=[],
    )

    def handler(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content)
        assert request.method == "POST"
        assert request.url.path == "/internal/agent/v2/assistant/answers"
        assert request.headers["Authorization"] == "Bearer test-token"
        assert request.headers["X-Customs-Service-Mode"] == "local-development"
        assert payload["question"] == "What should be reviewed?"
        return httpx.Response(
            200,
            json={
                "schema_version": "1.0",
                "case_id": review.case_id,
                "review_run_id": review.review_run_id,
                "conversation_id": "conversation-boundary",
                "answer_scope": "case_grounded",
                "direct_answer": "Review the documented facts first.",
                "citations": [],
                "suggested_questions": [],
                "correction_draft": None,
                "engine": {"kind": "deterministic", "model_id": "fixture"},
            },
            headers={"X-Customs-Service-Mode": "local-development"},
            request=request,
        )

    client = HttpAgentClient(
        base_url="http://127.0.0.1:8090",
        service_token="test-token",
        service_mode="local-development",
        transport=httpx.MockTransport(handler),
    )
    try:
        assert client.answer(assistant)["answer_scope"] == "case_grounded"
    finally:
        client.close()


def test_knowledge_assistant_uses_the_dedicated_mode_bound_http_endpoint() -> None:
    knowledge = AgentKnowledgeAnswerRequest(
        conversation_id="knowledge-boundary",
        question="수출 신고 전에 무엇을 확인해야 하나요?",
        trade_type="export",
        as_of=date(2026, 8, 5),
        recent_turns=[],
    )

    def handler(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content)
        assert request.method == "POST"
        assert request.url.path == "/internal/agent/v2/knowledge/answers"
        assert request.headers["Authorization"] == "Bearer test-token"
        assert request.headers["X-Customs-Service-Mode"] == "local-development"
        assert payload["trade_type"] == "export"
        assert "case_id" not in payload
        return httpx.Response(
            200,
            json={"accepted": True},
            headers={"X-Customs-Service-Mode": "local-development"},
            request=request,
        )

    client = HttpAgentClient(
        base_url="http://127.0.0.1:8090",
        service_token="test-token",
        service_mode="local-development",
        transport=httpx.MockTransport(handler),
    )
    try:
        assert client.answer_knowledge(knowledge) == {"accepted": True}
    finally:
        client.close()


def test_knowledge_assistant_preserves_a_safe_agent_input_boundary_without_body_echo() -> None:
    knowledge = AgentKnowledgeAnswerRequest(
        conversation_id="knowledge-boundary",
        question="일반 통관 안내를 알려 주세요.",
        trade_type="both",
        as_of=date(2026, 8, 5),
        recent_turns=[],
    )

    client = HttpAgentClient(
        base_url="http://127.0.0.1:8090",
        service_token="test-token",
        service_mode="local-development",
        transport=httpx.MockTransport(
            lambda request: httpx.Response(
                422,
                json={"code": "knowledge_input_boundary", "detail": "must not surface"},
                headers={"X-Customs-Service-Mode": "local-development"},
                request=request,
            )
        ),
    )
    try:
        with pytest.raises(KnowledgeInputRejectedError) as rejected:
            client.answer_knowledge(knowledge)
    finally:
        client.close()

    assert "must not surface" not in str(rejected.value)


def test_metadata_requires_the_same_mode_in_both_directions() -> None:
    accepted = HttpAgentClient(
        base_url="http://127.0.0.1:8090",
        service_token="test-token",
        service_mode="functional-uat",
        transport=httpx.MockTransport(
            lambda request: httpx.Response(
                200,
                json={"status": "ready"},
                headers={"X-Customs-Service-Mode": "functional-uat"},
                request=request,
            )
        ),
    )
    try:
        assert accepted.metadata() == {"status": "ready"}
    finally:
        accepted.close()

    rejected = HttpAgentClient(
        base_url="http://127.0.0.1:8090",
        service_token="test-token",
        service_mode="functional-uat",
        transport=httpx.MockTransport(
            lambda request: httpx.Response(200, json={"status": "ready"}, request=request)
        ),
    )
    try:
        with pytest.raises(AgentResponseRejectedError, match="mode"):
            rejected.metadata()
    finally:
        rejected.close()


def test_non_successful_review_is_rejected_without_a_retry_protocol() -> None:
    client = HttpAgentClient(
        base_url="http://127.0.0.1:8090",
        service_token="test-token",
        service_mode="local-development",
        transport=httpx.MockTransport(
            lambda request: httpx.Response(
                503,
                json={"error": "unavailable"},
                headers={"X-Customs-Service-Mode": "local-development"},
                request=request,
            )
        ),
    )
    try:
        with pytest.raises(AgentResponseRejectedError, match="HTTP 503"):
            client.review(_request())
    finally:
        client.close()


def test_review_trace_records_success_body_but_never_transport_headers() -> None:
    events: list[tuple[str, str, dict[str, object]]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={"accepted": True},
            headers={"X-Customs-Service-Mode": "functional-uat"},
            request=request,
        )

    client = HttpAgentClient(
        base_url="http://127.0.0.1:8090",
        service_token="live-service-token-value",
        service_mode="functional-uat",
        review_trace=lambda run_id, event, payload: events.append((run_id, event, payload)),
        transport=httpx.MockTransport(handler),
    )
    try:
        assert client.review(_request()) == {"accepted": True}
    finally:
        client.close()

    assert [event for _run_id, event, _payload in events] == ["review_request", "review_response"]
    assert all(run_id == "run-boundary" for run_id, _event, _payload in events)
    assert events[0][2]["path"] == "/internal/agent/v2/reviews"
    assert events[1][2]["response_body"] == {"accepted": True}
    assert "authorization" not in events[0][2]
    assert "live-service-token-value" not in json.dumps(events)


def test_review_trace_never_records_non_success_response_body() -> None:
    events: list[tuple[str, str, dict[str, object]]] = []
    client = HttpAgentClient(
        base_url="http://127.0.0.1:8090",
        service_token="test-token",
        service_mode="local-development",
        review_trace=lambda run_id, event, payload: events.append((run_id, event, payload)),
        transport=httpx.MockTransport(
            lambda request: httpx.Response(
                503,
                json={"provider_error": "raw secret failure response"},
                headers={"X-Customs-Service-Mode": "local-development"},
                request=request,
            )
        ),
    )
    try:
        with pytest.raises(AgentResponseRejectedError, match="HTTP 503"):
            client.review(_request())
    finally:
        client.close()

    assert [event for _run_id, event, _payload in events] == ["review_request", "review_failure"]
    failure = events[1][2]
    assert failure["response_status"] == 503
    assert "response_body" not in failure
    assert "raw secret failure response" not in json.dumps(events)
