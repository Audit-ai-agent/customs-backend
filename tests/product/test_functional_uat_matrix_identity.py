from __future__ import annotations

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
