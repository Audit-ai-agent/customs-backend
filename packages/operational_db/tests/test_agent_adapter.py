from operational_db import snapshot_from_agent_state


def test_agent_state_maps_to_immutable_snapshot():
    state = {
        "review_run_id": "run-live-1",
        "case_id": "case-live-1",
        "final_status": "completed",
        "requires_human_review": False,
        "knowledge_snapshot_id": "kb-1",
        "retrieval_request": {"request_id": "req-1"},
        "retrieval_status": "ok",
        "retrieval_coverage": "complete",
        "retrieval_results": [{"item_id": "doc_inv_line_1", "coverage": "complete"}],
        "retrieval_errors": [],
        "normalized_documents": {
            "invoice": {
                "document_type": "invoice",
                "line_items": [
                    {
                        "item_id": "doc_inv_line_1",
                        "description": "Stainless bolts",
                        "hs_code": "7318151000",
                    }
                ],
            }
        },
        "validation_issues": [],
        "rag_evidence": [],
        "regulatory_findings": [],
        "report_json": {"overall_status": "completed"},
        "audit_trail": [{"stage": "route", "event": "completed"}],
    }

    snapshot = snapshot_from_agent_state(state, request={"schema_version": "1.0"})

    assert snapshot.review_run_id == "run-live-1"
    assert snapshot.items[0]["item_id"] == "doc_inv_line_1"
    assert snapshot.request == {"schema_version": "1.0"}
    assert snapshot.retrieval_request == {"request_id": "req-1"}
    assert snapshot.retrieval_status == "ok"
    assert snapshot.retrieval_results[0]["coverage"] == "complete"
