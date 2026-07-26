"""Exercise Backend -> installed Agent wheel over real loopback HTTP.

The server process uses the installed wheel's real ASGI controller and real
``AgentRuntime.review_v3_json``/revision/answer methods. Claude and Data are
replaced only by explicit deterministic conformance adapters, so this Gate
proves package, HTTP, V3 workflow, and Backend-consumer interoperability. It is
not labelled as live Claude or Production Data conformance.
"""

from __future__ import annotations

import argparse
import os
import socket
import subprocess  # nosec B404 - fixed local interpreter and argv only
import time
from datetime import UTC, date, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any


TOKEN = "BackendCrossRepositoryProbe-0123456789-._~+/=Token"  # nosec B105


def _agent_python_executable(path: Path) -> Path:
    # A POSIX virtualenv's ``bin/python`` is commonly a symlink. Resolving that
    # symlink selects the base interpreter and silently drops the virtualenv's
    # installed packages, so only make the caller-provided path absolute.
    executable = Path(os.path.abspath(os.fspath(path)))
    if not executable.is_file():
        raise RuntimeError("installed Agent Python executable is unavailable")
    return executable


def _field(name: str, value: str, source_quote: str | None = None) -> dict[str, str]:
    return {"name": name, "value": value, "source_quote": source_quote or value}


def _serve(*, port: int, expected_git_sha: str, expected_wheel_sha256: str) -> None:
    import uvicorn
    from pydantic import SecretStr

    from customs_ai_agent.adapters.llm import (
        DocumentAnalysis,
        LLMError,
        ModelCallMetadata,
    )
    from customs_ai_agent.adapters.retrieval import MockRegulationRetriever
    from customs_ai_agent.runtime import AgentRuntime
    from customs_ai_agent.schemas.llm import ClaudeDocumentCandidate
    from customs_ai_agent.service.app import create_app
    from customs_ai_agent.service.settings import ServiceSettings

    class DeterministicAnalyzer:
        model_id = "claude-cross-repository-conformance"

        def analyze(self, document: dict[str, Any]) -> DocumentAnalysis:
            if document.get("document_id") != "doc-invoice-http":
                raise AssertionError("conformance request contains an unexpected document")
            line = (
                "ITEM: Frozen mackerel | HS: 0303540000 | QTY: 1000 KG | "
                "UNIT PRICE: 12 | AMOUNT: 12000 | ORIGIN: NO"
            )
            candidate = ClaudeDocumentCandidate.model_validate(
                {
                    "schema_version": "1.0",
                    "document_type": "invoice",
                    "classification_source_quote": "COMMERCIAL INVOICE",
                    "fields": [
                        _field("invoice_no", "INV-HTTP-001"),
                        _field("invoice_date", "2026-07-22"),
                        _field("seller", "Synthetic Exporter AS"),
                        _field("buyer", "Synthetic Importer Co."),
                        _field("currency", "USD"),
                        _field("total_amount", "12000", "TOTAL AMOUNT: 12000"),
                        _field("country_of_origin", "Norway"),
                    ],
                    "line_items": [
                        {
                            "source_quote": line,
                            "fields": [
                                _field("description", "Frozen mackerel"),
                                _field("hs_code", "0303540000"),
                                _field("quantity", "1000 KG"),
                                _field("unit_price", "12"),
                                _field("amount", "12000"),
                                _field("origin_country", "NO"),
                            ],
                        }
                    ],
                }
            )
            return DocumentAnalysis(
                candidate=candidate,
                metadata=ModelCallMetadata(
                    provider="anthropic",
                    model_id=self.model_id,
                    prompt_id="customs_document_analysis",
                    prompt_version="1.2.0",
                    output_schema_id="customs_claude_document_candidate",
                    output_schema_version="1.0",
                    output_schema_sha256="a" * 64,
                    request_id="req_cross_repository_http_001",
                    message_id="msg_cross_repository_http_001",
                    stop_reason="end_turn",
                    input_tokens=100,
                    output_tokens=50,
                    cache_creation_input_tokens=0,
                    cache_read_input_tokens=0,
                    latency_ms=1,
                ),
            )

        def run_bounded_agentic_task(self, **_kwargs: Any) -> Any:
            raise LLMError(
                "deterministic_conformance_adapter",
                "The cross-repository Gate intentionally exercises deterministic fallback.",
            )

        def close(self) -> None:
            return None

    class DeterministicRetriever(MockRegulationRetriever):
        expected_identity = SimpleNamespace(runtime_release_v3_sha256="e" * 64)

        def preflight(self, *_args: Any, **_kwargs: Any) -> object:
            return self.expected_identity

        def policy_bindings(self, _request_id: str | None) -> dict[str, Any]:
            return {}

    runtime = object.__new__(AgentRuntime)
    runtime._analyzer = DeterministicAnalyzer()
    runtime._closed = False
    runtime._config = SimpleNamespace(ocr_layout_provider=None)
    runtime._retriever = None
    runtime._retriever_v3 = DeterministicRetriever()
    runtime._secret_markers = ()
    runtime._assertion_verifier = None
    settings = ServiceSettings(
        bearer_token=SecretStr(TOKEN),
        evidence_base_url="http://127.0.0.1:8010",
        evidence_allowed_origins=frozenset({"http://127.0.0.1:8010"}),
        environment="test",
        build_sha=expected_git_sha,
        wheel_sha256=expected_wheel_sha256,
        host="127.0.0.1",
        port=port,
    )
    app = create_app(
        settings,
        runtime_factory=lambda _settings: runtime,
        readiness_probe_factory=lambda _settings: lambda: object(),
    )
    uvicorn.run(app, host="127.0.0.1", port=port, access_log=False, log_level="error")


