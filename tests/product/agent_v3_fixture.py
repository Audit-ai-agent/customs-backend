from __future__ import annotations

from typing import Any

from customs_backend.domain import AgentReviewRequest, canonical_digest


def build_agent_v3_result(request: AgentReviewRequest) -> dict[str, Any]:
    """A deterministic V3 response for Backend HTTP-contract tests only."""

    result: dict[str, Any] = {
        "schema_version": "3.0",
        "case_id": request.case_id,
        "review_run_id": request.review_run_id,
        "review_as_of": request.review_as_of.isoformat(),
        "trade_type": request.trade_type,
        "jurisdiction": request.jurisdiction,
        "status": "needs_review",
        "requires_human_review": True,
        "execution_status": "succeeded",
        "assessment": "no_issue_detected",
        "input_manifest_sha256": request.input_manifest_sha256,
        "report": {
            "overall_status": "needs_review",
            "summary": "Deterministic local fixture result.",
            "disclaimer": "A reviewer must make the final decision.",
        },
        "evidence": [],
        "findings": [],
        "required_actions": [
            {"action_code": "human_review_required", "message": "A reviewer must decide."}
        ],
        "result_sha256": "0" * 64,
    }
    result["result_sha256"] = canonical_digest(result)
    return result
