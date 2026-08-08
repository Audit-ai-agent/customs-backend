from __future__ import annotations

from copy import deepcopy
import json
from pathlib import Path
from typing import Any

import httpx
import pytest

from tools import run_dev_fixture_scenarios as runner
from customs_backend.scenario_transcript import ScenarioTranscriptRoot


def test_json_object_accepts_safe_problem_json_for_rejected_public_probe() -> None:
    response = httpx.Response(
        422,
        headers={"content-type": "application/problem+json; charset=utf-8"},
        json={"code": "knowledge_input_not_allowed"},
    )

    assert runner._json_object(response, endpoint="POST /knowledge/messages") == {
        "code": "knowledge_input_not_allowed"
    }


def _export_scenario() -> runner.Scenario:
    return next(
        scenario
        for scenario in runner.SCENARIOS
        if scenario.name == "export-documents-and-submission-evidence"
    )


def _export_run() -> dict[str, Any]:
    scenario = _export_scenario()
    document_analyses = [
        {"document_type": document_type, "facts": [], "line_items": []}
        for document_type in scenario.required_document_types
    ]
    document_analyses[4]["facts"] = [
        {"name": "export_declaration_no"},
        {"name": "destination_country"},
        {"name": "submission_status"},
    ]
    document_analyses[5]["facts"] = [{"name": "submission_receipt_no"}]
    return {
        "execution_status": "partial",
        "assessment": "issues_detected",
        "findings": [{"code": "CCCT_DOC_EXPORT_DECLARATION"}],
        "required_actions": [{"action_code": "human_review_required"}],
        "review_disposition": "human_review_required",
        "completion_eligibility": {"eligible": False},
        "workflow": {"status": "completed", "stages": []},
        "document_analyses": document_analyses,
        "comparisons": [{"rule_code": code} for code in scenario.required_comparison_codes],
    }


def test_export_fixture_requires_text_export_documents_comparisons_and_submission_facts() -> None:
    scenario = _export_scenario()
    assert {document.document_type for document in scenario.documents} >= {
        "AWB",
        "EXPORT_DECLARATION",
        "SUPPORTING_EVIDENCE",
    }
    assert scenario.review_body == {"trade_type": "export", "review_as_of": "2026-08-05"}
    assert scenario.execution_status == "partial"
    assert (
        scenario.expected_data_coverage_error_code == "FOREIGN_DESTINATION_REQUIREMENTS_UNAVAILABLE"
    )
    assert scenario.mixed_assistant_question is not None
    assert scenario.submission_status_question is not None

    run = _export_run()
    runner._assert_outcome(scenario, {"status": "succeeded"}, run)

    missing_comparison = deepcopy(run)
    missing_comparison["comparisons"] = missing_comparison["comparisons"][1:]
    with pytest.raises(runner.ScenarioExecutionError, match="required document comparison"):
        runner._assert_outcome(scenario, {"status": "succeeded"}, missing_comparison)


def test_missing_hs_fixture_keeps_an_input_gap_distinct_from_data_partial() -> None:
    scenario = next(item for item in runner.SCENARIOS if item.name == "missing-hs")

    # The bounded Agent does not send an empty HS retrieval to Data.  It
    # preserves the HS finding but finishes the review as a human-review-only
    # input limitation rather than describing the Data service as partial.
    assert scenario.execution_status == "succeeded"
    assert scenario.required_finding_codes == ("hs_code_not_found",)
    assert scenario.action_codes == ("human_review_required",)


def test_transport_document_without_hs_keeps_comparison_distinct_from_data_partial() -> None:
    scenario = next(item for item in runner.SCENARIOS if item.name == "cross-document-mismatches")

    # The B/L remains part of the document comparison, but it is not a
    # commodity retrieval target. The mismatch findings therefore complete
    # without representing Data as partially available.
    assert scenario.execution_status == "succeeded"
    assert {
        "bill_packing_bl_no_mismatch",
        "bill_packing_container_mismatch",
        "bill_packing_gross_weight_mismatch",
    }.issubset(scenario.required_finding_codes)


def _write_stream(path: Path, records: list[dict[str, Any]]) -> None:
    path.write_text("\n".join(json.dumps(record) for record in records) + "\n", encoding="utf-8")


