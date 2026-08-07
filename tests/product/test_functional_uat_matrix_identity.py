from __future__ import annotations

from copy import deepcopy
from typing import Any

import pytest

from tools import run_functional_uat_matrix as matrix


def test_matrix_cases_are_marked_as_system_functional_uat_cases() -> None:
    scenarios = matrix._scenario_matrix()

    assert len(scenarios) == 6
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
        assert all(any("가" <= character <= "힣" for character in document.filename) for document in scenario.documents)


def test_matrix_replacement_document_uses_a_korean_business_filename() -> None:
    assert matrix._REPLACEMENT_DOCUMENT_FILENAME == "상업송장_보완본.txt"


def _matching_result() -> dict[str, Any]:
    return {
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
            "planner": {
                "actions": [
                    {"summary": "문서 사실을 비교하고 법령 근거를 확인했습니다."}
                ]
            },
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
    scenario = next(item for item in matrix._scenario_matrix() if item.name == "all-documents-match")
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
    scenario = next(item for item in matrix._scenario_matrix() if item.name == "all-documents-match")
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
