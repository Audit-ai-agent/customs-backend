from __future__ import annotations

import pytest

from customs_backend.domain import AgentReviewResultV3
from customs_backend.ports import ConflictError
from customs_backend.service import _safe_evidence_provenance


def _request() -> dict:
    return {
        "request_id": "req-partial-1",
        "query_intent": "required_import_documents",
        "items": [
            {
                "item_id": "item-returned",
                "description": "Returned item",
                "hs_code": "0303540000",
            },
            {
                "item_id": "item-not-returned",
                "description": "Provider did not produce a typed result",
                "hs_code": "7318151000",
            },
        ],
    }


def _result(item_id: str = "item-returned") -> dict:
    return {
        "item_id": item_id,
        "hs_resolution": "exact",
        "resolved_hsk10": "0303540000",
        "coverage": "partial",
        "requirements_found": False,
        "evidence": [],
    }


def _envelope(*, status: str, result_item_id: str = "item-returned") -> AgentReviewResultV3:
    # This unit exercises the bounded projection after the full inbound V3
    # validator. ``model_construct`` keeps the fixture focused on the four
    # fields consumed by the projection.
    return AgentReviewResultV3.model_construct(
        retrieval_status=status,
        retrieval_request=_request(),
        retrieval_results=[_result(result_item_id)],
        rag_evidence=[],
    )


def test_manual_invoice_partial_retrieval_projects_only_observed_request_items() -> None:
    """Regress the UAT invoice run that returned one typed result for two queries."""

    projection = _safe_evidence_provenance(_envelope(status="partial"))

    assert projection["request_id"] == "req-partial-1"
    assert projection["query_intent"] == "required_import_documents"
    assert [item["item_id"] for item in projection["items"]] == ["item-returned"]
    assert projection["ranked_evidence"] == []


def test_partial_retrieval_rejects_result_outside_request() -> None:
    with pytest.raises(ConflictError, match="query/result items differ"):
        _safe_evidence_provenance(_envelope(status="partial", result_item_id="item-unknown"))


@pytest.mark.parametrize("duplicate_side", ["request", "result"])
def test_partial_retrieval_rejects_duplicate_item_identity(duplicate_side: str) -> None:
    envelope = _envelope(status="partial")
    if duplicate_side == "request":
        envelope.retrieval_request["items"].append(dict(envelope.retrieval_request["items"][0]))
    else:
        envelope.retrieval_results.append(dict(envelope.retrieval_results[0]))

    with pytest.raises(ConflictError, match="item IDs are duplicated"):
        _safe_evidence_provenance(envelope)


def test_ok_retrieval_still_requires_every_request_item() -> None:
    with pytest.raises(ConflictError, match="does not cover every query item"):
        _safe_evidence_provenance(_envelope(status="ok"))


def test_failed_retrieval_without_item_result_exposes_no_fabricated_item() -> None:
    envelope = AgentReviewResultV3.model_construct(
        retrieval_status="failed",
        retrieval_request=_request(),
        retrieval_results=[],
        rag_evidence=[],
    )

    projection = _safe_evidence_provenance(envelope)

    assert projection["request_id"] == "req-partial-1"
    assert projection["query_intent"] == "required_import_documents"
    assert projection["items"] == []
    assert projection["ranked_evidence"] == []
