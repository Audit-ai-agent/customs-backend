from __future__ import annotations

import base64
from copy import deepcopy
import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import httpx
import pytest

from tools import run_functional_uat_matrix as matrix


def _unsigned_token_with_expiry(expiry: int) -> str:
    payload = base64.urlsafe_b64encode(
        json.dumps({"exp": expiry}, separators=(",", ":")).encode("utf-8")
    ).rstrip(b"=")
    return f"header.{payload.decode('ascii')}.signature"


def test_functional_uat_oidc_reuses_a_valid_in_memory_token(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    oidc = matrix.FunctionalUatOidc({}, redirect_uri="http://127.0.0.1:5173/")
    oidc._access_tokens["reviewer"] = ("cached-token", 500.0)
    monkeypatch.setattr(matrix.time, "time", lambda: 100.0)

    assert oidc.access_token("reviewer") == "cached-token"


def test_access_token_expiry_parses_only_a_valid_numeric_expiry() -> None:
    assert matrix._access_token_expiry(_unsigned_token_with_expiry(500)) == 500.0

    with pytest.raises(matrix.MatrixError, match="functional_uat_token_response_invalid"):
        matrix._access_token_expiry("not-a-jwt")


def test_public_api_resolves_a_token_supplier_for_each_request(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    supplied = iter(["a" * 32, "b" * 32])
    client = matrix.PublicApi(
        base_url="http://127.0.0.1:18081/api/v1", access_token=lambda: next(supplied)
    )
    authorization_headers: list[str] = []

    def request(*_args: Any, **kwargs: Any) -> httpx.Response:
        authorization_headers.append(kwargs["headers"]["Authorization"])
        return httpx.Response(200, headers={"content-type": "application/json"}, json={})

    monkeypatch.setattr(client._client, "request", request)

    assert client.require("GET", "/health") == {}
    assert client.require("GET", "/health") == {}
    assert authorization_headers == [f"Bearer {'a' * 32}", f"Bearer {'b' * 32}"]
    client.close()


def test_public_api_reauthenticates_once_for_a_read_after_an_expired_token_401(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    renewal_count = 0
    authorization_headers: list[str] = []
    client = matrix.PublicApi(
        base_url="http://127.0.0.1:18081/api/v1",
        access_token="expired-token",
        renew_access_token=lambda: "renewed-token",
    )
    responses = iter(
        [
            httpx.Response(401, headers={"content-type": "application/problem+json"}, json={}),
            httpx.Response(200, headers={"content-type": "application/json"}, json={}),
        ]
    )

    def request(*_args: Any, **kwargs: Any) -> httpx.Response:
        authorization_headers.append(kwargs["headers"]["Authorization"])
        return next(responses)

    def renew() -> str:
        nonlocal renewal_count
        renewal_count += 1
        return "renewed-token"

    client._renew_access_token = renew
    monkeypatch.setattr(client._client, "request", request)

    assert client.require("GET", "/cases") == {}
    assert renewal_count == 1
    assert authorization_headers == ["Bearer expired-token", "Bearer renewed-token"]
    client.close()


def test_public_api_keeps_writes_and_a_repeated_read_401_fail_closed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    renewals = 0

    def renew() -> str:
        nonlocal renewals
        renewals += 1
        return "renewed-token"

    client = matrix.PublicApi(
        base_url="http://127.0.0.1:18081/api/v1",
        access_token="initial-token",
        renew_access_token=renew,
    )
    responses = iter(
        [
            httpx.Response(403, headers={"content-type": "application/problem+json"}, json={}),
            httpx.Response(401, headers={"content-type": "application/problem+json"}, json={}),
            httpx.Response(401, headers={"content-type": "application/problem+json"}, json={}),
            httpx.Response(401, headers={"content-type": "application/problem+json"}, json={}),
        ]
    )
    monkeypatch.setattr(client._client, "request", lambda *_args, **_kwargs: next(responses))

    assert client.require("GET", "/cases", expected=403) == {}
    assert renewals == 0
    assert client.require("POST", "/cases", body={"title": "기능 UAT"}, expected=401) == {}
    assert renewals == 0
    with pytest.raises(matrix.MatrixError, match="public_api_status_get_401"):
        client.require("GET", "/cases")
    assert renewals == 1
    client.close()


def test_public_api_accepts_rfc7807_json_for_an_expected_rejected_probe(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = matrix.PublicApi(base_url="http://127.0.0.1:18081/api/v1", access_token="test-token")
    response = httpx.Response(
        422,
        headers={"content-type": "application/problem+json; charset=utf-8"},
        json={"code": "knowledge_input_not_allowed"},
    )
    monkeypatch.setattr(client._client, "request", lambda *_args, **_kwargs: response)

    assert client.require(
        "POST",
        "/knowledge/conversations/example/messages",
        body={"content": "안전한 요청"},
        idempotency_key="knowledge-problem-json-001",
        expected=422,
    ) == {"code": "knowledge_input_not_allowed"}
    client.close()


def test_public_api_keeps_html_error_responses_non_json(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = matrix.PublicApi(base_url="http://127.0.0.1:18081/api/v1", access_token="test-token")
    response = httpx.Response(422, headers={"content-type": "text/html"}, text="rejected")
    monkeypatch.setattr(client._client, "request", lambda *_args, **_kwargs: response)

    with pytest.raises(matrix.MatrixError, match="public_api_non_json_post"):
        client.require(
            "POST",
            "/knowledge/conversations/example/messages",
            body={"content": "안전한 요청"},
            idempotency_key="knowledge-html-rejection-001",
            expected=422,
        )
    client.close()


def test_matrix_cases_are_marked_as_system_functional_uat_cases() -> None:
    scenarios = matrix._scenario_matrix()

    assert len(scenarios) == 7
    for scenario in scenarios:
        body = matrix._case_create_body(scenario)
        assert body == {
            "title": scenario.case_title,
            "metadata": {
                "system_case_kind": "functional_uat_matrix",
                "system_scenario": scenario.name,
            },
        }
        assert scenario.case_title.startswith("기능 UAT · ")
        assert all(document.filename.endswith(".txt") for document in scenario.documents)
        assert all(
            any("가" <= character <= "힣" for character in document.filename)
            for document in scenario.documents
        )
    export = next(
        item for item in scenarios if item.name == "export-documents-and-submission-evidence"
    )
    assert export.trade_type == "export"
    assert {document.document_type for document in export.documents} >= {
        "AWB",
        "EXPORT_DECLARATION",
        "SUPPORTING_EVIDENCE",
    }


def test_matrix_replacement_document_uses_a_korean_business_filename() -> None:
    assert matrix._REPLACEMENT_DOCUMENT_FILENAME == "상업송장_보완본.txt"


def _prepare_case_document_paste_streams(tmp_path: Path, run_id: str) -> None:
    directory = tmp_path / run_id
    directory.mkdir()
    for stream in matrix._CASE_DOCUMENT_PASTE_STREAMS:
        (directory / f"{stream}.jsonl").write_bytes(b"opaque-existing-event\n")


def _case_document_paste_assistant() -> dict[str, Any]:
    # Deliberately omit user/assistant prose: the matrix guard must validate
    # fixed safe fields without looking at a pasted body or generated prose.
    return {
        "role": "assistant",
        "answer_scope": "insufficient_evidence",
        "case_input_notice": "unregistered_document_text",
        "citations": [],
        "answer_sections": [],
    }


def test_case_document_paste_matrix_keeps_safe_aggregate_zero_dispatch_receipt(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    run_id = "run-case-document-paste"
    _prepare_case_document_paste_streams(tmp_path, run_id)
    review = _matching_result()
    sent_paste_count = 0

    class FakeApi:
        def require(
            self,
            method: str,
            path: str,
            *,
            body: dict[str, Any] | None = None,
            **_kwargs: Any,
        ) -> dict[str, Any]:
            nonlocal sent_paste_count
            if method == "POST" and path.endswith("/assistant/conversations"):
                return {"conversation_id": "conversation-case-document-paste"}
            if method == "POST" and path.endswith("/messages"):
                assert body == {"content": matrix._CASE_DOCUMENT_PASTE_CONTENT}
                sent_paste_count += 1
                return {"assistant_message": _case_document_paste_assistant()}
            if method == "GET" and path == f"/review-runs/{run_id}":
                return deepcopy(review)
            if method == "GET" and path.endswith("/correction-proposals"):
                return {"items": []}
            raise AssertionError(f"unexpected request: {method} {path}")

    monkeypatch.setattr(matrix, "workspace_transcript_root", lambda: tmp_path)

    receipt = matrix._assert_case_document_paste_zero_dispatch(
        FakeApi(), run_id=run_id, run=review
    )

    assert sent_paste_count == 1
    assert receipt == {
        "status": "passed",
        "case_document_paste_notice_verified": True,
        "case_document_paste_agent_http_exchanges": 0,
        "case_document_paste_public_transcript_exchanges": 0,
        "case_document_paste_agent_data_dispatches": 0,
        "case_document_paste_claude_dispatches": 0,
        "case_document_paste_fact_changes": 0,
        "case_document_paste_finding_changes": 0,
        "case_document_paste_correction_changes": 0,
    }
    assert matrix._CASE_DOCUMENT_PASTE_CONTENT not in repr(receipt)
    assert "conversation-case-document-paste" not in repr(receipt)


@pytest.mark.parametrize("changed_stream", matrix._CASE_DOCUMENT_PASTE_STREAMS)
def test_case_document_paste_matrix_rejects_any_dispatch_stream_change(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, changed_stream: str
) -> None:
    run_id = "run-case-document-paste-dispatch"
    _prepare_case_document_paste_streams(tmp_path, run_id)
    review = _matching_result()

    class FakeApi:
        def require(
            self,
            method: str,
            path: str,
            *,
            body: dict[str, Any] | None = None,
            **_kwargs: Any,
        ) -> dict[str, Any]:
            if method == "POST" and path.endswith("/assistant/conversations"):
                return {"conversation_id": "conversation-case-document-paste"}
            if method == "POST" and path.endswith("/messages"):
                assert body == {"content": matrix._CASE_DOCUMENT_PASTE_CONTENT}
                with (tmp_path / run_id / f"{changed_stream}.jsonl").open("ab") as handle:
                    handle.write(b"unexpected-dispatch\n")
                return {"assistant_message": _case_document_paste_assistant()}
            if method == "GET" and path.endswith("/correction-proposals"):
                return {"items": []}
            raise AssertionError(f"unexpected request: {method} {path}")

    monkeypatch.setattr(matrix, "workspace_transcript_root", lambda: tmp_path)

    with pytest.raises(matrix.MatrixError, match="case_document_paste_transcript_dispatch"):
        matrix._assert_case_document_paste_zero_dispatch(FakeApi(), run_id=run_id, run=review)


@pytest.mark.parametrize(
    ("changed_state", "error"),
    [
        ("facts", "case_document_paste_review_state_changed"),
        ("findings", "case_document_paste_review_state_changed"),
        ("corrections", "case_document_paste_correction_state_changed"),
    ],
)
def test_case_document_paste_matrix_rejects_fact_finding_or_correction_changes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    changed_state: str,
    error: str,
) -> None:
    run_id = "run-case-document-paste-state"
    _prepare_case_document_paste_streams(tmp_path, run_id)
    review = _matching_result()
    pasted = False

    class FakeApi:
        def require(
            self,
            method: str,
            path: str,
            *,
            body: dict[str, Any] | None = None,
            **_kwargs: Any,
        ) -> dict[str, Any]:
            nonlocal pasted
            if method == "POST" and path.endswith("/assistant/conversations"):
                return {"conversation_id": "conversation-case-document-paste"}
            if method == "POST" and path.endswith("/messages"):
                assert body == {"content": matrix._CASE_DOCUMENT_PASTE_CONTENT}
                pasted = True
                return {"assistant_message": _case_document_paste_assistant()}
            if method == "GET" and path == f"/review-runs/{run_id}":
                value = deepcopy(review)
                if pasted and changed_state == "facts":
                    value["document_analyses"][0]["facts"].append({})
                if pasted and changed_state == "findings":
                    value["findings"].append({})
                return value
            if method == "GET" and path.endswith("/correction-proposals"):
                return {"items": [{}] if pasted and changed_state == "corrections" else []}
            raise AssertionError(f"unexpected request: {method} {path}")

    monkeypatch.setattr(matrix, "workspace_transcript_root", lambda: tmp_path)

    with pytest.raises(matrix.MatrixError, match=error):
        matrix._assert_case_document_paste_zero_dispatch(FakeApi(), run_id=run_id, run=review)


def _matching_result() -> dict[str, Any]:
    return {
        "review_as_of": "2026-08-05",
        "execution_status": "succeeded",
        "workflow": {
            "status": "completed",
            "stages": [
                {
                    "stage": "retrieve_evidence",
                    "status": "completed",
                    "summary": "법령 근거를 한 번 조회했습니다.",
                }
            ],
            "planner": {"actions": [{"summary": "문서 사실을 비교하고 법령 근거를 확인했습니다."}]},
        },
        "document_analyses": [
            {
                "document_id": "document-invoice",
                "filename": "상업송장_정합확인.txt",
                "document_type": "invoice",
                "facts": [{"name": "hs_code"}],
                "line_items": [],
            },
            {
                "document_id": "document-packing",
                "filename": "포장명세서_정합확인.txt",
                "document_type": "packing_list",
                "facts": [],
                "line_items": [{"facts": [{"name": "hs_code"}]}],
            },
            {
                "document_id": "document-origin",
                "filename": "원산지증명서_정합확인.txt",
                "document_type": "certificate_of_origin",
                "facts": [{"name": "hs_codes"}],
                "line_items": [],
            },
            {
                "document_id": "document-bill",
                "filename": "선하증권_정합확인.txt",
                "document_type": "bill_of_lading",
                "facts": [],
                "line_items": [],
            },
        ],
        "comparisons": [
            {
                "comparison_id": "comparison-quantity",
                "rule_code": "invoice_packing_quantity_unit",
                "message": "상업송장과 포장명세서의 수량과 단위가 일치합니다.",
            }
        ],
        "evidence": [{"evidence_id": "evidence-food-import", "item_id": "item-fish"}],
        "findings": [
            {
                "finding_id": "finding-food-import",
                "kind": "regulatory_finding",
                "code": "food_import_confirmation",
                "evidence_ids": ["evidence-food-import"],
                "document_ids": [
                    "document-invoice",
                    "document-packing",
                    "document-origin",
                ],
                "message": "필수 확인 항목: 수입식품등 수입신고확인증",
            }
        ],
        "required_actions": [
            {
                "action_code": "inspect-food-import",
                "finding_ids": ["finding-food-import"],
                "message": "수입식품등 수입신고확인증의 제출 여부를 확인합니다.",
            },
            {
                "action_code": "human_review_required",
                "finding_ids": [],
                "message": "자동완료하지 않고 담당자가 최종 검토합니다.",
            },
        ],
        "report": {
            "summary": "문서 간 정합성과 법령 근거를 확인했습니다.",
            "disclaimer": "이 결과는 담당자의 최종 검토가 필요합니다.",
            "sections": [
                {
                    "title": title,
                    "claims": [{"text": "현재 심사 결과를 바탕으로 확인 사항을 정리했습니다."}],
                }
                for title in (
                    "검토 결론",
                    "심사 진행 과정",
                    "문서 간 비교",
                    "확인 사항과 원인",
                    "법령 근거",
                    "영향",
                    "다음 조치",
                    "한계",
                )
            ],
        },
    }


def test_matching_scenario_requires_hs_alias_grouped_requirement_and_korean_prose() -> None:
    scenario = next(
        item for item in matrix._scenario_matrix() if item.name == "all-documents-match"
    )
    result = _matching_result()

    matrix._assert_scenario_semantics(result, scenario)
    matrix._assert_run_user_prose(result)


@pytest.mark.parametrize(
    "mutate, error",
    [
        (
            lambda result: result["report"]["sections"][0]["claims"].__setitem__(
                0, {"text": "Document validation is incomplete."}
            ),
            "not_korean",
        ),
        (
            lambda result: result["report"]["sections"][0]["claims"].__setitem__(
                0, {"text": "상업송장_정합확인.txt의 내용을 확인합니다."}
            ),
            "raw_filename",
        ),
    ],
)
def test_user_prose_rejects_english_or_raw_filenames(mutate, error: str) -> None:
    result = _matching_result()
    mutate(result)

    with pytest.raises(matrix.MatrixError, match=error):
        matrix._assert_run_user_prose(result)


@pytest.mark.parametrize(
    "mutate, error",
    [
        (
            lambda result: result["findings"].append(
                {
                    "finding_id": "finding-missing-hs",
                    "kind": "validation_issue",
                    "code": "hs_code_not_found",
                    "message": "HS 코드 확인이 필요합니다.",
                }
            ),
            "false_hs_missing_finding",
        ),
        (
            lambda result: result["workflow"].update({"status": "partial"}),
            "false_data_partial",
        ),
        (
            lambda result: result["findings"].append(
                {
                    "finding_id": "finding-food-import-duplicate",
                    "kind": "regulatory_finding",
                    "code": "food_import_confirmation",
                    "evidence_ids": ["evidence-food-import-duplicate"],
                    "document_ids": ["document-invoice"],
                    "message": "필수 확인 항목: 수입식품등 수입신고확인증",
                }
            ),
            "regulatory_requirement_count",
        ),
    ],
)
def test_matching_semantics_rejects_false_hs_data_partial_or_duplicate_requirement(
    mutate, error: str
) -> None:
    scenario = next(
        item for item in matrix._scenario_matrix() if item.name == "all-documents-match"
    )
    result = deepcopy(_matching_result())
    mutate(result)

    with pytest.raises(matrix.MatrixError, match=error):
        matrix._assert_scenario_semantics(result, scenario)


def test_assistant_scopes_require_grounded_answers_or_safe_insufficient_evidence() -> None:
    result = _matching_result()
    matrix._assert_assistant_message(
        {
            "role": "assistant",
            "answer_scope": "case_grounded",
            "content": "원산지증명서에 HS 코드가 기재된 사실을 현재 심사 결과에서 확인했습니다.",
            "citations": [{"kind": "fact"}],
            "suggested_questions": ["문서 간 HS 코드 대응 결과를 확인해 볼까요?"],
        },
        expected_scope="case_grounded",
        run=result,
    )
    matrix._assert_assistant_message(
        {
            "role": "assistant",
            "answer_scope": "insufficient_evidence",
            "content": "현재 심사 입력에는 수입신고확인증의 제출 또는 제출 예정 사실이 없어 확인할 수 없습니다.",
            "citations": [],
            "suggested_questions": [],
        },
        expected_scope="insufficient_evidence",
        run=result,
        required_terms=("제출", "예정", "확인"),
    )
    matrix._assert_assistant_message(
        {
            "role": "assistant",
            "answer_scope": "general_customs_guidance",
            "content": "공식 안내 범위에서 원산지 증명서 확인 절차를 안내합니다.",
            "citations": [_official_guidance_citation()],
            "suggested_questions": [],
        },
        expected_scope="general_customs_guidance",
        run=result,
    )


def test_assistant_user_text_rejects_raw_identifier() -> None:
    with pytest.raises(matrix.MatrixError, match="internal_(?:label|identifier)"):
        matrix._assert_assistant_message(
            {
                "role": "assistant",
                "answer_scope": "case_grounded",
                "content": "finding-food-import 항목을 확인해 주세요.",
                "citations": [{"kind": "fact"}],
                "suggested_questions": [],
            },
            expected_scope="case_grounded",
            run=_matching_result(),
        )


def _official_guidance_citation() -> dict[str, str]:
    return {
        "kind": "guidance_evidence",
        "source_id": "guide-official-001",
        "source_kind": "official_agency_guidance",
        "source_title": "관세청 수출입 통관 공개 안내",
        "source_uri": "https://www.customs.go.kr/guide/export",
        "as_of": "2026-08-05",
        "snapshot_id": "kr-trade-snapshot-20260805",
        "coverage": "covered",
    }


def _v2_guidance_resolution(
    *,
    topic: str,
    scope: str,
    requested_direction: str = "export",
    applicable_directions: list[str] | None = None,
    coverage: str = "covered",
    snapshot_id: str | None = "kr-trade-v2-snapshot-20260805",
) -> dict[str, Any]:
    return {
        "topic": topic,
        "scope": scope,
        "requested_direction": requested_direction,
        "applicable_directions": applicable_directions or [requested_direction],
        "coverage": coverage,
        "snapshot_id": snapshot_id,
        "as_of": "2026-08-05",
        "registry_version": "2.0",
    }


def _v2_guidance_citation(
    *,
    source_id: str,
    source_title: str,
    source_uri: str,
) -> dict[str, str]:
    return {
        "kind": "guidance_evidence",
        "source_id": source_id,
        "source_kind": "official_agency_guidance",
        "source_title": source_title,
        "source_uri": source_uri,
        "as_of": "2026-08-05",
        "snapshot_id": "kr-trade-v2-snapshot-20260805",
        "coverage": "covered",
    }


@pytest.mark.parametrize(
    "source_uri",
    [
        "http://www.customs.go.kr/guide/export",
        "https://untrusted.example/guide/export",
        "https://official@www.customs.go.kr/guide/export",
        "https://www.customs.go.kr:443/guide/export",
        "https://www.customs.go.kr:/guide/export",
    ],
)
def test_official_guidance_citation_rejects_noncanonical_navigation_uri(source_uri: str) -> None:
    citation = _official_guidance_citation()
    citation["source_uri"] = source_uri

    with pytest.raises(matrix.MatrixError, match="knowledge_source_uri"):
        matrix._assert_official_guidance_citations(
            [citation], expected_as_of="2026-08-05", code="knowledge"
        )


def test_mixed_assistant_answer_keeps_case_and_official_guidance_sections_separate() -> None:
    guidance = _official_guidance_citation()
    matrix._assert_mixed_assistant_sections(
        {
            "role": "assistant",
            "answer_scope": "case_grounded",
            "content": "현재 사건의 문서 비교 결과를 바탕으로 우선 확인할 항목을 정리했습니다.",
            "citations": [{"kind": "comparison"}, guidance],
            "suggested_questions": [],
            "answer_sections": [
                {
                    "kind": "case_analysis",
                    "title": "현재 사건 기준",
                    "content": "현재 사건의 문서 비교 결과를 바탕으로 우선 확인할 항목을 정리했습니다.",
                    "citations": [{"kind": "comparison"}],
                },
                {
                    "kind": "general_guidance",
                    "title": "일반 통관 안내",
                    "content": "공식 통관 안내를 바탕으로 기본 서류와 신고 절차를 함께 확인해야 합니다.",
                    "citations": [guidance],
                },
            ],
        },
        run=_matching_result(),
    )


def test_case_application_requires_v2_resolution_and_separated_citations() -> None:
    guidance = _v2_guidance_citation(
        source_id="record-v2-import-clearance",
        source_title="수입 통관",
        source_uri=(
            "https://www.customs.go.kr/kcs/cm/cntnts/cntntsView.do?cntntsId=817&mi=2819"
        ),
    )
    resolution = _v2_guidance_resolution(
        topic="customs_clearance",
        scope="import_clearance_flow",
        requested_direction="import",
        applicable_directions=["import"],
    )
    matrix._assert_case_application_sections(
        {
            "role": "assistant",
            "answer_scope": "case_grounded",
            "content": "현재 사건의 수량 비교와 일반 수입 통관 안내를 구분해 다음 확인 항목을 정리했습니다.",
            "citations": [{"kind": "comparison"}, guidance],
            "suggested_questions": [],
            "guidance_resolution": resolution,
            "answer_sections": [
                {
                    "kind": "case_analysis",
                    "title": "현재 사건 기준",
                    "content": "현재 심사 결과에서는 수량 비교 항목을 먼저 확인해야 합니다.",
                    "citations": [{"kind": "comparison"}],
                },
                {
                    "kind": "general_guidance",
                    "title": "일반 통관 안내",
                    "content": "공식 수입 통관 안내의 절차 범위를 확인할 수 있습니다.",
                    "citations": [guidance],
                },
                {
                    "kind": "case_application",
                    "title": "사건 적용·다음 확인",
                    "content": "현재 사건에서는 수량 비교를 유지한 채 필요한 통관 절차 문서를 추가로 확인해 주세요.",
                    "citations": [{"kind": "comparison"}, guidance],
                },
            ],
        },
        run=_matching_result(),
    )


def test_v2_guidance_uri_must_match_the_exact_manifest_page() -> None:
    citation = _v2_guidance_citation(
        source_id="record-v2-import-clearance",
        source_title="수입 통관",
        source_uri=(
            "https://www.customs.go.kr/kcs/cm/cntnts/cntntsView.do?cntntsId=817&mi=2819"
        ),
    )
    resolution = _v2_guidance_resolution(
        topic="customs_clearance",
        scope="import_clearance_flow",
        requested_direction="import",
        applicable_directions=["import"],
    )
    matrix._assert_official_guidance_citations(
        [citation],
        expected_as_of="2026-08-05",
        code="v2_guidance",
        guidance_resolution=resolution,
    )
    citation["source_uri"] += "&unregistered=1"
    with pytest.raises(matrix.MatrixError, match="v2_guidance_registry_source"):
        matrix._assert_official_guidance_citations(
            [citation],
            expected_as_of="2026-08-05",
            code="v2_guidance",
            guidance_resolution=resolution,
        )


def test_export_matrix_scenario_requires_agent_export_document_facts_and_comparisons() -> None:
    scenario = next(
        item
        for item in matrix._scenario_matrix()
        if item.name == "export-documents-and-submission-evidence"
    )
    assert scenario.expected_execution == "partial"
    assert scenario.expected_retrieval_status == "needs_review"
    result = {
        "trade_type": "export",
        "workflow": {
            "stages": [
                {
                    "stage": "retrieve_evidence",
                    "status": "needs_review",
                }
            ]
        },
        "document_analyses": [
            {"document_type": "invoice", "facts": [], "line_items": []},
            {"document_type": "packing_list", "facts": [], "line_items": []},
            {"document_type": "certificate_of_origin", "facts": [], "line_items": []},
            {"document_type": "air_waybill", "facts": [], "line_items": []},
            {
                "document_type": "export_declaration",
                "facts": [
                    {"name": "export_declaration_no"},
                    {"name": "destination_country"},
                    {"name": "submission_status"},
                ],
                "line_items": [],
            },
            {
                "document_type": "supporting_evidence",
                "facts": [{"name": "submission_receipt_no"}],
                "line_items": [],
            },
        ],
        "comparisons": [{"rule_code": code} for code in scenario.required_comparison_codes],
        "findings": [],
    }

    matrix._assert_scenario_semantics(result, scenario)

    result["comparisons"] = result["comparisons"][1:]
    with pytest.raises(matrix.MatrixError, match="comparison_codes"):
        matrix._assert_scenario_semantics(result, scenario)


def test_standalone_knowledge_matrix_receipt_pins_official_snapshot_without_case_values(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[tuple[str, str, dict[str, Any] | None]] = []
    verified: list[Path] = []
    provider_traces: list[Path] = []
    no_coverage_traces: list[Path] = []
    export_guidance = _v2_guidance_citation(
        source_id="record-v2-export-requirements",
        source_title="수출 통관",
        source_uri=(
            "https://www2.customs.go.kr/kcs/cm/cntnts/cntntsView.do?cntntsId=818&mi=2820"
        ),
    )
    fta_guidance = _v2_guidance_citation(
        source_id="record-v2-fta-procedure",
        source_title="원산지증명서 개요",
        source_uri=(
            "https://www.customs.go.kr/ftaportalkor/cm/cntnts/cntntsView.do?cntntsId=1061&mi=3401"
        ),
    )
    knowledge_turn_count = 0

    class FakeApi:
        def require(
            self,
            method: str,
            path: str,
            *,
            body: dict[str, Any] | None = None,
            **_kwargs: Any,
        ) -> dict[str, Any]:
            calls.append((method, path, body))
            if method == "POST" and path == "/knowledge/conversations":
                return {
                    "conversation_id": "knowledge-conversation-1",
                    "as_of": "2026-08-05",
                }
            if (
                method == "POST"
                and path.endswith("/messages")
                and body
                and "filename:" in str(body.get("content", ""))
            ):
                assert _kwargs.get("expected") == 422
                return {"code": "knowledge_input_not_allowed"}
            if method == "POST" and path.endswith("/messages"):
                nonlocal knowledge_turn_count
                knowledge_turn_count += 1
                if knowledge_turn_count == 3:
                    return {
                        "assistant_message": {
                            "role": "assistant",
                            "answer_scope": "insufficient_evidence",
                            "content": "현재 공식 지식 스냅샷에서 확인할 수 없습니다.",
                            "citations": [],
                            "answer_sections": [],
                            "knowledge_coverage": "no_coverage",
                            "knowledge_as_of": "2026-08-05",
                            "guidance_resolution": _v2_guidance_resolution(
                                topic="customs_duty",
                                scope="customs_value_basics",
                                requested_direction="export",
                                applicable_directions=["import"],
                                coverage="no_coverage",
                                snapshot_id=None,
                            ),
                        }
                    }
                guidance = export_guidance if knowledge_turn_count == 1 else fta_guidance
                resolution = (
                    _v2_guidance_resolution(
                        topic="export_requirements",
                        scope="agency_confirmation_and_supporting_documents",
                    )
                    if knowledge_turn_count == 1
                    else _v2_guidance_resolution(
                        topic="fta_procedure",
                        scope="preference_procedure",
                        applicable_directions=["import", "export"],
                    )
                )
                return {
                    "assistant_message": {
                        "role": "assistant",
                        "answer_scope": "general_customs_guidance",
                        "content": "공식 안내를 바탕으로 수출 신고 전 기본 서류와 절차를 확인해야 합니다.",
                        "citations": [guidance],
                        "answer_sections": [{"kind": "general_guidance", "citations": [guidance]}],
                        "knowledge_coverage": "covered",
                        "knowledge_snapshot_id": "kr-trade-v2-snapshot-20260805",
                        "knowledge_as_of": "2026-08-05",
                        "guidance_resolution": resolution,
                    }
                }
            if method == "GET" and path.endswith("/messages"):
                return {
                    "items": [
                        {"role": "user"},
                        {
                            "role": "assistant",
                            "knowledge_snapshot_id": "kr-trade-v2-snapshot-20260805",
                        },
                        {"role": "user"},
                        {
                            "role": "assistant",
                            "knowledge_snapshot_id": "kr-trade-v2-snapshot-20260805",
                        },
                        {"role": "user"},
                        {"role": "assistant"},
                    ]
                }
            if method == "GET" and path == "/knowledge/conversations":
                return {
                    "items": [
                        {
                            "conversation_id": "knowledge-conversation-1",
                            "snapshot_id": "kr-trade-v2-snapshot-20260805",
                        }
                    ]
                }
            raise AssertionError(f"unexpected request: {method} {path}")

    monkeypatch.setattr(
        matrix,
        "_knowledge_turn_directories",
        lambda _conversation_id, *, expected_count: (
            [
                Path("turn-" + "c" * 32),
                Path("turn-" + "a" * 32),
                Path("turn-" + "b" * 32),
            ]
            if expected_count == 3
            else (_ for _ in ()).throw(AssertionError("unexpected turn count"))
        ),
    )
    monkeypatch.setattr(
        matrix,
        "_assert_knowledge_provider_not_called_for_no_coverage",
        lambda turn_directory: no_coverage_traces.append(turn_directory) or 1,
    )
    monkeypatch.setattr(
        matrix,
        "_verify_agent_knowledge_transcript",
        lambda turn_directory: verified.append(turn_directory),
    )
    monkeypatch.setattr(
        matrix,
        "_assert_real_knowledge_provider_trace",
        lambda turn_directory: (
            (_ for _ in ()).throw(matrix.MatrixError("knowledge_claude_provider_answer_unaccepted"))
            if turn_directory.name == "turn-" + "c" * 32
            else (
                provider_traces.append(turn_directory)
                or {
                    "knowledge_provider": "anthropic",
                    "knowledge_claude_event_count": 2,
                    "knowledge_provider_attempt_count": 1,
                    "knowledge_provider_accepted_count": 1,
                    "knowledge_provider_safe_fallback_count": 0,
                }
            )
        ),
    )

    receipt = matrix._standalone_knowledge_conversation(FakeApi())

    assert receipt == {
        "status": "passed",
        "official_guidance_citations_verified": 2,
        "snapshot_pinned": True,
        "agent_knowledge_transcript_verified": True,
        "knowledge_turns_verified": 3,
        "knowledge_recent_history_verified": 1,
        "knowledge_no_coverage_verified": 1,
        "knowledge_no_coverage_not_called_count": 1,
        "knowledge_rejected_inputs_verified": 1,
        "knowledge_rejected_agent_dispatches": 0,
        "knowledge_rejected_exchanges": 0,
        "knowledge_provider": "anthropic",
        "knowledge_claude_event_count": 4,
        "knowledge_provider_attempt_count": 2,
        "knowledge_provider_accepted_count": 2,
        "knowledge_provider_safe_fallback_count": 0,
    }
    assert "knowledge-conversation-1" not in repr(receipt)
    assert "수출 요건을 공식 자료 범위에서 알려 주세요." not in repr(receipt)
    assert verified == [
        Path("turn-" + "c" * 32),
        Path("turn-" + "a" * 32),
        Path("turn-" + "b" * 32),
    ]
    assert provider_traces == [Path("turn-" + "a" * 32), Path("turn-" + "b" * 32)]
    assert no_coverage_traces == [Path("turn-" + "c" * 32)]
    assert calls[0] == (
        "POST",
        "/knowledge/conversations",
        {"title": "수출 신고 기본 안내", "trade_type": "export", "as_of": "2026-08-05"},
    )
    assert [
        body["content"]
        for method, path, body in calls
        if method == "POST"
        and path.endswith("/messages")
        and body
        and "filename:" not in body["content"]
    ] == [
        "수출 요건을 공식 자료 범위에서 알려 주세요.",
        "FTA 특혜 적용 절차도 공식 자료 범위에서 알려 주세요.",
        "수출 과세가격의 기본을 공식 자료 범위에서 알려 주세요.",
    ]


def test_agent_knowledge_transcript_verifier_runs_with_safe_result_only(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    turn = tmp_path / ("turn-" + "a" * 32)
    turn.mkdir()
    (tmp_path / "customs-ai-agent").mkdir()
    invoked: list[dict[str, Any]] = []

    monkeypatch.setattr(matrix, "_workspace_root", lambda: tmp_path)

    def run(command: list[str], **kwargs: Any) -> SimpleNamespace:
        invoked.append({"command": command, **kwargs})
        return SimpleNamespace(returncode=0, stdout='{"status":"passed"}')

    monkeypatch.setattr(matrix.subprocess, "run", run)

    matrix._verify_agent_knowledge_transcript(turn)

    assert invoked[0]["command"][-3:] == [
        "functional-uat",
        "--turn-directory",
        str(turn),
    ]
    assert invoked[0]["cwd"] == tmp_path / "customs-ai-agent"
    assert invoked[0]["encoding"] == "utf-8"


def test_matrix_run_includes_export_mixed_and_safe_standalone_knowledge_receipt(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    scenarios = matrix._scenario_matrix()
    created: list[str] = []
    mixed: list[str] = []
    document_paste_runs: list[str] = []
    standalone_calls: list[bool] = []

    class FakeOidc:
        def __init__(self, *_args: Any, **_kwargs: Any) -> None:
            return None

        def access_token(self, actor: str) -> str:
            return f"token-{actor}"

    class FakeApi:
        def __init__(self, **_kwargs: Any) -> None:
            return None

        def close(self) -> None:
            return None

    def create_case(
        _api: object, scenario: matrix.Scenario
    ) -> tuple[str, list[dict[str, str]], str, dict[str, Any]]:
        created.append(scenario.name)
        return (
            f"case-{scenario.name}",
            [{"document_id": f"document-{scenario.name}", "document_type": "CI"}],
            f"run-{scenario.name}",
            {},
        )

    monkeypatch.setattr(
        matrix.httpx, "get", lambda *_args, **_kwargs: SimpleNamespace(status_code=200)
    )
    monkeypatch.setattr(matrix, "_load_profile", lambda _path: {})
    monkeypatch.setattr(matrix, "FunctionalUatOidc", FakeOidc)
    monkeypatch.setattr(matrix, "PublicApi", FakeApi)
    monkeypatch.setattr(matrix, "_create_case_and_review", create_case)
    monkeypatch.setattr(matrix, "_conversation", lambda *_args, **_kwargs: "conversation")
    monkeypatch.setattr(
        matrix,
        "_assert_case_document_paste_zero_dispatch",
        lambda _api, *, run_id, run: (
            document_paste_runs.append(run_id)
            or {
                "status": "passed",
                "case_document_paste_notice_verified": True,
                "case_document_paste_agent_http_exchanges": 0,
                "case_document_paste_public_transcript_exchanges": 0,
                "case_document_paste_agent_data_dispatches": 0,
                "case_document_paste_claude_dispatches": 0,
                "case_document_paste_fact_changes": 0,
                "case_document_paste_finding_changes": 0,
                "case_document_paste_correction_changes": 0,
            }
        ),
    )
    monkeypatch.setattr(
        matrix,
        "_mixed_conversation",
        lambda _api, *, run_id, run: mixed.append(run_id),
    )
    monkeypatch.setattr(
        matrix,
        "_standalone_knowledge_conversation",
        lambda _api: (
            standalone_calls.append(True)
            or {
                "status": "passed",
                "official_guidance_citations_verified": 1,
                "snapshot_pinned": True,
                "agent_knowledge_transcript_verified": True,
                "knowledge_provider": "anthropic",
                "knowledge_claude_event_count": 2,
                "knowledge_provider_accepted_count": 1,
            }
        ),
    )
    monkeypatch.setattr(matrix, "_assert_recent_context_limit", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(
        matrix,
        "_assert_human_correction_flow",
        lambda *_args, **_kwargs: ("run-child", "run-replacement"),
    )
    monkeypatch.setattr(
        matrix,
        "_verify_transcript",
        lambda run_id: {"review_run_id": run_id, "status": "completed"},
    )
    monkeypatch.setattr(
        matrix,
        "_assert_real_provider_trace",
        lambda _run_ids: {"provider": "anthropic", "claude_event_count": 1},
    )

    result = matrix.run(
        base_url="http://127.0.0.1:18081/api/v1",
        profile_path=tmp_path / "backend.env",
        redirect_uri="http://127.0.0.1:5173/",
    )

    assert result["scenario_count"] == 7
    assert result["review_run_count"] == 9
    assert result["guidance_profile"] == "functional-uat-kr-trade-v2"
    assert created == [scenario.name for scenario in scenarios]
    assert "run-all-documents-match" in mixed
    assert document_paste_runs == ["run-all-documents-match"]
    assert result["case_document_paste"] == {
        "status": "passed",
        "case_document_paste_notice_verified": True,
        "case_document_paste_agent_http_exchanges": 0,
        "case_document_paste_public_transcript_exchanges": 0,
        "case_document_paste_agent_data_dispatches": 0,
        "case_document_paste_claude_dispatches": 0,
        "case_document_paste_fact_changes": 0,
        "case_document_paste_finding_changes": 0,
        "case_document_paste_correction_changes": 0,
    }
    assert standalone_calls == [True]
    assert result["knowledge_guidance"] == {
        "status": "passed",
        "official_guidance_citations_verified": 1,
        "snapshot_pinned": True,
        "agent_knowledge_transcript_verified": True,
        "knowledge_provider": "anthropic",
        "knowledge_claude_event_count": 2,
        "knowledge_provider_accepted_count": 1,
    }


def _provider_trace(
    *,
    report_accepted: bool,
    assistant_accepted: bool,
    recent_turn_count: int = 8,
) -> list[dict[str, Any]]:
    provider = {"provider": "anthropic", "model": "claude-test"}
    return [
        {"event": "review_report_request_started", "payload": provider},
        {"event": "review_report_response_received", "payload": provider},
        *(
            []
            if report_accepted
            else [
                {
                    "event": "review_report_fallback",
                    "payload": {"reason_code": "provider_report_unavailable_or_ungrounded"},
                }
            ]
        ),
        {
            "event": "review_report_generated",
            "payload": {"source": "claude" if report_accepted else "deterministic"},
        },
        {
            "event": "assistant_answer_request_started",
            "payload": {
                **provider,
                "messages": [
                    {
                        "content": json.dumps(
                            {"recent_turns": [{} for _ in range(recent_turn_count)]}
                        )
                    }
                ],
            },
        },
        {"event": "assistant_answer_response_received", "payload": provider},
        *(
            []
            if assistant_accepted
            else [
                {
                    "event": "assistant_answer_fallback",
                    "payload": {"reason_code": "provider_answer_unavailable_or_ungrounded"},
                }
            ]
        ),
    ]


def _write_provider_trace(tmp_path: Path, run_id: str, events: list[dict[str, Any]]) -> None:
    directory = tmp_path / run_id
    directory.mkdir()
    (directory / "claude.jsonl").write_text(
        "\n".join(json.dumps(event, ensure_ascii=False) for event in events) + "\n",
        encoding="utf-8",
    )


def _write_knowledge_provider_trace(tmp_path: Path, events: list[dict[str, Any]]) -> Path:
    turn = tmp_path / "knowledge" / "knowledge-conversation" / ("turn-" + "a" * 32)
    turn.mkdir(parents=True)
    (turn / "claude.jsonl").write_text(
        "\n".join(json.dumps(event, ensure_ascii=False) for event in events) + "\n",
        encoding="utf-8",
    )
    return turn


def _knowledge_provider_trace(*, model: str = "claude-test") -> list[dict[str, Any]]:
    return [
        {
            "event": "knowledge_answer_request_started",
            "payload": {
                "provider": "anthropic",
                "purpose": "knowledge_answer",
                "model": model,
            },
        },
        {
            "event": "knowledge_answer_response_received",
            "payload": {
                "provider": "anthropic",
                "model": model,
                "output_text": "안전한 응답",
            },
        },
    ]


def _knowledge_no_coverage_trace() -> list[dict[str, Any]]:
    return [
        {
            "event": "knowledge_answer_not_called",
            "payload": {
                "provider": "claude",
                "called": False,
                "reason_code": "official_guidance_coverage_unavailable",
            },
        }
    ]


def test_real_knowledge_provider_trace_records_an_accepted_anthropic_answer_in_same_turn(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    turn = _write_knowledge_provider_trace(tmp_path, _knowledge_provider_trace())

    receipt = matrix._assert_real_knowledge_provider_trace(turn)

    assert receipt == {
        "knowledge_provider": "anthropic",
        "knowledge_claude_event_count": 2,
        "knowledge_provider_attempt_count": 1,
        "knowledge_provider_accepted_count": 1,
        "knowledge_provider_safe_fallback_count": 0,
    }
    assert "knowledge-conversation" not in repr(receipt)
    assert "안전한 응답" not in repr(receipt)


def test_knowledge_no_coverage_trace_requires_explicit_safe_non_call(tmp_path: Path) -> None:
    turn = _write_knowledge_provider_trace(tmp_path, _knowledge_no_coverage_trace())

    assert matrix._assert_knowledge_provider_not_called_for_no_coverage(turn) == 1


@pytest.mark.parametrize(
    "event",
    [
        {"event": "knowledge_answer_fallback", "payload": {}},
        {
            "event": "knowledge_answer_not_called",
            "payload": {
                "provider": "claude",
                "called": False,
                "reason_code": "another_reason",
            },
        },
    ],
)
def test_knowledge_no_coverage_trace_rejects_any_provider_or_reason_deviation(
    tmp_path: Path, event: dict[str, Any]
) -> None:
    turn = _write_knowledge_provider_trace(tmp_path, [event])

    with pytest.raises(matrix.MatrixError, match="knowledge_no_coverage_claude"):
        matrix._assert_knowledge_provider_not_called_for_no_coverage(turn)


@pytest.mark.parametrize(
    "events",
    [
        _knowledge_provider_trace()
        + [
            {
                "event": "knowledge_answer_fallback",
                "payload": {"reason_code": "provider_answer_unavailable_or_ungrounded"},
            }
        ],
        [
            _knowledge_provider_trace()[0],
            {
                "event": "knowledge_answer_request_failed",
                "payload": {
                    "provider": "anthropic",
                    "model": "claude-test",
                    "error_code": "provider_request_failed",
                },
            },
            {
                "event": "knowledge_answer_fallback",
                "payload": {"reason_code": "provider_answer_unavailable_or_ungrounded"},
            },
        ],
        _knowledge_provider_trace()
        + [
            {
                "event": "knowledge_answer_response_invalid",
                "payload": {
                    "provider": "anthropic",
                    "model": "claude-test",
                    "error_code": "provider_output_invalid",
                },
            },
            {
                "event": "knowledge_answer_fallback",
                "payload": {"reason_code": "provider_answer_unavailable_or_ungrounded"},
            },
        ],
    ],
)
def test_real_knowledge_provider_trace_classifies_documented_safe_fallbacks(
    tmp_path: Path, events: list[dict[str, Any]]
) -> None:
    turn = _write_knowledge_provider_trace(tmp_path, events)

    receipt = matrix._assert_real_knowledge_provider_trace(turn)

    assert receipt == {
        "knowledge_provider": "anthropic",
        "knowledge_claude_event_count": len(events),
        "knowledge_provider_attempt_count": 1,
        "knowledge_provider_accepted_count": 0,
        "knowledge_provider_safe_fallback_count": 1,
    }


@pytest.mark.parametrize(
    ("events", "error"),
    [
        (
            [
                _knowledge_provider_trace()[0],
                {
                    "event": "knowledge_answer_request_failed",
                    "payload": {
                        "provider": "anthropic",
                        "model": "claude-test",
                        "error_code": "provider_request_failed",
                    },
                },
            ],
            "knowledge_claude_provider_sequence_invalid",
        ),
        (
            _knowledge_provider_trace()
            + [
                {
                    "event": "knowledge_answer_response_invalid",
                    "payload": {
                        "provider": "anthropic",
                        "model": "claude-test",
                        "error_code": "provider_output_invalid",
                    },
                }
            ],
            "knowledge_claude_provider_sequence_invalid",
        ),
        (
            _knowledge_provider_trace()
            + [
                {
                    "event": "knowledge_answer_fallback",
                    "payload": {"reason_code": "wrong_reason"},
                }
            ],
            "knowledge_claude_fallback_metadata_invalid",
        ),
        (
            _knowledge_provider_trace()
            + [
                {
                    "event": "knowledge_answer_not_called",
                    "payload": {
                        "provider": "claude",
                        "called": False,
                        "reason_code": "official_guidance_coverage_unavailable",
                    },
                }
            ],
            "knowledge_claude_provider_answer_unaccepted",
        ),
    ],
)
def test_real_knowledge_provider_trace_rejects_incomplete_or_invalid_terminal_sequences(
    tmp_path: Path, events: list[dict[str, Any]], error: str
) -> None:
    turn = _write_knowledge_provider_trace(tmp_path, events)

    with pytest.raises(matrix.MatrixError, match=error):
        matrix._assert_real_knowledge_provider_trace(turn)


def test_knowledge_provider_turns_do_not_count_safe_fallback_as_accepted_claude(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    turns = [
        Path("turn-" + "c" * 32),
        Path("turn-" + "a" * 32),
        Path("turn-" + "b" * 32),
    ]
    monkeypatch.setattr(matrix, "_verify_agent_knowledge_transcript", lambda _turn: None)
    monkeypatch.setattr(
        matrix,
        "_assert_knowledge_provider_not_called_for_no_coverage",
        lambda _turn: 1,
    )

    def classify(turn: Path) -> dict[str, Any]:
        if turn.name == "turn-" + "c" * 32:
            raise matrix.MatrixError("knowledge_claude_provider_answer_unaccepted")
        return {
            "knowledge_provider": "anthropic",
            "knowledge_claude_event_count": 3,
            "knowledge_provider_attempt_count": 1,
            "knowledge_provider_accepted_count": 0,
            "knowledge_provider_safe_fallback_count": 1,
        }

    monkeypatch.setattr(matrix, "_assert_real_knowledge_provider_trace", classify)

    with pytest.raises(matrix.MatrixError, match="knowledge_claude_provider_answer_unaccepted"):
        matrix._assert_knowledge_provider_turns(turns)


@pytest.mark.parametrize(
    "mutator",
    [
        lambda events: events[0]["payload"].update({"model": ""}),
        lambda events: events[1]["payload"].update({"model": "claude-other"}),
        lambda events: events[1]["payload"].update({"output_text": ""}),
    ],
)
def test_real_knowledge_provider_trace_rejects_malformed_or_mismatched_model_pair(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    mutator: Any,
) -> None:
    events = _knowledge_provider_trace()
    mutator(events)
    turn = _write_knowledge_provider_trace(tmp_path, events)

    with pytest.raises(matrix.MatrixError, match="knowledge_claude_provider_payload_invalid"):
        matrix._assert_real_knowledge_provider_trace(turn)


def test_real_provider_trace_accepts_grounded_results_with_safe_fallbacks(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _write_provider_trace(
        tmp_path,
        "run-accepted",
        _provider_trace(report_accepted=True, assistant_accepted=True),
    )
    _write_provider_trace(
        tmp_path,
        "run-safe-fallback",
        _provider_trace(report_accepted=False, assistant_accepted=False, recent_turn_count=1),
    )
    monkeypatch.setattr(matrix, "workspace_transcript_root", lambda: tmp_path)

    receipt = matrix._assert_real_provider_trace(["run-accepted", "run-safe-fallback"])

    assert receipt == {
        "provider": "anthropic",
        "claude_event_count": 12,
        "max_recent_turns": 8,
        "provider_report_accepted_count": 1,
        "provider_report_fallback_count": 1,
        "provider_assistant_accepted_count": 1,
        "provider_assistant_fallback_count": 1,
    }


@pytest.mark.parametrize(
    ("report_accepted", "assistant_accepted", "error"),
    [
        (False, True, "claude_provider_report_unaccepted"),
        (True, False, "claude_provider_assistant_unaccepted"),
    ],
)
def test_real_provider_trace_requires_an_accepted_report_and_assistant_answer(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    report_accepted: bool,
    assistant_accepted: bool,
    error: str,
) -> None:
    _write_provider_trace(
        tmp_path,
        "run-only-fallback",
        _provider_trace(report_accepted=report_accepted, assistant_accepted=assistant_accepted),
    )
    monkeypatch.setattr(matrix, "workspace_transcript_root", lambda: tmp_path)

    with pytest.raises(matrix.MatrixError, match=error):
        matrix._assert_real_provider_trace(["run-only-fallback"])


def test_real_provider_trace_rejects_unsafe_fallback_metadata(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    events = _provider_trace(report_accepted=False, assistant_accepted=True)
    fallback = next(event for event in events if event["event"] == "review_report_fallback")
    fallback["payload"] = {
        "reason_code": "provider_report_unavailable_or_ungrounded",
        "unexpected": "not-safe",
    }
    _write_provider_trace(tmp_path, "run-unsafe-fallback", events)
    monkeypatch.setattr(matrix, "workspace_transcript_root", lambda: tmp_path)

    with pytest.raises(matrix.MatrixError, match="claude_fallback_metadata_invalid"):
        matrix._assert_real_provider_trace(["run-unsafe-fallback"])


def test_real_provider_trace_rejects_fallback_from_a_different_run(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _write_provider_trace(
        tmp_path,
        "run-with-provider-answer",
        _provider_trace(report_accepted=True, assistant_accepted=True),
    )
    unmatched_events = [
        event
        for event in _provider_trace(report_accepted=True, assistant_accepted=True)
        if not event["event"].startswith("assistant_answer_")
    ]
    unmatched_events.append(
        {
            "event": "assistant_answer_fallback",
            "payload": {"reason_code": "provider_answer_unavailable_or_ungrounded"},
        }
    )
    _write_provider_trace(tmp_path, "run-with-unmatched-fallback", unmatched_events)
    monkeypatch.setattr(matrix, "workspace_transcript_root", lambda: tmp_path)

    with pytest.raises(matrix.MatrixError, match="claude_provider_sequence_invalid"):
        matrix._assert_real_provider_trace(
            ["run-with-provider-answer", "run-with-unmatched-fallback"]
        )


@pytest.mark.parametrize(
    "event_name",
    ["review_report_request_started", "assistant_answer_response_received"],
)
def test_real_provider_trace_requires_anthropic_provider_and_model_on_each_call_event(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, event_name: str
) -> None:
    events = _provider_trace(report_accepted=True, assistant_accepted=True)
    target = next(event for event in events if event["event"] == event_name)
    target["payload"] = {"provider": "anthropic", "model": ""}
    _write_provider_trace(tmp_path, "run-provider-payload", events)
    monkeypatch.setattr(matrix, "workspace_transcript_root", lambda: tmp_path)

    with pytest.raises(matrix.MatrixError, match="claude_provider_payload_invalid"):
        matrix._assert_real_provider_trace(["run-provider-payload"])


def test_real_provider_trace_keeps_provider_request_and_json_failures_fatal(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    events = _provider_trace(report_accepted=True, assistant_accepted=True)
    events.append(
        {
            "event": "assistant_answer_response_invalid",
            "payload": {
                "provider": "anthropic",
                "model": "claude-test",
                "error_code": "provider_output_invalid",
            },
        }
    )
    _write_provider_trace(tmp_path, "run-provider-failure", events)
    monkeypatch.setattr(matrix, "workspace_transcript_root", lambda: tmp_path)

    with pytest.raises(matrix.MatrixError, match="claude_provider_request_or_json_failed"):
        matrix._assert_real_provider_trace(["run-provider-failure"])
