from __future__ import annotations

from datetime import date
from io import BytesIO
from urllib.error import HTTPError

import pytest

import integration.review_service as service
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
    assert execution.snapshot.request["llm_mode"] == "mock"


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
        lambda _rag_url, **_kwargs: {
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


@pytest.mark.parametrize(
    "rag_url",
    [
        "http://user:password@127.0.0.1:8010",
        "http://127.0.0.1:8010?token=secret",
        "http://127.0.0.1:8010/internal/rag/v1",
    ],
)
def test_evidence_url_rejects_credentials_query_and_application_path(rag_url):
    with pytest.raises(ValueError, match="rag_url must not contain"):
        _command(rag_url=rag_url)


def test_service_token_is_excluded_from_command_dump_and_repr():
    token = "do-not-serialize-this-token"
    command = _command(evidence_service_token=token)

    assert command.evidence_service_token is not None
    assert command.evidence_service_token.get_secret_value() == token
    assert "evidence_service_token" not in command.model_dump()
    assert token not in repr(command)


def test_operational_dsn_is_excluded_from_command_dump_and_repr():
    dsn = "postgresql://user:do-not-print@127.0.0.1:55432/operations"
    command = _command(ops_dsn=dsn)

    assert command.ops_dsn == dsn
    assert "ops_dsn" not in command.model_dump()
    assert dsn not in repr(command)


def test_readiness_sends_service_token_in_header_only(monkeypatch):
    captured = {}

    class Response:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def read(self):
            return b'{"status":"ready","supported_review_as_of":"2026-07-10"}'

    def fake_urlopen(request, timeout):
        captured["request"] = request
        captured["timeout"] = timeout
        return Response()

    monkeypatch.setattr(service, "urlopen", fake_urlopen)

    payload = service.fetch_readiness(
        "http://127.0.0.1:8010", service_token="header-only-token"
    )

    request = captured["request"]
    assert payload["status"] == "ready"
    assert request.get_header("X-demo-token") == "header-only-token"
    assert request.full_url.endswith("/internal/rag/v1/health/ready")
    assert "header-only-token" not in request.full_url


def test_readiness_error_never_echoes_response_body_or_token(monkeypatch):
    token = "never-echo-this-token"

    def failed_urlopen(request, timeout):
        del request, timeout
        raise HTTPError(
            "http://127.0.0.1:8010/internal/rag/v1/health/ready",
            401,
            "Unauthorized",
            {},
            BytesIO(f'{{"detail":"{token}"}}'.encode()),
        )

    monkeypatch.setattr(service, "urlopen", failed_urlopen)

    with pytest.raises(service.ReadinessError) as exc_info:
        service.fetch_readiness(
            "http://127.0.0.1:8010",
            service_token=token,
        )

    assert str(exc_info.value) == "Evidence readiness HTTP 401"
    assert token not in str(exc_info.value)


def test_token_requires_compatible_agent_retriever_and_does_not_leak(monkeypatch):
    token = "not-in-error"

    class LegacyRetriever:
        def __init__(self, base_url):
            self.base_url = base_url

    monkeypatch.setattr(service, "HttpRegulationRetriever", LegacyRetriever)

    with pytest.raises(RuntimeError, match="compatible Agent wheel") as exc_info:
        service._http_retriever("http://127.0.0.1:8010", token)

    assert token not in str(exc_info.value)


def test_future_additive_agent_token_api_is_used_without_source_coupling(monkeypatch):
    class TokenAwareRetriever:
        def __init__(self, base_url, *, service_token=None):
            self.base_url = base_url
            self.service_token = service_token

    monkeypatch.setattr(service, "HttpRegulationRetriever", TokenAwareRetriever)

    retriever = service._http_retriever("http://127.0.0.1:8010", "future-token")

    assert retriever.base_url == "http://127.0.0.1:8010"
    assert retriever.service_token == "future-token"


def test_llm_mode_remains_fail_fast_before_provider_implementation(monkeypatch):
    monkeypatch.delenv("CUSTOMS_AGENT_ENABLE_LLM", raising=False)

    with pytest.raises(RuntimeError, match="CUSTOMS_AGENT_ENABLE_LLM is not set"):
        execute_review(
            _command(review_run_id="visual-llm-not-implemented", llm_mode="llm", store=False),
            retriever=CompleteNoRequirementRetriever(),
        )


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
