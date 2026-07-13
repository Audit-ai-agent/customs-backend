from __future__ import annotations

from typing import Any

from operational_db.models import ReviewRunSnapshot


def snapshot_from_agent_state(
    state: dict[str, Any],
    *,
    request: dict[str, Any] | None = None,
    parent_run_id: str | None = None,
) -> ReviewRunSnapshot:
    """Convert the Agent's compatible dict state into an immutable DB snapshot.

    This adapter is intentionally owned by the persistence boundary: Agent graph
    nodes remain unaware of the operational database.
    """

    items: list[dict[str, Any]] = []
    for document in (state.get("normalized_documents") or {}).values():
        if document.get("document_type") != "invoice":
            continue
        for item in document.get("line_items") or []:
            if not item.get("item_id"):
                continue
            items.append(
                {
                    "item_id": item["item_id"],
                    "description": item.get("description"),
                    "hs_code": item.get("hs_code"),
                    "origin_country": item.get("origin_country"),
                    "item": item,
                }
            )

    report = state.get("report_json") or {}
    final_status = state.get("final_status") or report.get("overall_status")
    if not final_status:
        raise ValueError("Agent state has no final status")
    review_run_id = state.get("review_run_id")
    if not review_run_id:
        raise ValueError("Agent state has no review_run_id")

    return ReviewRunSnapshot(
        review_run_id=review_run_id,
        case_id=state["case_id"],
        parent_run_id=parent_run_id,
        final_status=final_status,
        requires_human_review=bool(state.get("requires_human_review")),
        knowledge_snapshot_id=state.get("knowledge_snapshot_id"),
        request=request or {},
        retrieval_request=state.get("retrieval_request") or {},
        retrieval_status=state.get("retrieval_status"),
        retrieval_coverage=state.get("retrieval_coverage"),
        retrieval_results=state.get("retrieval_results") or [],
        retrieval_errors=state.get("retrieval_errors") or [],
        documents=state.get("documents") or [],
        classified_documents=state.get("classified_documents") or {},
        extracted_documents=state.get("extracted_documents") or {},
        normalized_documents=state.get("normalized_documents") or {},
        pairing_result=state.get("pairing_result") or {},
        items=items,
        validation_issues=state.get("validation_issues") or [],
        evidence=state.get("rag_evidence") or [],
        regulatory_findings=state.get("regulatory_findings") or [],
        report=report,
        audit_events=state.get("audit_trail") or [],
    )
