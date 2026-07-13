from __future__ import annotations

from datetime import date

import pytest

from customs_ai_agent.adapters.retrieval import FailingRegulationRetriever
from customs_ai_agent.schemas.models import (
    EvidenceRetrievalResponse,
    RetrievalItemResult,
)
from integration.review_service import ReviewCommand, execute_review
from operational_db import InMemoryOperationalRepository


class CompleteNoRequirementRetriever:
    """A deterministic contract-valid retriever for application-service tests."""

    def __init__(self) -> None:
        self.requests = []

    def retrieve(self, request):
        self.requests.append(request)
        results = []
        for item in request.items:
            resolved = item.hs_code if len(item.hs_code or "") == 10 else "7318151000"
            results.append(
                RetrievalItemResult(
                    item_id=item.item_id,
                    hs_resolution=(
                        "exact" if len(item.hs_code or "") == 10 else "unique_descendant"
                    ),
                    resolved_hsk10=resolved,
                    candidate_hsk10s=[resolved],
                    coverage="complete",
                    requirements_found=False,
                    evidence=[],
                    errors=[],
                )
            )
        return EvidenceRetrievalResponse(
            request_id=request.request_id,
            status="ok",
            knowledge_snapshot_id="live-test-snapshot",
            results=results,
            errors=[],
        )


def _command(**updates) -> ReviewCommand:
    payload = {
        "scenario_id": "happy_path",
        "review_run_id": "visual-service-run-1",
        "review_as_of": "2026-07-10",
        "trade_type": "import",
        "rag_url": "http://127.0.0.1:8010",
        "store": True,
    }
    payload.update(updates)
    return ReviewCommand.model_validate(payload)


def test_execute_review_runs_agent_persists_and_verifies_readback():
    repository = InMemoryOperationalRepository()
    retriever = CompleteNoRequirementRetriever()

    execution = execute_review(
        _command(), repository=repository, retriever=retriever
    )
    payload = execution.to_api_dict()

    assert len(retriever.requests) == 1
    assert execution.reused is False
    assert execution.stored is True
    assert execution.persistence_verified is True
    assert execution.hash_before == execution.hash_after
    stored = repository.get_run("visual-service-run-1")
    assert stored is not None
    assert stored.canonical_hash() == execution.snapshot.canonical_hash()
    assert payload["summary"] == {
        "case_id": "case_happy_path",
        "review_run_id": "visual-service-run-1",
        "final_status": "completed",
        "requires_human_review": False,
        "retrieval_status": "ok",
        "retrieval_coverage": "complete",
        "knowledge_snapshot_id": "live-test-snapshot",
    }
    assert payload["system_status"] == "PASS"
    assert payload["counts"]["documents"] == 4
    assert payload["counts"]["items"] == 1
    assert payload["counts"]["audit_events"] >= 10


def test_same_run_and_request_is_reused_without_calling_retriever_again():
    repository = InMemoryOperationalRepository()
    retriever = CompleteNoRequirementRetriever()
    command = _command(review_run_id="visual-idempotent-run")

    first = execute_review(command, repository=repository, retriever=retriever)
    second = execute_review(command, repository=repository, retriever=retriever)

    assert first.reused is False
    assert second.reused is True
    assert second.state is None
    assert second.persistence_verified is True
    assert second.hash_before == first.hash_before == second.hash_after
    assert len(retriever.requests) == 1


def test_same_run_with_different_request_is_rejected():
    repository = InMemoryOperationalRepository()
    retriever = CompleteNoRequirementRetriever()
    execute_review(
        _command(review_run_id="visual-conflict-run"),
        repository=repository,
        retriever=retriever,
    )

    with pytest.raises(ValueError, match="different request"):
        execute_review(
            _command(review_run_id="visual-conflict-run", trade_type="export"),
            repository=repository,
            retriever=retriever,
        )


def test_no_store_keeps_persistence_result_explicitly_unverified():
    execution = execute_review(
        _command(review_run_id="visual-no-store", store=False),
        retriever=CompleteNoRequirementRetriever(),
    )

    assert execution.stored is False
    assert execution.readback is None
    assert execution.persistence_verified is None
    assert execution.hash_after is None
    assert execution.to_api_dict()["system_status"] == "PASS"


def test_missing_review_date_uses_evidence_readiness_supported_date(monkeypatch):
    retriever = CompleteNoRequirementRetriever()
    monkeypatch.setattr(
        "integration.review_service.fetch_readiness",
        lambda _rag_url: {
            "status": "ready",
            "supported_review_as_of": "2026-07-10",
        },
    )

    execution = execute_review(
        _command(review_run_id="visual-ready-date", review_as_of=None, store=False),
        retriever=retriever,
    )

    assert retriever.requests[0].review_as_of == date(2026, 7, 10)
    assert execution.snapshot.request["review_as_of"] == "2026-07-10"


def test_retriever_failure_is_saved_as_warn_fail_closed_result():
    execution = execute_review(
        _command(review_run_id="visual-rag-failure"),
        repository=InMemoryOperationalRepository(),
        retriever=FailingRegulationRetriever(),
    )
    payload = execution.to_api_dict()

    assert execution.persistence_verified is True
    assert payload["summary"]["final_status"] == "needs_review"
    assert payload["summary"]["requires_human_review"] is True
    assert payload["summary"]["retrieval_status"] == "failed"
    assert payload["summary"]["retrieval_coverage"] == "unknown"
    assert payload["system_status"] == "WARN"


def test_non_loopback_evidence_url_is_rejected():
    with pytest.raises(ValueError, match="must target localhost"):
        _command(rag_url="https://example.com")


def test_persistence_readback_mismatch_fails_instead_of_claiming_success():
    class MissingReadbackRepository:
        def get_run(self, _review_run_id):
            return None

        def save_run(self, snapshot):
            return snapshot

    with pytest.raises(RuntimeError, match="readback did not match"):
        execute_review(
            _command(review_run_id="visual-missing-readback"),
            repository=MissingReadbackRepository(),
            retriever=CompleteNoRequirementRetriever(),
        )
