"""Operations metrics: plain counts over stored cases and review runs.

The unit tests exercise the pure aggregation; the endpoint test drives the
API client through a real review so every number reflects stored state.
"""

from __future__ import annotations

from datetime import date, datetime, timezone
from typing import Any

from customs_backend.domain import (
    CaseClosureRecord,
    CaseRecord,
    CompletionEligibility,
    FindingDecisionHistoryEntry,
    FindingDecisionRecord,
    ReviewRunRecord,
)
from customs_backend.operations_metrics import build_operations_metrics

from tests.product.test_workbench_api import _auth, _client
from tests.product.test_trade_features import _completed_run, _review_result_with_facts

_NOW = datetime(2026, 8, 18, 12, 0, tzinfo=timezone.utc)


def _case(case_id: str, status: str, closure: CaseClosureRecord | None = None) -> CaseRecord:
    return CaseRecord(
        case_id=case_id,
        org_id="org_test",
        title=f"case {case_id}",
        status=status,
        document_manifest_digest="0" * 64,
        closure=closure,
        created_by="tester",
        created_at=_NOW,
        updated_at=_NOW,
    )


def _run(
    run_id: str,
    case_id: str,
    *,
    created_at: datetime,
    assessment: str = "issues_detected",
    parent: str | None = None,
    findings: list[dict[str, Any]] | None = None,
    decided: list[str] | None = None,
) -> ReviewRunRecord:
    return ReviewRunRecord(
        review_run_id=run_id,
        case_id=case_id,
        org_id="org_test",
        job_id=f"job_{run_id}",
        parent_review_run_id=parent,
        case_revision=1,
        review_as_of=date(2026, 8, 5),
        trade_type="import",
        input_document_manifest=[
            {"document_id": "doc_ci", "document_type": "CI", "content_sha256": "0" * 64}
        ],
        input_manifest_digest="0" * 64,
        agent_input_manifest_sha256="0" * 64,
        provider_result_sha256="0" * 64,
        result_digest="0" * 64,
        execution_status="succeeded",
        assessment=assessment,
        completion_eligibility=CompletionEligibility(unmet_reason_codes=["human_review_required"]),
        report={},
        evidence=[],
        findings=findings or [],
        required_actions=[],
        reviewer_decisions=[
            FindingDecisionRecord(
                finding_id=finding_id,
                status="confirmed",
                reason="checked",
                decided_by="tester",
                decided_at=_NOW,
                history=[
                    FindingDecisionHistoryEntry(
                        status="confirmed", reason="checked", decided_by="tester", decided_at=_NOW
                    )
                ],
            )
            for finding_id in decided or []
        ],
        created_by="tester",
        created_at=created_at,
    )


def test_metrics_count_cases_reviews_and_latest_run_findings() -> None:
    cases = [
        _case("c1", "in_review"),
        _case("c2", "open"),
        _case(
            "c3",
            "closed",
            CaseClosureRecord(
                disposition="cleared", reason="ok", review_run_id="r0", closed_by="tester", closed_at=_NOW
            ),
        ),
    ]
    old = _run(
        "r1",
        "c1",
        created_at=datetime(2026, 8, 10, tzinfo=timezone.utc),
        findings=[{"finding_id": "f1"}, {"finding_id": "f2"}],
    )
    latest = _run(
        "r2",
        "c1",
        created_at=datetime(2026, 8, 17, tzinfo=timezone.utc),
        parent="r1",
        assessment="no_issue_detected",
        findings=[{"finding_id": "f3"}, {"finding_id": "f4"}],
        decided=["f3"],
    )
    view = build_operations_metrics(
        cases, {"c1": [old, latest], "c2": [], "c3": []}, now=_NOW
    )

    assert view.cases.total == 3
    assert (view.cases.open, view.cases.in_review, view.cases.closed) == (1, 1, 1)
    assert view.reviews.total == 2
    assert view.reviews.revisions == 1
    assert view.reviews.issues_detected == 1
    assert view.reviews.no_issue_detected == 1
    # Findings count the latest run only: f1/f2 were superseded by r2.
    assert view.findings.total == 2
    assert view.findings.decided == 1
    assert view.findings.open == 1
    assert view.closures.cleared == 1
    assert view.closures.returned == 0


def test_metrics_daily_series_covers_a_gapless_trailing_window() -> None:
    runs = [
        _run("r1", "c1", created_at=datetime(2026, 8, 18, 1, 0, tzinfo=timezone.utc)),
        _run("r2", "c1", created_at=datetime(2026, 8, 16, 1, 0, tzinfo=timezone.utc)),
        # Outside the 14-day window: never counted.
        _run("r3", "c1", created_at=datetime(2026, 7, 1, tzinfo=timezone.utc)),
    ]
    view = build_operations_metrics([_case("c1", "in_review")], {"c1": runs}, now=_NOW)

    assert len(view.daily_reviews) == 14
    assert [entry.date for entry in view.daily_reviews] == sorted(
        entry.date for entry in view.daily_reviews
    )
    by_date = {entry.date: entry.count for entry in view.daily_reviews}
    assert by_date["2026-08-18"] == 1
    assert by_date["2026-08-16"] == 1
    assert by_date["2026-08-17"] == 0
    assert sum(by_date.values()) == 2


def test_operations_metrics_endpoint_round_trip() -> None:
    with _client(review_handler=_review_result_with_facts) as client:
        _completed_run(client)
        response = client.get("/api/v1/operations-metrics", headers=_auth())
        assert response.status_code == 200
        body = response.json()
        assert body["cases"]["total"] >= 1
        assert body["reviews"]["total"] >= 1
        assert body["findings"]["total"] >= 0
        assert len(body["daily_reviews"]) == 14
        assert body["generated_at"]