def test_internal_transcript_accepts_only_the_explicit_no_hs_data_skip(tmp_path) -> None:
    root = ScenarioTranscriptRoot.create(tmp_path / "_local" / "state" / "scenario-runs")
    run = root.open_or_create_run("run-development-skip")

    def record(event: str, payload: dict[str, Any]) -> dict[str, Any]:
        return {
            "schema_version": "scenario-transcript/v1",
            "recorded_at": "2026-08-05T00:00:00+00:00",
            "event": event,
            "payload": payload,
        }

    _write_stream(
        run.directory / "backend-agent.jsonl",
        [record("review_request", {}), record("review_response", {})],
    )
    _write_stream(
        run.directory / "agent-data.jsonl",
        [
            record(
                "retrieve_skipped",
                {"reason_code": "no_hs_qualified_commodity", "retrieve_called": False},
            )
        ],
    )
    _write_stream(run.directory / "claude.jsonl", [record("not_called", {"called": False})])

    runner._assert_internal_transcript(run)

    _write_stream(
        run.directory / "agent-data.jsonl",
        [
            record(
                "retrieve_skipped",
                {"reason_code": "no_hs_qualified_commodity", "retrieve_called": True},
            )
        ],
    )
    with pytest.raises(runner.ScenarioExecutionError, match="retrieval skip transcript"):
        runner._assert_internal_transcript(run)


def test_internal_transcript_distinguishes_known_data_coverage_from_data_failure(tmp_path) -> None:
    root = ScenarioTranscriptRoot.create(tmp_path / "_local" / "state" / "scenario-runs")
    run = root.open_or_create_run("run-export-coverage")

    def record(event: str, payload: dict[str, Any]) -> dict[str, Any]:
        return {
            "schema_version": "scenario-transcript/v1",
            "recorded_at": "2026-08-05T00:00:00+00:00",
            "event": event,
            "payload": payload,
        }

    _write_stream(
        run.directory / "backend-agent.jsonl",
        [record("review_request", {}), record("review_response", {})],
    )
    _write_stream(
        run.directory / "agent-data.jsonl",
        [
            record("http_exchange", {"response_body": {"status": "ready"}}),
            record(
                "http_exchange",
                {
                    "response_body": {
                        "status": "partial",
                        "results": [
                            {
                                "coverage": "partial",
                                "errors": [
                                    {"code": "FOREIGN_DESTINATION_REQUIREMENTS_UNAVAILABLE"}
                                ],
                            }
                        ],
                    }
                },
            ),
        ],
    )
    _write_stream(run.directory / "claude.jsonl", [record("not_called", {"called": False})])

    runner._assert_internal_transcript(
        run,
        expected_data_coverage_error_code="FOREIGN_DESTINATION_REQUIREMENTS_UNAVAILABLE",
    )

    _write_stream(
        run.directory / "agent-data.jsonl",
        [
            record("http_exchange", {"response_body": {"status": "ready"}}),
            record(
                "http_exchange",
                {"response_body": {"status": "partial", "results": []}},
            ),
            record("http_failed", {"error_code": "data_request_failed"}),
        ],
    )
    with pytest.raises(runner.ScenarioExecutionError, match="Data coverage limitation transcript"):
        runner._assert_internal_transcript(
            run,
            expected_data_coverage_error_code="FOREIGN_DESTINATION_REQUIREMENTS_UNAVAILABLE",
        )


def test_mixed_and_submission_answer_assertions_keep_the_two_boundaries_distinct() -> None:
    runner._assert_mixed_assistant_sections(
        {
            "answer_sections": [
                {"kind": "case_analysis", "citations": [{"kind": "comparison"}]},
                {
                    "kind": "general_guidance",
                    "citations": [
                        {
                            "kind": "guidance_evidence",
                            "source_kind": "development_fixture",
                            "coverage": "covered",
                        }
                    ],
                },
            ]
        },
        scenario_name="fixture",
    )
    runner._assert_submission_status_answer(
        {"answer_scope": "case_grounded", "citations": [{"kind": "fact"}]},
        scenario_name="fixture",
    )

    with pytest.raises(runner.ScenarioExecutionError, match="mixed assistant sections"):
        runner._assert_mixed_assistant_sections(
            {"answer_sections": [{"kind": "case_analysis", "citations": []}]},
            scenario_name="fixture",
        )
    with pytest.raises(runner.ScenarioExecutionError, match="submission-status answer"):
        runner._assert_submission_status_answer(
            {"answer_scope": "case_grounded", "citations": []}, scenario_name="fixture"
        )


