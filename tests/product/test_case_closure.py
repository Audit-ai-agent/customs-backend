"""Case closure: the reviewer's final internal disposition."""

from __future__ import annotations

from typing import Any

from tests.product.agent_v3_fixture import build_agent_v3_result
from tests.product.test_workbench_api import _auth, _client


def _case_with_review(client: Any, *, include_finding: bool) -> str:
    case = client.post("/api/v1/cases", json={"title": "closure"}, headers=_auth()).json()
    client.post(
        f"/api/v1/cases/{case['case_id']}/documents",
        json={"document_type": "CI", "filename": "ci.txt", "content_text": "INVOICE"},
        headers=_auth(),
    )
    review = client.post(
        f"/api/v1/cases/{case['case_id']}/reviews",
        json={},
        headers={**_auth(), "Idempotency-Key": "closure-review-1"},
    )
    assert review.status_code == 202, review.text
    if include_finding:
        run_id = review.json()["job"]["review_run_id"]
        decision = client.put(
            f"/api/v1/review-runs/{run_id}/findings/finding_fixture_quantity/decision",
            json={"status": "confirmed", "reason": "확인함"},
            headers=_auth(),
        )
        assert decision.status_code == 200, decision.text
    return str(case["case_id"])


def test_close_case_records_disposition_and_blocks_more_reviews() -> None:
    with _client(review_handler=build_agent_v3_result) as client:
        case_id = _case_with_review(client, include_finding=False)
        closed = client.post(
            f"/api/v1/cases/{case_id}/closure",
            json={"disposition": "cleared", "reason": "이상 없음 · 검토 종결"},
            headers=_auth(),
        )
        assert closed.status_code == 200, closed.text
        body = closed.json()
        assert body["status"] == "closed"
        assert body["closure"]["disposition"] == "cleared"
        assert body["closure"]["closed_by"]

        again = client.post(
            f"/api/v1/cases/{case_id}/closure",
            json={"disposition": "cleared", "reason": "중복"},
            headers=_auth(),
        )
        assert again.status_code == 409

        review = client.post(
            f"/api/v1/cases/{case_id}/reviews",
            json={},
            headers={**_auth(), "Idempotency-Key": "closure-review-2"},
        )
        assert review.status_code == 409


def test_close_case_requires_decided_findings_and_a_review() -> None:
    def review_with_finding(request: Any) -> dict[str, Any]:
        return build_agent_v3_result(request, include_finding=True)

    with _client(review_handler=review_with_finding) as client:
        no_review = client.post("/api/v1/cases", json={"title": "no review"}, headers=_auth())
        blocked = client.post(
            f"/api/v1/cases/{no_review.json()['case_id']}/closure",
            json={"disposition": "cleared", "reason": "너무 이르다"},
            headers=_auth(),
        )
        assert blocked.status_code == 409

        case = client.post("/api/v1/cases", json={"title": "undecided"}, headers=_auth()).json()
        client.post(
            f"/api/v1/cases/{case['case_id']}/documents",
            json={"document_type": "CI", "filename": "ci.txt", "content_text": "INVOICE"},
            headers=_auth(),
        )
        client.post(
            f"/api/v1/cases/{case['case_id']}/reviews",
            json={},
            headers={**_auth(), "Idempotency-Key": "closure-review-3"},
        )
        undecided = client.post(
            f"/api/v1/cases/{case['case_id']}/closure",
            json={"disposition": "returned", "reason": "판단 전 종료 시도"},
            headers=_auth(),
        )
        assert undecided.status_code == 409

        decided_case = _case_with_review(client, include_finding=True)
        closed = client.post(
            f"/api/v1/cases/{decided_case}/closure",
            json={"disposition": "returned", "reason": "보완 요청 후 종결"},
            headers=_auth(),
        )
        assert closed.status_code == 200, closed.text
        assert closed.json()["closure"]["disposition"] == "returned"


def test_close_case_requires_reviewer_role() -> None:
    with _client(review_handler=build_agent_v3_result) as client:
        case_id = _case_with_review(client, include_finding=False)
        response = client.post(
            f"/api/v1/cases/{case_id}/closure",
            json={"disposition": "cleared", "reason": "승인자 시도"},
            headers={"Authorization": "Bearer approver-token"},
        )
        assert response.status_code == 403
