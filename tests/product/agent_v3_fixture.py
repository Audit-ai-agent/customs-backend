from __future__ import annotations

from typing import Any

from customs_backend.domain import AgentReviewRequest, canonical_digest


def build_agent_v3_result(
    request: AgentReviewRequest, *, include_finding: bool = False
) -> dict[str, Any]:
    """A deterministic V3.1 response for Backend HTTP-contract tests only."""

    analyses: list[dict[str, Any]] = []
    for document in request.documents:
        quote = document.content_text[:1]
        document_type = (
            document.declared_type
            if document.declared_type
            in {
                "invoice",
                "packing_list",
                "bill_of_lading",
                "certificate_of_origin",
                "unknown",
            }
            else "unknown"
        )
        analyses.append(
            {
                "document_id": document.document_id,
                "filename": document.filename,
                "declared_type": document.declared_type,
                "document_type": document_type,
                "classification_source": {
                    "document_id": document.document_id,
                    "quote": quote,
                    "occurrence": 0,
                },
                "extraction_status": "complete",
                "facts": [],
                "line_items": [],
                "warnings": [],
            }
        )

    comparisons: list[dict[str, Any]] = []
    findings: list[dict[str, Any]] = []
    actions: list[dict[str, Any]] = [
        {
            "action_code": "human_review_required",
            "message": "A reviewer must decide.",
            "finding_ids": [],
        }
    ]
    if include_finding:
        document_id = request.documents[0].document_id
        comparisons.append(
            {
                "comparison_id": "cmp_fixture_quantity",
                "rule_code": "QTY_MISMATCH_INVOICE_PACKING",
                "status": "mismatch",
                "left": {
                    "document_id": document_id,
                    "item_id": None,
                    "fact_ids": [],
                    "display_value": "10",
                },
                "right": {
                    "document_id": None,
                    "item_id": None,
                    "fact_ids": [],
                    "display_value": "12",
                },
                "expected_value": "10",
                "observed_value": "12",
                "unit": "EA",
                "message": "Fixture quantity comparison is inconsistent.",
            }
        )
        findings.append(
            {
                "finding_id": "finding_fixture_quantity",
                "kind": "validation_issue",
                "code": "QTY_MISMATCH_INVOICE_PACKING",
                "severity": "high",
                "message": "Fixture quantity comparison needs a reviewer decision.",
                "item_id": None,
                "evidence_ids": [],
                "comparison_id": "cmp_fixture_quantity",
                "document_ids": [document_id],
                "fact_ids": [],
            }
        )
        actions[0]["finding_ids"] = ["finding_fixture_quantity"]

    result: dict[str, Any] = {
        "schema_version": "3.1",
        "case_id": request.case_id,
        "review_run_id": request.review_run_id,
        "review_as_of": request.review_as_of.isoformat(),
        "trade_type": request.trade_type,
        "jurisdiction": request.jurisdiction,
        "status": "needs_review",
        "requires_human_review": True,
        "execution_status": "succeeded",
        "assessment": "issues_detected" if include_finding else "no_issue_detected",
        "input_manifest_sha256": request.input_manifest_sha256,
        "report": {
            "overall_status": "needs_review",
            "summary": "Deterministic local fixture result.",
            "disclaimer": "A reviewer must make the final decision.",
        },
        "workflow": {
            "status": "completed",
            "stages": [
                {
                    "stage": "generate_result",
                    "status": "completed",
                    "attempt_count": 1,
                    "summary": "Fixture generated a deterministic review result.",
                }
            ],
            "reextractions": [],
        },
        "document_analyses": analyses,
        "comparisons": comparisons,
        "evidence": [],
        "findings": findings,
        "required_actions": actions,
        "engine": {"kind": "deterministic", "model_id": "backend-fixture"},
        "result_sha256": "0" * 64,
    }
    result["result_sha256"] = canonical_digest(
        {key: value for key, value in result.items() if key != "result_sha256"}
    )
    return result