def test_standalone_knowledge_journey_keeps_case_values_out_of_its_safe_receipt(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[tuple[str, str, dict[str, Any] | None]] = []

    class FakeClient:
        def __init__(self, *, base_url: str) -> None:
            assert base_url == "http://127.0.0.1:5174/api/v1"

        def begin_development_session(self) -> str:
            return "local-token"

        def request(
            self,
            method: str,
            path: str,
            *,
            body: dict[str, Any] | None = None,
            **_kwargs: Any,
        ) -> dict[str, Any]:
            calls.append((method, path, body))
            citation = {
                "kind": "guidance_evidence",
                "source_kind": "development_fixture",
                "coverage": "covered",
            }
            if method == "POST" and path == "/knowledge/conversations":
                return {
                    "conversation_id": "knowledge-conversation-1",
                    "trade_type": "export",
                    "as_of": "2026-08-05",
                }
            if (
                method == "POST"
                and path.endswith("/messages")
                and body
                and "filename:" in str(body.get("content", ""))
            ):
                assert _kwargs.get("record") is False
                assert _kwargs.get("expected_status") == 422
                return {"code": "knowledge_input_not_allowed"}
            if method == "POST" and path.endswith("/messages"):
                return {
                    "assistant_message": {
                        "role": "assistant",
                        "answer_scope": "general_customs_guidance",
                        "knowledge_coverage": "covered",
                        "knowledge_as_of": "2026-08-05",
                        "citations": [citation],
                        "answer_sections": [{"kind": "general_guidance", "citations": [citation]}],
                    }
                }
            if method == "GET" and path.endswith("/messages"):
                return {
                    "items": [
                        {"role": "user"},
                        {"role": "assistant"},
                        {"role": "user"},
                        {"role": "assistant"},
                    ]
                }
            if method == "GET" and path == "/knowledge/conversations":
                return {
                    "items": [
                        {
                            "conversation_id": "knowledge-conversation-1",
                            "trade_type": "export",
                            "as_of": "2026-08-05",
                        }
                    ]
                }
            raise AssertionError(f"unexpected request: {method} {path}")

        def close(self) -> None:
            return None

    monkeypatch.setattr(runner, "PublicApiScenarioClient", FakeClient)
    turn_counts = iter((2, 2))
    monkeypatch.setattr(runner, "_knowledge_turn_count", lambda _conversation_id: next(turn_counts))

    receipt = runner._run_standalone_knowledge_journey(base_url="http://127.0.0.1:5174/api/v1")

    assert receipt == {
        "scenario": "standalone-official-trade-guidance",
        "status": "passed",
        "knowledge_turns_verified": 2,
        "knowledge_recent_history_verified": 1,
        "development_fixture_citations_verified": 1,
        "knowledge_rejected_inputs_verified": 1,
        "knowledge_rejected_agent_dispatches": 0,
        "knowledge_rejected_exchanges": 0,
    }
    assert "knowledge-conversation-1" not in repr(receipt)
    assert calls[0] == (
        "POST",
        "/knowledge/conversations",
        {"title": "개발용 수출 신고 안내", "trade_type": "export", "as_of": "2026-08-05"},
    )
    assert calls[1][0:2] == ("POST", "/knowledge/conversations/knowledge-conversation-1/messages")
    assert calls[1][2] == {"content": "수출 신고 전에 필요한 기본 서류와 절차를 알려 주세요."}
    assert calls[2][0:2] == ("POST", "/knowledge/conversations/knowledge-conversation-1/messages")
    assert calls[2][2] == {"content": "수출 운송 서류도 공식 자료 범위에서 알려 주세요."}
    assert calls[4][0:2] == ("POST", "/knowledge/conversations/knowledge-conversation-1/messages")
    assert calls[4][2] == {"content": "filename: sample-invoice.txt\nQuantity: 2"}


def test_runner_adds_one_safe_knowledge_receipt_after_review_scenarios(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    seen: list[str] = []

    def run_review(scenario: runner.Scenario, **_kwargs: Any) -> dict[str, Any]:
        seen.append(scenario.name)
        return {"scenario": scenario.name, "status": "passed"}

    monkeypatch.setattr(runner, "_run_scenario", run_review)
    monkeypatch.setattr(
        runner,
        "_run_standalone_knowledge_journey",
        lambda **_kwargs: {
            "scenario": "standalone-official-trade-guidance",
            "status": "passed",
            "knowledge_turns_verified": 2,
            "knowledge_recent_history_verified": 1,
            "development_fixture_citations_verified": 1,
            "knowledge_rejected_inputs_verified": 1,
            "knowledge_rejected_agent_dispatches": 0,
            "knowledge_rejected_exchanges": 0,
        },
    )

    results = runner.run(
        base_url="http://127.0.0.1:5174/api/v1",
        transcript_root=tmp_path / "_local" / "state" / "scenario-runs",
        scenarios=(_export_scenario(),),
    )

    assert seen == ["export-documents-and-submission-evidence"]
    assert results[-1] == {
        "scenario": "standalone-official-trade-guidance",
        "status": "passed",
        "knowledge_turns_verified": 2,
        "knowledge_recent_history_verified": 1,
        "development_fixture_citations_verified": 1,
        "knowledge_rejected_inputs_verified": 1,
        "knowledge_rejected_agent_dispatches": 0,
        "knowledge_rejected_exchanges": 0,
    }
