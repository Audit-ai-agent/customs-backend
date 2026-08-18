"""Org-scoped operations metrics for the Browser dashboard.

Every number here is a plain count over stored cases and review runs — no
sampling, no estimation, no external calls.  The view states exactly which
population each block counts so the Browser can label provenance honestly:

- ``cases``     — every case in the org, by status.
- ``reviews``   — every review run ever recorded (the full history).
- ``findings``  — findings on each case's *latest* run only, because a child
  run re-evaluates its parent's findings and only the latest set is still a
  judgement workload.
- ``closures``  — reviewer dispositions of closed cases.
- ``daily_reviews`` — runs per day for the trailing 14 days, empty days
  included so the series has no gaps.
"""

from __future__ import annotations

from datetime import datetime, timedelta

from pydantic import BaseModel, ConfigDict, Field

from customs_backend.domain import CaseRecord, ReviewRunRecord

_DAILY_WINDOW_DAYS = 14


class CaseStatusMetrics(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    total: int = Field(ge=0)
    open: int = Field(ge=0)
    in_review: int = Field(ge=0)
    closed: int = Field(ge=0)


class ReviewMetrics(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    total: int = Field(ge=0)
    # Child runs created by applying approved corrections.
    revisions: int = Field(ge=0)
    no_issue_detected: int = Field(ge=0)
    issues_detected: int = Field(ge=0)
    indeterminate: int = Field(ge=0)
    succeeded: int = Field(ge=0)
    partial: int = Field(ge=0)
    failed: int = Field(ge=0)


class FindingMetrics(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    total: int = Field(ge=0)
    decided: int = Field(ge=0)
    open: int = Field(ge=0)


class ClosureMetrics(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    cleared: int = Field(ge=0)
    returned: int = Field(ge=0)


class DailyReviewCount(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    date: str = Field(pattern=r"^\d{4}-\d{2}-\d{2}$")
    count: int = Field(ge=0)


class OperationsMetricsView(BaseModel):
    model_config = ConfigDict(extra="forbid")

    generated_at: str
    cases: CaseStatusMetrics
    reviews: ReviewMetrics
    findings: FindingMetrics
    closures: ClosureMetrics
    daily_reviews: list[DailyReviewCount]


def _latest_run(runs: list[ReviewRunRecord]) -> ReviewRunRecord | None:
    return max(runs, key=lambda run: run.created_at) if runs else None


def build_operations_metrics(
    cases: list[CaseRecord],
    runs_by_case: dict[str, list[ReviewRunRecord]],
    *,
    now: datetime,
) -> OperationsMetricsView:
    statuses = [case.status.value for case in cases]
    all_runs = [run for runs in runs_by_case.values() for run in runs]

    findings_total = 0
    findings_decided = 0
    for case in cases:
        latest = _latest_run(runs_by_case.get(case.case_id, []))
        if latest is None:
            continue
        decided_ids = {decision.finding_id for decision in latest.reviewer_decisions}
        finding_ids = {
            str(finding.get("finding_id", ""))
            for finding in latest.findings
            if isinstance(finding, dict)
        }
        findings_total += len(finding_ids)
        findings_decided += len(finding_ids & decided_ids)

    closures = [case.closure for case in cases if case.closure is not None]

    window_start = (now - timedelta(days=_DAILY_WINDOW_DAYS - 1)).date()
    per_day = {
        (window_start + timedelta(days=offset)).isoformat(): 0
        for offset in range(_DAILY_WINDOW_DAYS)
    }
    for run in all_runs:
        key = run.created_at.date().isoformat()
        if key in per_day:
            per_day[key] += 1

    return OperationsMetricsView(
        generated_at=now.isoformat(),
        cases=CaseStatusMetrics(
            total=len(cases),
            open=statuses.count("open"),
            in_review=statuses.count("in_review"),
            closed=statuses.count("closed"),
        ),
        reviews=ReviewMetrics(
            total=len(all_runs),
            revisions=sum(1 for run in all_runs if run.parent_review_run_id is not None),
            no_issue_detected=sum(1 for run in all_runs if run.assessment.value == "no_issue_detected"),
            issues_detected=sum(1 for run in all_runs if run.assessment.value == "issues_detected"),
            indeterminate=sum(1 for run in all_runs if run.assessment.value == "indeterminate"),
            succeeded=sum(1 for run in all_runs if run.execution_status.value == "succeeded"),
            partial=sum(1 for run in all_runs if run.execution_status.value == "partial"),
            failed=sum(1 for run in all_runs if run.execution_status.value == "failed"),
        ),
        findings=FindingMetrics(
            total=findings_total,
            decided=findings_decided,
            open=findings_total - findings_decided,
        ),
        closures=ClosureMetrics(
            cleared=sum(1 for closure in closures if closure.disposition == "cleared"),
            returned=sum(1 for closure in closures if closure.disposition == "returned"),
        ),
        daily_reviews=[
            DailyReviewCount(date=day, count=count) for day, count in sorted(per_day.items())
        ],
    )


__all__ = [
    "OperationsMetricsView",
    "build_operations_metrics",
]
