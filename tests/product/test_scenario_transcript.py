from __future__ import annotations

import json
from pathlib import Path

import pytest

from customs_backend.scenario_transcript import (
    FunctionalTranscriptController,
    ScenarioTranscriptError,
    ScenarioTranscriptRoot,
    redact_for_transcript,
    verify_run,
)


def _root(tmp_path: Path) -> Path:
    return tmp_path / "_local" / "state" / "scenario-runs"


def test_transcript_is_local_only_and_keeps_nonsecret_bodies(tmp_path: Path) -> None:
    root = ScenarioTranscriptRoot.create(_root(tmp_path))
    run = root.open_or_create_run("run-scenario")
    run.append(
        "public-api",
        "http_exchange",
        {
            "request_body": {"content_text": "COMMERCIAL INVOICE\nHS CODE: 0303540000"},
            "response_body": {"access_token": "must-not-persist", "status": "ready"},
        },
    )
    run.write_json("scenario.json", {"fixture_document": "synthetic invoice"})
    run.write_json("summary.json", {"status": "passed"})

    event = json.loads((run.directory / "public-api.jsonl").read_text(encoding="utf-8"))
    assert event["payload"]["request_body"]["content_text"].endswith("0303540000")
    assert event["payload"]["response_body"]["access_token"] == "[REDACTED]"
    assert (
        json.loads((run.directory / "summary.json").read_text(encoding="utf-8"))["status"]
        == "passed"
    )


def test_transcript_rejects_an_arbitrary_path_and_reused_summary(tmp_path: Path) -> None:
    with pytest.raises(ScenarioTranscriptError, match="root_invalid"):
        ScenarioTranscriptRoot.create(tmp_path / "output")

    run = ScenarioTranscriptRoot.create(_root(tmp_path)).open_or_create_run("run-once")
    run.write_json("summary.json", {"status": "passed"})
    with pytest.raises(ScenarioTranscriptError, match="file_exists"):
        run.write_json("summary.json", {"status": "passed"})


def test_text_and_nested_sensitive_values_are_redacted() -> None:
    synthetic_key = "sk" + "-ant-" + "abcdefghijklmnop"
    value = redact_for_transcript(
        {
            "authorization": "Bearer aaaaaaaaaaaaaaaaaaaa",
            "content_text": f"ANTHROPIC_API_KEY={synthetic_key}",
            "nested": [{"cookie": "session-value"}],
        }
    )
    assert value == {
        "authorization": "[REDACTED]",
        "content_text": "ANTHROPIC_API_KEY=[REDACTED]",
        "nested": [{"cookie": "[REDACTED]"}],
    }


def test_complete_functional_run_is_verified_without_echoing_bodies(tmp_path: Path) -> None:
    root = ScenarioTranscriptRoot.create(_root(tmp_path), secret_values=("live-secret-value",))
    controller = FunctionalTranscriptController(root=root)
    # A terminal summary is a checkpoint rather than a write barrier: later
    # functional-UAT conversation or correction traffic must not be dropped.
    controller.record_public(
        case_id="case-functional",
        method="POST",
        path="/api/v1/cases",
        request_body={"title": "actual case"},
        response_status=201,
        response_body={"case_id": "case-functional"},
    )
    controller.record_backend_agent(
        "run-functional",
        "review_request",
        {"request_body": {"content_text": "actual live-secret-value document"}},
    )
    controller.bind_review(
        case_id="case-functional",
        review_run_id="run-functional",
        job_id="job-functional",
        review_request={"trade_type": "import"},
        response_status=202,
        response_body={"job": {"review_run_id": "run-functional"}},
        job_status="succeeded",
    )
    directory = _root(tmp_path) / "run-functional"
    for stream, event in (("agent-data", "http_exchange"), ("claude", "response_received")):
        (directory / f"{stream}.jsonl").write_text(
            json.dumps(
                {
                    "schema_version": "scenario-transcript/v1",
                    "event": event,
                    "payload": {"status": "recorded"},
                }
            )
            + "\n",
            encoding="utf-8",
        )
    controller.record_public(
        case_id="case-functional",
        method="GET",
        path="/api/v1/review-runs/run-functional",
        request_body=None,
        response_status=200,
        response_body={"review_run_id": "run-functional"},
    )
    controller.finalize("run-functional", status="completed", fields={"job_status": "succeeded"})

    assert verify_run(_root(tmp_path), "run-functional") == {
        "review_run_id": "run-functional",
        "profile": "functional-uat",
        "status": "completed",
        "streams": {
            "public-api.jsonl": 3,
            "backend-agent.jsonl": 1,
            "agent-data.jsonl": 1,
            "claude.jsonl": 1,
        },
    }
    assert "live-secret-value" not in (directory / "backend-agent.jsonl").read_text(
        encoding="utf-8"
    )
    controller.record_public(
        case_id="case-functional",
        method="GET",
        path="/api/v1/review-runs/run-functional",
        request_body=None,
        response_status=200,
        response_body={"review_run_id": "run-functional"},
    )
    assert len((directory / "public-api.jsonl").read_text(encoding="utf-8").splitlines()) == 4
    summary = json.loads((directory / "summary.json").read_text(encoding="utf-8"))
    assert isinstance(summary.get("last_recorded_at"), str)


def test_revision_bind_records_its_actual_public_path(tmp_path: Path) -> None:
    controller = FunctionalTranscriptController(root=ScenarioTranscriptRoot.create(_root(tmp_path)))
    controller.bind_review(
        case_id="case-functional",
        review_run_id="run-revision",
        job_id="job-revision",
        review_request={"trade_type": "import"},
        response_status=202,
        response_body={"job": {"review_run_id": "run-revision"}},
        job_status="succeeded",
        public_path="/api/v1/review-runs/run-parent/revisions",
    )

    event = json.loads(
        (_root(tmp_path) / "run-revision" / "public-api.jsonl").read_text(encoding="utf-8")
    )
    assert event["payload"]["path"] == "/api/v1/review-runs/run-parent/revisions"


def test_verifier_rejects_unredacted_credential_shaped_stream_value(tmp_path: Path) -> None:
    root = ScenarioTranscriptRoot.create(_root(tmp_path))
    run = root.open_or_create_run("run-redaction")
    run.write_json(
        "scenario.json",
        {
            "schema_version": "scenario-transcript/v1",
            "profile": "functional-uat",
            "review_run_id": "run-redaction",
        },
    )
    run.write_json(
        "summary.json",
        {
            "schema_version": "scenario-transcript/v1",
            "profile": "functional-uat",
            "review_run_id": "run-redaction",
            "status": "completed",
        },
    )
    directory = run.directory
    synthetic_key = "sk" + "-ant-" + "abcdefghijklmnop"
    record = {
        "schema_version": "scenario-transcript/v1",
        "event": "http_exchange",
        "payload": {"content_text": f"ANTHROPIC_API_KEY={synthetic_key}"},
    }
    for stream in ("public-api", "backend-agent", "agent-data", "claude"):
        (directory / f"{stream}.jsonl").write_text(json.dumps(record) + "\n", encoding="utf-8")

    with pytest.raises(ScenarioTranscriptError, match="stream_invalid:public-api"):
        verify_run(_root(tmp_path), "run-redaction")
