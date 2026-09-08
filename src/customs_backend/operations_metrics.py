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
- ``review_load`` — where the human judgement work comes from, on each
  case's latest run only: comparison outcomes, findings per rule code, and
  the reviewer's coded rationale.  This is the measurement behind reducing
  the share of cases that need a reviewer; nothing here closes a case.
"""

from __future__ import annotations

from datetime import datetime, timedelta

from pydantic import BaseModel, ConfigDict, Field

from customs_backend.domain import CaseRecord, ReviewRunRecord

_DAILY_WINDOW_DAYS = 14
_COMPARISON_STATUSES = ("pass", "mismatch", "needs_review", "skip")


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


class CodeCount(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    code: str = Field(min_length=1, max_length=128)
    count: int = Field(ge=0)


class ComparisonOutcomeMetrics(BaseModel):
    """Automatic comparison outcomes on the latest run of every case."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    total: int = Field(ge=0)
    passed: int = Field(ge=0)
    mismatch: int = Field(ge=0)
    needs_review: int = Field(ge=0)
    skipped: int = Field(ge=0)


class DecisionStatusMetrics(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    confirmed: int = Field(ge=0)
    needs_information: int = Field(ge=0)
    resolved: int = Field(ge=0)


class ReviewLoadMetrics(BaseModel):
    """Which rules and rationales create the reviewer's judgement work."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    # Cases whose latest run raised at least one finding, versus none.
    cases_with_findings: int = Field(ge=0)
    cases_without_findings: int = Field(ge=0)
    comparisons: ComparisonOutcomeMetrics
    # Findings on latest runs grouped by rule code, most frequent first.
    findings_by_code: list[CodeCount]
    decision_statuses: DecisionStatusMetrics
    # Reviewer rationale codes on latest runs, most frequent first.
    # Decisions recorded without a code are counted under ``uncoded``.
    decision_reason_codes: list[CodeCount]


class OperationsMetricsView(BaseModel):
    model_config = ConfigDict(extra="forbid")

    generated_at: str
    cases: CaseStatusMetrics
    reviews: ReviewMetrics
    findings: FindingMetrics
    closures: ClosureMetrics
    daily_reviews: list[DailyReviewCount]
    review_load: ReviewLoadMetrics


def _latest_run(runs: list[ReviewRunRecord]) -> ReviewRunRecord | None:
    return max(runs, key=lambda run: run.created_at) if runs else None


def _sorted_counts(counter: dict[str, int]) -> list[CodeCount]:
    return [
        CodeCount(code=code, count=count)
        for code, count in sorted(counter.items(), key=lambda item: (-item[1], item[0]))
    ]


def _review_load(cases: list[CaseRecord], runs_by_case: dict[str, list[ReviewRunRecord]]) -> ReviewLoadMetrics:
    with_findings = 0
    without_findings = 0
    comparison_counts = dict.fromkeys(_COMPARISON_STATUSES, 0)
    findings_by_code: dict[str, int] = {}
    statuses = {"confirmed": 0, "needs_information": 0, "resolved": 0}
    reason_codes: dict[str, int] = {}
    for case in cases:
        latest = _latest_run(runs_by_case.get(case.case_id, []))
        if latest is None:
            continue
        finding_rows = [row for row in latest.findings if isinstance(row, dict)]
        if finding_rows:
            with_findings += 1
        else:
            without_findings += 1
        for row in finding_rows:
            code = row.get("code")
            if isinstance(code, str) and code:
                findings_by_code[code] = findings_by_code.get(code, 0) + 1
        for comparison in latest.comparisons:
            if not isinstance(comparison, dict):
                continue
            status = comparison.get("status")
            if isinstance(status, str) and status in comparison_counts:
                comparison_counts[status] += 1
        for decision in latest.reviewer_decisions:
            statuses[decision.status.value] += 1
            key = decision.reason_code.value if decision.reason_code is not None else "uncoded"
            reason_codes[key] = reason_codes.get(key, 0) + 1
    return ReviewLoadMetrics(
        cases_with_findings=with_findings,
        cases_without_findings=without_findings,
        comparisons=ComparisonOutcomeMetrics(
            total=sum(comparison_counts.values()),
            passed=comparison_counts["pass"],
            mismatch=comparison_counts["mismatch"],
            needs_review=comparison_counts["needs_review"],
            skipped=comparison_counts["skip"],
        ),
        findings_by_code=_sorted_counts(findings_by_code),
        decision_statuses=DecisionStatusMetrics(**statuses),
        decision_reason_codes=_sorted_counts(reason_codes),
    )


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
        review_load=_review_load(cases, runs_by_case),
    )


__all__ = [
    "OperationsMetricsView",
    "ReviewLoadMetrics",
    "build_operations_metrics",
]