def _review_request(run_id: str):
    from customs_backend.domain import AgentDocumentInput, AgentReviewRequest

    content = "\n".join(
        [
            "COMMERCIAL INVOICE",
            "INV-HTTP-001",
            "2026-07-22",
            "Synthetic Exporter AS",
            "Synthetic Importer Co.",
            "USD",
            "TOTAL AMOUNT: 12000",
            "Norway",
            (
                "ITEM: Frozen mackerel | HS: 0303540000 | QTY: 1000 KG | "
                "UNIT PRICE: 12 | AMOUNT: 12000 | ORIGIN: NO"
            ),
        ]
    )
    return AgentReviewRequest(
        review_run_id=run_id,
        case_id="case-cross-repository-http",
        documents=[
            AgentDocumentInput(
                document_id="doc-invoice-http",
                filename="invoice.txt",
                content_text=content,
                declared_type="invoice",
            )
        ],
        review_as_of=date(2026, 7, 22),
        assertion_cutoff_at=datetime(2026, 7, 22, tzinfo=UTC),
        trade_type="import",
        document_language="en",
    )


def _probe(
    *,
    agent_python: Path,
    expected_git_sha: str,
    expected_wheel_sha256: str,
) -> None:
    import httpx

    from customs_backend.adapters import HttpAgentClient
    from customs_backend.domain import (
        AgentHumanDecision,
        AgentReviewResultV3,
        AgentRevisionRequest,
        AgentRevisionResult,
        canonical_digest,
    )
    from customs_backend.release import validate_agent_v3_payload

    with socket.socket() as reservation:
        reservation.bind(("127.0.0.1", 0))
        port = int(reservation.getsockname()[1])
    process = subprocess.Popen(  # nosec B603
        [
            str(_agent_python_executable(agent_python)),
            "-I",
            str(Path(__file__).resolve()),
            "--serve",
            "--port",
            str(port),
            "--expected-git-sha",
            expected_git_sha,
            "--expected-wheel-sha256",
            expected_wheel_sha256,
        ],
        cwd=Path(__file__).resolve().parents[1],
    )
    base_url = f"http://127.0.0.1:{port}"
    client: HttpAgentClient | None = None
    try:
        deadline = time.monotonic() + 20
        while time.monotonic() < deadline:
            if process.poll() is not None:
                raise RuntimeError("installed Agent conformance service exited during startup")
            try:
                if httpx.get(f"{base_url}/health/live", timeout=0.5).status_code == 200:
                    break
            except httpx.HTTPError:
                time.sleep(0.05)
        else:
            raise RuntimeError("installed Agent conformance service did not start")
        client = HttpAgentClient(base_url=base_url, service_token=TOKEN, timeout_seconds=30)
        metadata = client.metadata()
        agent_identity = metadata.get("agent", {})
        if agent_identity.get("package_version") != "0.3.0":
            raise RuntimeError("installed Agent metadata has an unexpected package identity")
        if agent_identity.get("build_sha") != expected_git_sha:
            raise RuntimeError("installed Agent metadata has an unexpected Git identity")
        if agent_identity.get("wheel_sha256") != expected_wheel_sha256:
            raise RuntimeError("installed Agent metadata has an unexpected wheel identity")

        parent_request = _review_request("run-cross-http-parent")
        raw_parent = client.review(parent_request)
        validate_agent_v3_payload("review_result", raw_parent)
        parent = AgentReviewResultV3.model_validate(raw_parent)
        if parent.runtime_release.agent_git_sha != expected_git_sha:
            raise RuntimeError("V3 result does not carry the installed wheel Git identity")
        if len(parent.document_results) != 1 or not parent.audit_trail:
            raise RuntimeError("installed Agent V3 workflow did not execute its full result path")

        question = "이 심사 결과의 근거를 설명해 주세요."
        question_id = f"question_{canonical_digest({'review_run_id': parent.review_run_id, 'provider_result_sha256': parent.result_sha256, 'question': question})}"
        answer = client.answer(
            provider_result=raw_parent,
            question=question,
            question_id=question_id,
        )
        if answer.question_id != question_id:
            raise RuntimeError("installed Agent answer was not bound to the Backend question")

        decision_payload = {
            "decision": "approve_review",
            "actor_id": "cross-repository-approver",
            "actor_role": "approver",
            "decided_at": "2026-07-22T00:00:00Z",
            "reason": "cross-repository conformance approval",
            "confirmed_reference_id": None,
            "correction": None,
            "supporting_document": None,
        }
        decision = AgentHumanDecision(
            decision_id=f"decision_{canonical_digest(decision_payload)}",
            **decision_payload,
        )
        revised_request = parent_request.model_copy(
            update={"review_run_id": "run-cross-http-child"}
        )
        revision_request = AgentRevisionRequest(
            parent_result_sha256=parent.result_sha256,
            parent_input_manifest_sha256=parent.input_manifest_sha256,
            parent_result=raw_parent,
            parent_request=parent_request,
            revised_request=revised_request,
            decisions=[decision],
        )
        revision = AgentRevisionResult.model_validate(client.revise(revision_request))
        if revision.child_result.review_run_id != revised_request.review_run_id:
            raise RuntimeError("installed Agent revision did not produce the bound child Run")
        if revision.applied_decision_ids != [decision.decision_id]:
            raise RuntimeError("installed Agent revision lost its approved decision binding")
        print(
            "installed Agent wheel real-ASGI/full-V3 Backend HTTP conformance PASS "
            "(deterministic Claude/Data adapters; not live-provider conformance)"
        )
    finally:
        if client is not None:
            client.close()
        if process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=5)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--serve", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--port", type=int)
    parser.add_argument("--agent-python", type=Path)
    parser.add_argument("--expected-git-sha", required=True)
    parser.add_argument("--expected-wheel-sha256", required=True)
    args = parser.parse_args()
    if args.serve:
        if args.port is None:
            parser.error("--port is required in server mode")
        _serve(
            port=args.port,
            expected_git_sha=args.expected_git_sha,
            expected_wheel_sha256=args.expected_wheel_sha256,
        )
        return
    if args.agent_python is None:
        parser.error("--agent-python is required in probe mode")
    _probe(
        agent_python=args.agent_python,
        expected_git_sha=args.expected_git_sha,
        expected_wheel_sha256=args.expected_wheel_sha256,
    )


if __name__ == "__main__":
    main()
