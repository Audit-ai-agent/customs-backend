from __future__ import annotations

from copy import deepcopy
import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from tools import run_functional_uat_matrix as matrix


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
    guidance = _official_guidance_citation()

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
                return {
                    "assistant_message": {
                        "role": "assistant",
                        "answer_scope": "general_customs_guidance",
                        "content": "공식 안내를 바탕으로 수출 신고 전 기본 서류와 절차를 확인해야 합니다.",
                        "citations": [guidance],
                        "answer_sections": [{"kind": "general_guidance", "citations": [guidance]}],
                        "knowledge_coverage": "covered",
                        "knowledge_snapshot_id": "kr-trade-snapshot-20260805",
                        "knowledge_as_of": "2026-08-05",
                    }
                }
            if method == "GET" and path.endswith("/messages"):
                return {
                    "items": [
                        {"role": "user"},
                        {
                            "role": "assistant",
                            "knowledge_snapshot_id": "kr-trade-snapshot-20260805",
                        },
                        {"role": "user"},
                        {
                            "role": "assistant",
                            "knowledge_snapshot_id": "kr-trade-snapshot-20260805",
                        },
                    ]
                }
            if method == "GET" and path == "/knowledge/conversations":
                return {
                    "items": [
                        {
                            "conversation_id": "knowledge-conversation-1",
                            "snapshot_id": "kr-trade-snapshot-20260805",
                        }
                    ]
                }
            raise AssertionError(f"unexpected request: {method} {path}")

    monkeypatch.setattr(
        matrix,
        "_knowledge_turn_directories",
        lambda _conversation_id, *, expected_count: (
            [
                Path("turn-" + "a" * 32),
                Path("turn-" + "b" * 32),
            ]
            if expected_count == 2
            else (_ for _ in ()).throw(AssertionError("unexpected turn count"))
        ),
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
            provider_traces.append(turn_directory)
            or {
                "knowledge_provider": "anthropic",
                "knowledge_claude_event_count": 2,
                "knowledge_provider_accepted_count": 1,
            }
        ),
    )

    receipt = matrix._standalone_knowledge_conversation(FakeApi())

    assert receipt == {
        "status": "passed",
        "official_guidance_citations_verified": 2,
        "snapshot_pinned": True,
        "agent_knowledge_transcript_verified": True,
        "knowledge_turns_verified": 2,
        "knowledge_recent_history_verified": 1,
        "knowledge_rejected_inputs_verified": 1,
        "knowledge_rejected_agent_dispatches": 0,
        "knowledge_rejected_exchanges": 0,
        "knowledge_provider": "anthropic",
        "knowledge_claude_event_count": 4,
        "knowledge_provider_accepted_count": 2,
    }
    assert "knowledge-conversation-1" not in repr(receipt)
    assert "수출 신고 전에 필요한 기본 서류와 절차를 알려 주세요." not in repr(receipt)
    assert verified == [Path("turn-" + "a" * 32), Path("turn-" + "b" * 32)]
    assert provider_traces == [Path("turn-" + "a" * 32), Path("turn-" + "b" * 32)]
    assert calls[0] == (
        "POST",
        "/knowledge/conversations",
        {"title": "수출 신고 기본 안내", "trade_type": "export", "as_of": "2026-08-05"},
    )


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


def test_matrix_run_includes_export_mixed_and_safe_standalone_knowledge_receipt(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    scenarios = matrix._scenario_matrix()
    created: list[str] = []
    mixed: list[str] = []
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
    assert created == [scenario.name for scenario in scenarios]
    assert "run-all-documents-match" in mixed
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


def test_real_knowledge_provider_trace_requires_accepted_anthropic_answer_in_same_turn(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    turn = _write_knowledge_provider_trace(tmp_path, _knowledge_provider_trace())

    receipt = matrix._assert_real_knowledge_provider_trace(turn)

    assert receipt == {
        "knowledge_provider": "anthropic",
        "knowledge_claude_event_count": 2,
        "knowledge_provider_accepted_count": 1,
    }
    assert "knowledge-conversation" not in repr(receipt)
    assert "안전한 응답" not in repr(receipt)


@pytest.mark.parametrize(
    ("event", "error"),
    [
        (
            {
                "event": "knowledge_answer_request_failed",
                "payload": {"provider": "anthropic", "model": "claude-test"},
            },
            "knowledge_claude_provider_request_or_json_failed",
        ),
        (
            {
                "event": "knowledge_answer_response_invalid",
                "payload": {"provider": "anthropic", "model": "claude-test"},
            },
            "knowledge_claude_provider_request_or_json_failed",
        ),
        (
            {
                "event": "knowledge_answer_fallback",
                "payload": {"reason_code": "provider_answer_unavailable_or_ungrounded"},
            },
            "knowledge_claude_provider_answer_unaccepted",
        ),
        (
            {
                "event": "knowledge_answer_not_called",
                "payload": {
                    "provider": "claude",
                    "called": False,
                    "reason_code": "official_guidance_coverage_unavailable",
                },
            },
            "knowledge_claude_provider_answer_unaccepted",
        ),
    ],
)
def test_real_knowledge_provider_trace_rejects_provider_failure_or_unaccepted_answer(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    event: dict[str, Any],
    error: str,
) -> None:
    events = _knowledge_provider_trace()
    events.append(event)
    turn = _write_knowledge_provider_trace(tmp_path, events)

    with pytest.raises(matrix.MatrixError, match=error):
        matrix._assert_real_knowledge_provider_trace(turn)


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
