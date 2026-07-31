"""Exercise Backend -> installed Agent wheel over real loopback HTTP.

The server process uses the installed wheel's real ASGI controller and real
``AgentRuntime.review_v3_json``/revision/answer methods. Claude and Data are
replaced only by explicit deterministic conformance adapters, so this Gate
proves package, HTTP, V3 workflow, and Backend-consumer interoperability. It is
not labelled as live Claude or Production Data conformance.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import socket
import subprocess  # nosec B404 - fixed local interpreter and argv only
import sys
import time
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from urllib.parse import urlsplit
from urllib.request import url2pathname


TOKEN = "BackendCrossRepositoryProbe-0123456789-._~+/=Token"  # nosec B105
AGENT_DISTRIBUTION = "customs-ai-agent"
AGENT_PACKAGE_VERSION = "0.3.0"
MODEL_ID = "claude-cross-repository-conformance"


def _canonical_json_bytes(value: object) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        allow_nan=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")


def _canonical_sha256(value: object) -> str:
    return hashlib.sha256(_canonical_json_bytes(value)).hexdigest()


def _git_output(checkout: Path, *arguments: str) -> str:
    completed = subprocess.run(  # nosec B603
        ["git", "-C", str(checkout), *arguments],
        check=True,
        capture_output=True,
        text=True,
        timeout=20,
    )
    return completed.stdout.strip()


def _assert_clean_exact_agent_checkout(
    checkout: Path,
    *,
    expected_git_sha: str,
) -> Path:
    resolved = checkout.expanduser().resolve(strict=True)
    if not resolved.is_dir():
        raise RuntimeError("Agent checkout is not a directory")
    if _git_output(resolved, "rev-parse", "HEAD") != expected_git_sha:
        raise RuntimeError("Agent checkout does not match the expected exact Git SHA")
    if _git_output(resolved, "status", "--porcelain=v1", "--untracked-files=all"):
        raise RuntimeError("Agent checkout is not clean")
    tracked_tool = "tools/local_uat_agent_service/app.py"
    if _git_output(resolved, "ls-files", "--error-unmatch", "--", tracked_tool) != tracked_tool:
        raise RuntimeError("Agent checkout does not contain the tracked UAT composition root")
    return resolved


def _direct_url_sha256(distribution: Any, *, expected_wheel_sha256: str) -> str:
    direct_url_text = distribution.read_text("direct_url.json")
    if not direct_url_text:
        raise RuntimeError("installed Agent distribution has no wheel origin record")
    try:
        direct_url = json.loads(direct_url_text)
    except (json.JSONDecodeError, UnicodeError):
        raise RuntimeError("installed Agent wheel origin record is invalid") from None
    if (
        not isinstance(direct_url, dict)
        or direct_url.get("dir_info") is not None
        or direct_url.get("vcs_info") is not None
    ):
        raise RuntimeError("installed Agent was not installed from an immutable wheel archive")
    archive_info = direct_url.get("archive_info")
    if not isinstance(archive_info, dict):
        raise RuntimeError("installed Agent wheel archive identity is missing")
    observed: set[str] = set()
    raw_hash = archive_info.get("hash")
    if isinstance(raw_hash, str) and raw_hash.startswith("sha256="):
        observed.add(raw_hash.removeprefix("sha256=").casefold())
    raw_hashes = archive_info.get("hashes")
    if isinstance(raw_hashes, dict) and isinstance(raw_hashes.get("sha256"), str):
        observed.add(raw_hashes["sha256"].casefold())
    if observed and observed != {expected_wheel_sha256}:
        raise RuntimeError("installed Agent wheel origin SHA-256 is inconsistent")
    raw_url = direct_url.get("url")
    if not isinstance(raw_url, str):
        raise RuntimeError("installed Agent wheel archive URL is missing")
    parsed = urlsplit(raw_url)
    if (
        parsed.scheme != "file"
        or parsed.netloc
        or parsed.query
        or parsed.fragment
        or not parsed.path
    ):
        raise RuntimeError("installed Agent wheel origin is not one local file")
    wheel_path = Path(url2pathname(parsed.path)).resolve(strict=True)
    if not wheel_path.is_file():
        raise RuntimeError("installed Agent wheel origin file is unavailable")
    digest = hashlib.sha256()
    with wheel_path.open("rb") as wheel_file:
        for block in iter(lambda: wheel_file.read(1024 * 1024), b""):
            digest.update(block)
    actual = digest.hexdigest()
    if actual != expected_wheel_sha256:
        raise RuntimeError("installed Agent wheel origin bytes have an unexpected SHA-256")
    return actual


def _assert_installed_agent_distribution(
    *,
    agent_checkout: Path,
    expected_git_sha: str,
    expected_wheel_sha256: str,
) -> None:
    from importlib.metadata import PackageNotFoundError, distribution

    try:
        installed_distribution = distribution(AGENT_DISTRIBUTION)
    except PackageNotFoundError:
        raise RuntimeError("installed Agent distribution is unavailable") from None
    if installed_distribution.version != AGENT_PACKAGE_VERSION:
        raise RuntimeError("installed Agent distribution version is unexpected")

    import customs_ai_agent
    from customs_ai_agent.build_info import load_packaged_build_manifest

    package_file = getattr(customs_ai_agent, "__file__", None)
    if not isinstance(package_file, str):
        raise RuntimeError("installed Agent package has no concrete file origin")
    package_origin = Path(package_file).resolve(strict=True)
    distribution_root = Path(installed_distribution.locate_file("")).resolve(strict=True)
    interpreter_root = Path(sys.prefix).resolve(strict=True)
    if (
        not package_origin.is_relative_to(distribution_root)
        or not package_origin.is_relative_to(interpreter_root)
        or package_origin.is_relative_to(agent_checkout)
    ):
        raise RuntimeError("Agent package was imported outside the isolated installed runtime")

    manifest = load_packaged_build_manifest(required=True)
    if manifest is None or manifest.git_sha != expected_git_sha:
        raise RuntimeError("installed Agent build manifest Git identity is unexpected")
    if (
        _direct_url_sha256(
            installed_distribution,
            expected_wheel_sha256=expected_wheel_sha256,
        )
        != expected_wheel_sha256
    ):
        raise RuntimeError("installed Agent wheel SHA-256 differs from the expected artifact")


def _assert_agent_modules_remain_installed(
    *,
    agent_checkout: Path,
) -> None:
    interpreter_root = Path(sys.prefix).resolve(strict=True)
    for name, module in tuple(sys.modules.items()):
        if name != "customs_ai_agent" and not name.startswith("customs_ai_agent."):
            continue
        module_file = getattr(module, "__file__", None)
        if module_file is None:
            continue
        origin = Path(module_file).resolve(strict=True)
        if origin.is_relative_to(agent_checkout) or not origin.is_relative_to(interpreter_root):
            raise RuntimeError("Agent checkout contaminated the installed Agent module graph")


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


def _serve(
    *,
    port: int,
    agent_checkout: Path,
    expected_git_sha: str,
    expected_wheel_sha256: str,
) -> None:
    agent_checkout = _assert_clean_exact_agent_checkout(
        agent_checkout,
        expected_git_sha=expected_git_sha,
    )
    _assert_installed_agent_distribution(
        agent_checkout=agent_checkout,
        expected_git_sha=expected_git_sha,
        expected_wheel_sha256=expected_wheel_sha256,
    )

    import uvicorn
    from pydantic import SecretStr

    from customs_ai_agent.adapters.llm import (
        DocumentAnalysis,
        LLMError,
        ModelCallMetadata,
        StructuredAgenticResult,
        anthropic_output_schema_sha256,
    )
    from customs_ai_agent.adapters.retrieval import MockRegulationRetriever
    from customs_ai_agent.assertion_trust import (
        ASSERTION_TRUST_DOMAIN,
        AssertionTrustStoreV1,
        SupportingDocumentAssertionVerifier,
        TrustedAssertionKeyV1,
    )
    from customs_ai_agent.contracts.evidence_runtime_release import (
        EvidenceRuntimeReleaseIdentity,
        canonical_json_bytes,
    )
    from customs_ai_agent.contracts.evidence_v3 import (
        EvidenceRuntimeReleaseIdentityV3,
        packaged_evidence_v3_schema_hashes,
    )
    from customs_ai_agent.runtime import AgentRuntime
    from customs_ai_agent.schemas.llm import ClaudeDocumentCandidate
    from customs_ai_agent.schemas.v3 import canonical_json_sha256
    from customs_ai_agent.service.settings import ServiceSettings
    from customs_ai_agent.uat_capability import issue_uat_capability

    # Only the official, tracked UAT composition root is sourced from the exact
    # clean checkout. All ``customs_ai_agent`` imports above and below must stay
    # bound to the independently installed wheel.
    sys.path.insert(0, str(agent_checkout))
    try:
        from tools.local_uat_agent_service.app import (
            LocalUatDiagnosticRuntime,
            LocalUatManifest,
            _compose_local_uat_app,
        )
    finally:
        sys.path.remove(str(agent_checkout))
    composition_module = sys.modules["tools.local_uat_agent_service.app"]
    composition_file = Path(str(composition_module.__file__)).resolve(strict=True)
    if not composition_file.is_relative_to(agent_checkout / "tools"):
        raise RuntimeError("UAT composition root was not loaded from the exact Agent checkout")
    _assert_agent_modules_remain_installed(agent_checkout=agent_checkout)

    base_identity_payload: dict[str, Any] = {
        "contract": "evidence-runtime-release",
        "schema_version": "2.0",
        "retrieval_schema_version": "1.0",
        "provider_release": "evidence-api-v1.2.0",
        "provider_revision": "1" * 40,
        "provider_build_sha256": "2" * 64,
        "retrieval_schema_sha256": "3" * 64,
        "provider_source_manifest_sha256": "4" * 64,
        "knowledge_generation_id": "5" * 64,
        "knowledge_generation_attestation_sha256": "6" * 64,
        "knowledge_source_manifest_sha256": "7" * 64,
        "model_manifest_sha256": "8" * 64,
        "index_config_sha256": "9" * 64,
        "evaluation_report_sha256": "a" * 64,
        "seal_attestation_sha256": "b" * 64,
        "knowledge_snapshot_id": "knowledge-cross-repository-http",
        "supported_review_as_of": "2026-07-22",
    }
    base_identity_payload["runtime_release_sha256"] = hashlib.sha256(
        canonical_json_bytes(base_identity_payload)
    ).hexdigest()
    base_identity = EvidenceRuntimeReleaseIdentity.from_mapping(base_identity_payload)
    schema_hashes = packaged_evidence_v3_schema_hashes()
    v3_identity_payload: dict[str, Any] = {
        "contract": "evidence-runtime-release",
        "schema_version": "3.0",
        "retrieval_schema_version": "3.0",
        "base_runtime_release": base_identity.to_wire(),
        "retrieval_schema_sha256": schema_hashes["rag_retrieval_v3.schema.json"],
        "readiness_schema_sha256": schema_hashes["readiness_v3.schema.json"],
        "requirement_policy_schema_sha256": schema_hashes["requirement_policy_v1.schema.json"],
        "requirement_policy_release": ("test-only-three-law-scenario-eval-human-only.v1"),
        "requirement_policy_manifest_sha256": (
            "2467226fe64c84f8fdc0b41a8ab44ebaba04e6ba7280cd3e4bd56bd2e9c38c95"
        ),
        "coverage_matrix_sha256": (
            "21012116b4fddf0ae0e0bb12343558aca472373480d32d535e6b59875800c444"
        ),
        "requirement_policy_approval_sha256": "c" * 64,
        "reviewer_trust_store_sha256": (
            "7864482157b701b44c457bd35c7721b31ffc31564ea43d83948aa27e08233637"
        ),
    }
    v3_identity_payload["runtime_release_v3_sha256"] = hashlib.sha256(
        canonical_json_bytes(v3_identity_payload)
    ).hexdigest()
    evidence_identity = EvidenceRuntimeReleaseIdentityV3.from_mapping(v3_identity_payload)

    public_key = bytes(range(32))
    import base64

    trust_key = TrustedAssertionKeyV1(
        authority_id=(
            "backend:principal-sha256:"
            "6cc4551495f1ec8da505fdeaa4f34c17eeb8c234bcaeb6ca184f92a0cdd6318d"
        ),
        authority_release="backend-human-decision-policy/v1",
        method="human_approver",
        key_id="local-uat-cross-repository-http",
        public_key_base64url=base64.urlsafe_b64encode(public_key).decode("ascii").rstrip("="),
        valid_from=date(2020, 1, 1),
        valid_until=date(2035, 1, 1),
        status="active",
    )
    trust_store_payload = {
        "contract": "customs-ai-agent-assertion-trust-store",
        "schema_version": "1.0",
        "release_state": "active",
        "trust_domain": ASSERTION_TRUST_DOMAIN,
        "keys": [trust_key.model_dump(mode="json")],
    }
    trust_store = AssertionTrustStoreV1(
        **trust_store_payload,
        trust_store_sha256=_canonical_sha256(trust_store_payload),
    )
    issued_at = datetime.now(UTC)
    capability = issue_uat_capability(
        agent_commit=expected_git_sha,
        model_id=MODEL_ID,
        evidence_runtime_v3_sha256=evidence_identity.runtime_release_v3_sha256,
        issued_at=issued_at,
        expires_at=issued_at + timedelta(hours=1),
        adaptive_active=True,
    )
    capability_file_sha256 = hashlib.sha256(
        _canonical_json_bytes(capability.model_dump(mode="json"))
    ).hexdigest()

    class DeterministicAnalyzer:
        model_id = MODEL_ID
        _agentic_call_count = 0
        _fallback_report_failures_remaining = 0

        def analyze(self, document: dict[str, Any]) -> DocumentAnalysis:
            document_id = document.get("document_id")
            if document_id == "doc-bl-http":
                candidate_payload = {
                    "schema_version": "1.0",
                    "document_type": "bill_of_lading",
                    "classification_source_quote": "BILL OF LADING",
                    "fields": [
                        _field("bl_no", "BL-HTTP-001"),
                        _field("shipper", "Synthetic Exporter AS"),
                        _field("consignee", "Synthetic Importer Co."),
                        _field("vessel", "PACIFIC STAR"),
                        _field("voyage_no", "PS2607"),
                        _field("port_of_loading", "OSLO"),
                        _field("port_of_discharge", "BUSAN"),
                        _field("container_numbers", "MSKU1234565"),
                        _field("seal_numbers", "SL12345"),
                        _field("packages", "100 CTNS"),
                        _field("gross_weight_kg", "1200 KG"),
                        _field("measurement_cbm", "20 CBM"),
                        _field("freight_prepaid", "Y"),
                    ],
                    "line_items": [],
                }
            elif document_id in {"doc-invoice-http", "doc-invoice-fallback-http"}:
                if document_id == "doc-invoice-fallback-http":
                    self._fallback_report_failures_remaining = 2
                line = (
                    "ITEM: Stainless bolts | HS: 7318.15 | QTY: 1200 KG | "
                    "UNIT PRICE: 10 | AMOUNT: 12000 | ORIGIN: NO"
                )
                candidate_payload = {
                    "schema_version": "1.0",
                    "document_type": "invoice",
                    "classification_source_quote": "COMMERCIAL INVOICE",
                    "fields": [
                        _field("invoice_no", "INV-HTTP-001"),
                        _field("invoice_date", "2026.07.22"),
                        _field("seller", "Synthetic Exporter AS"),
                        _field("buyer", "Synthetic Importer Co."),
                        _field("incoterms", "CIF"),
                        _field("currency", "USD"),
                        _field("total_amount", "12000", "TOTAL AMOUNT: 12000"),
                        _field("departure_date", "2026.08.15"),
                        _field("vessel", "PACIFIC STAR"),
                        _field("voyage_no", "PS2607"),
                        _field("port_of_loading", "OSLO"),
                        _field("port_of_discharge", "BUSAN"),
                        _field("country_of_origin", "Norway"),
                        _field("shipping_marks", "SYNTHETIC-MARK-001"),
                        _field("packages", "100 CTNS"),
                    ],
                    "line_items": [
                        {
                            "source_quote": line,
                            "fields": [
                                _field("description", "Stainless bolts"),
                                _field("hs_code", "7318.15"),
                                _field("quantity", "1200 KG"),
                                _field("unit_price", "10"),
                                _field("amount", "12000"),
                                _field("origin_country", "NO"),
                            ],
                        }
                    ],
                }
            elif document_id == "doc-pl-http":
                line = (
                    "ITEM: Stainless bolts | QTY: 1200 KG | NET WEIGHT: 1100 KG | "
                    "GROSS WEIGHT: 1200 KG | PACKAGE COUNT: 100"
                )
                candidate_payload = {
                    "schema_version": "1.0",
                    "document_type": "packing_list",
                    "classification_source_quote": "PACKING LIST",
                    "fields": [
                        _field("packing_no", "PL-HTTP-001"),
                        _field("invoice_no", "INV-HTTP-001"),
                        _field("invoice_date", "2026.07.22"),
                        _field("bl_no", "BL-HTTP-001"),
                        _field("seller", "Synthetic Exporter AS"),
                        _field("buyer", "Synthetic Importer Co."),
                        _field("departure_date", "2026.08.15"),
                        _field("vessel", "PACIFIC STAR"),
                        _field("voyage_no", "PS2607"),
                        _field("port_of_loading", "OSLO"),
                        _field("port_of_discharge", "BUSAN"),
                        _field("country_of_origin", "Norway"),
                        _field("shipping_marks", "SYNTHETIC-MARK-001"),
                        _field("container_numbers", "MSKU1234565"),
                        _field("seal_numbers", "SL12345"),
                        _field("packages", "100 CTNS"),
                        _field(
                            "total_gross_weight_kg",
                            "1200 KG",
                            "TOTAL GROSS WEIGHT: 1200 KG",
                        ),
                        _field("total_measurement_cbm", "20 CBM"),
                    ],
                    "line_items": [
                        {
                            "source_quote": line,
                            "fields": [
                                _field("description", "Stainless bolts"),
                                _field("quantity", "1200 KG"),
                                _field("net_weight_kg", "1100 KG"),
                                _field("gross_weight_kg", "1200 KG"),
                                _field("package_count", "100"),
                            ],
                        }
                    ],
                }
            elif document_id == "doc-co-http":
                candidate_payload = {
                    "schema_version": "1.0",
                    "document_type": "certificate_of_origin",
                    "classification_source_quote": "CERTIFICATE OF ORIGIN",
                    "fields": [
                        _field("certificate_no", "CO-HTTP-001"),
                        _field("exporter", "Synthetic Exporter AS"),
                        _field("importer", "Synthetic Importer Co."),
                        _field("origin_country", "Norway"),
                        _field("hs_codes", "7318.15"),
                        _field("goods_description", "Stainless bolts"),
                        _field("issue_date", "Jul 22 2026"),
                    ],
                    "line_items": [],
                }
            else:
                raise AssertionError("conformance request contains an unexpected document")
            candidate = ClaudeDocumentCandidate.model_validate(candidate_payload)
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
                    request_id=f"req_cross_repository_http_{document_id}",
                    message_id=f"msg_cross_repository_http_{document_id}",
                    stop_reason="end_turn",
                    input_tokens=100,
                    output_tokens=50,
                    cache_creation_input_tokens=0,
                    cache_read_input_tokens=0,
                    latency_ms=1,
                ),
            )

        def run_bounded_agentic_task(self, **kwargs: Any) -> StructuredAgenticResult:
            """Return schema-valid deterministic model results for the HTTP Gate.

            The probe is deliberately not a live-Claude evaluation.  It does,
            however, exercise the same successful presentation, entailment,
            scope-classification, answer, and answer-verification branches that
            a provider response traverses in the installed Agent wheel.
            """

            self._agentic_call_count += 1
            task_id = kwargs["task_id"]
            untrusted_context = kwargs["untrusted_context"]
            schema = kwargs["output_schema"]
            if task_id == "customs_experience_report":
                if self._fallback_report_failures_remaining:
                    self._fallback_report_failures_remaining -= 1
                    raise LLMError(
                        "deterministic_presentation_unavailable",
                        "The fallback control rejects both bounded presentation calls.",
                    )
                if not untrusted_context.get("previous_verification_failures"):
                    # A schema-valid but semantically incomplete first response
                    # proves that the wire accepts the Agent's bounded regeneration.
                    value = {"statements": []}
                else:
                    value = {
                        "statements": [
                            {
                                "section_code": group["section_code"],
                                "role": group["role"],
                                "text": (
                                    "담당자가 결과를 이해하기 쉽도록 확인 내용을 설명하면, "
                                    + " ".join(
                                        claim["canonical_text"]
                                        for claim in group["canonical_claims"]
                                    )
                                ),
                                "source_claim_ids": group["source_claim_ids"],
                            }
                            for group in untrusted_context["required_claim_groups"]
                        ]
                    }
            elif task_id == "customs_experience_entailment":
                value = {
                    "verdicts": [
                        {
                            "statement_id": statement["statement_id"],
                            "supported": True,
                            "reason_code": "supported",
                        }
                        for statement in untrusted_context["candidate_statements"]
                    ]
                }
            elif task_id == "customs_case_scope_classifier":
                value = {
                    "answer_scope": "case_grounded",
                    "reason_code": "semantic_intent_classified",
                    "guidance_topics": [],
                }
            elif task_id == "customs_case_answer_v2":
                if not untrusted_context.get("previous_verification_failures"):
                    value = {
                        "direct_answer": "확인 중입니다.",
                        "explanation": [],
                        "evidence_meaning": [],
                        "impact": [],
                        "next_steps": [],
                        "limitations": [],
                        "claim_selections": {
                            "direct_answer": [],
                            "explanation": [],
                            "evidence_meaning": [],
                            "impact": [],
                            "next_steps": [],
                        },
                        "citation_references": [],
                        "suggested_questions": [],
                        "used_context_message_ids": [],
                    }
                else:
                    claim = next(
                        claim
                        for claim in untrusted_context["allowed_claims"]
                        if claim["section_code"] == "conclusion"
                    )
                    value = {
                        "direct_answer": (
                            "질문하신 결론을 먼저 이해하기 쉽게 설명하면, "
                            + claim["canonical_text"]
                        ),
                        "explanation": [],
                        "evidence_meaning": [],
                        "impact": [],
                        "next_steps": [],
                        "limitations": [
                            "이 설명은 현재 심사 회차에 연결된 문서와 법령 근거 안에서만 유효합니다."
                        ],
                        "claim_selections": {
                            "direct_answer": [claim["claim_id"]],
                            "explanation": [],
                            "evidence_meaning": [],
                            "impact": [],
                            "next_steps": [],
                        },
                        "citation_references": claim["citation_references"],
                        "suggested_questions": [],
                        "used_context_message_ids": [],
                    }
            elif task_id == "customs_case_answer_verifier":
                value = {"supported": True, "reason_codes": []}
            else:
                # Coordinator/narrative tasks belong to the canonical v2/v3
                # review workflow and intentionally take their production
                # fail-closed fallback in this deterministic provider Gate.
                raise LLMError(
                    "deterministic_conformance_adapter",
                    f"No deterministic provider result is configured for {task_id}.",
                )
            return StructuredAgenticResult(
                value=value,
                model_id=self.model_id,
                prompt_sha256=hashlib.sha256(kwargs["system_prompt"].encode("utf-8")).hexdigest(),
                output_schema_sha256=anthropic_output_schema_sha256(schema),
                semantic_output_schema_sha256=canonical_json_sha256(schema),
                request_id=f"req-cross-http-{self._agentic_call_count:03d}",
                message_id=f"msg-cross-http-{self._agentic_call_count:03d}",
                input_tokens=100,
                output_tokens=50,
                latency_ms=1,
            )

        def close(self) -> None:
            return None

    class DeterministicRetriever(MockRegulationRetriever):
        expected_identity = evidence_identity

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
    runtime._assertion_verifier = SupportingDocumentAssertionVerifier(trust_store)
    runtime._uat_expected_agent_commit = expected_git_sha
    runtime._uat_capability = capability
    uat_runtime = LocalUatDiagnosticRuntime(
        runtime,
        runtime._retriever_v3,
        model_id=MODEL_ID,
    )
    settings = ServiceSettings(
        bearer_token=SecretStr(TOKEN),
        evidence_base_url="http://127.0.0.1:8010",
        evidence_allowed_origins=frozenset({"http://127.0.0.1:8010"}),
        environment="test",
        build_sha=expected_git_sha,
        wheel_sha256=expected_wheel_sha256,
        host="127.0.0.1",
        port=port,
        max_concurrency=1,
        enhanced_agent_experience=True,
        uat_adaptive_active=True,
    )
    manifest_payload = {
        "evidence_mode": "preserved-3law-v3-scenario-eval",
        "evidence_base_url": settings.evidence_base_url,
        "evidence_runtime_identity": evidence_identity.to_wire(),
        "assertion_fixture_mode": "LOOPBACK_TEST_ONLY_HUMAN_APPROVER",
        "assertion_trust_store_sha256": trust_store.trust_store_sha256,
        "enhanced_experience_enabled": True,
        "uat_adaptive_active": True,
        "uat_capability_profile_sha256": capability_file_sha256,
        "agent_commit": expected_git_sha,
    }
    manifest = LocalUatManifest(
        evidence_mode="preserved-3law-v3-scenario-eval",
        evidence_base_url=settings.evidence_base_url,
        evidence_runtime_identity=evidence_identity,
        assertion_fixture_mode="LOOPBACK_TEST_ONLY_HUMAN_APPROVER",
        assertion_trust_store=trust_store,
        manifest_sha256=_canonical_sha256(manifest_payload),
        enhanced_experience_enabled=True,
        uat_adaptive_active=True,
        uat_capability_profile_path=None,
        uat_capability_profile_sha256=capability_file_sha256,
        agent_commit=expected_git_sha,
    )
    app = _compose_local_uat_app(
        manifest=manifest,
        settings=settings,
        acknowledge_local_uat=True,
        runtime=uat_runtime,
        readiness_probe=uat_runtime.preflight,
        model_id=MODEL_ID,
    )
    uvicorn.run(app, host="127.0.0.1", port=port, access_log=False, log_level="error")


def _review_request(run_id: str):
    from customs_backend.domain import AgentDocumentInput, AgentReviewRequest

    shared = [
        "INV-HTTP-001",
        "2026.07.22",
        "Synthetic Exporter AS",
        "Synthetic Importer Co.",
        "PACIFIC STAR",
        "PS2607",
        "OSLO",
        "BUSAN",
        "Norway",
        "SYNTHETIC-MARK-001",
        "100 CTNS",
    ]
    documents = [
        AgentDocumentInput(
            document_id="doc-bl-http",
            filename="bill-of-lading.txt",
            content_text="\n".join(
                [
                    "BILL OF LADING",
                    "BL-HTTP-001",
                    *shared[2:8],
                    "MSKU1234565",
                    "SL12345",
                    "100 CTNS",
                    "1200 KG",
                    "20 CBM",
                    "Y",
                ]
            ),
            declared_type="bill_of_lading",
        ),
        AgentDocumentInput(
            document_id="doc-invoice-http",
            filename="invoice.txt",
            content_text="\n".join(
                [
                    "COMMERCIAL INVOICE",
                    *shared,
                    "CIF",
                    "USD",
                    "TOTAL AMOUNT: 12000",
                    "2026.08.15",
                    (
                        "ITEM: Stainless bolts | HS: 7318.15 | QTY: 1200 KG | "
                        "UNIT PRICE: 10 | AMOUNT: 12000 | ORIGIN: NO"
                    ),
                ]
            ),
            declared_type="invoice",
        ),
        AgentDocumentInput(
            document_id="doc-pl-http",
            filename="packing-list.txt",
            content_text="\n".join(
                [
                    "PACKING LIST",
                    "PL-HTTP-001",
                    "BL-HTTP-001",
                    *shared,
                    "2026.08.15",
                    "MSKU1234565",
                    "SL12345",
                    "TOTAL GROSS WEIGHT: 1200 KG",
                    "20 CBM",
                    (
                        "ITEM: Stainless bolts | QTY: 1200 KG | NET WEIGHT: 1100 KG | "
                        "GROSS WEIGHT: 1200 KG | PACKAGE COUNT: 100"
                    ),
                ]
            ),
            declared_type="packing_list",
        ),
        AgentDocumentInput(
            document_id="doc-co-http",
            filename="certificate-of-origin.txt",
            content_text="\n".join(
                [
                    "CERTIFICATE OF ORIGIN",
                    "CO-HTTP-001",
                    "Synthetic Exporter AS",
                    "Synthetic Importer Co.",
                    "Norway",
                    "7318.15",
                    "Stainless bolts",
                    "Jul 22 2026",
                ]
            ),
            declared_type="certificate_of_origin",
        ),
    ]
    return AgentReviewRequest(
        review_run_id=run_id,
        case_id="case-cross-repository-http",
        documents=documents,
        review_as_of=date(2026, 7, 22),
        assertion_cutoff_at=datetime(2026, 7, 22, tzinfo=UTC),
        trade_type="import",
        document_language="en",
    )


def _missing_document_fallback_request(run_id: str):
    base = _review_request(run_id)
    invoice = next(document for document in base.documents if document.declared_type == "invoice")
    return base.model_copy(
        update={
            "documents": [invoice.model_copy(update={"document_id": "doc-invoice-fallback-http"})]
        }
    )


def _probe(
    *,
    agent_python: Path,
    agent_checkout: Path,
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
    from customs_backend.experience import (
        AgentReviewStageEventV1,
        AgentUatCapabilityAttestationV1,
        AssistantAnswerV2,
        CaseConversationRequestV2,
        ReviewExperienceEnvelopeV1,
        validate_answer_against_context,
        validate_presentation_against_result,
    )
    from customs_backend.experience_contract import (
        validate_agent_experience_contract_bundle,
        validate_agent_experience_payload,
    )
    from customs_backend.release import validate_agent_v3_payload
    from customs_backend.service import _OrderedAgentStageStream

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
            "--agent-checkout",
            str(agent_checkout),
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
        deadline = time.monotonic() + 60
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
        if {str(item["document_id"]) for item in parent.document_results} != {
            item.document_id for item in parent_request.documents
        } or not parent.audit_trail:
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

        contract = validate_agent_experience_contract_bundle().lock
        experience_metadata = client.experience_metadata()
        if experience_metadata.get("service_api_version") != "internal-agent-service/v3":
            raise RuntimeError("installed Agent V3 metadata service identity is unexpected")
        experience_agent = experience_metadata.get("agent")
        if not isinstance(experience_agent, dict) or (
            experience_agent.get("package_version") != AGENT_PACKAGE_VERSION
            or experience_agent.get("build_sha") != expected_git_sha
            or experience_agent.get("wheel_sha256") != expected_wheel_sha256
        ):
            raise RuntimeError("installed Agent V3 metadata artifact identity is unexpected")
        schemas = experience_metadata.get("schemas")
        if (
            not isinstance(schemas, dict)
            or schemas.get("service_openapi_sha256") != contract.provider_openapi_sha256
        ):
            raise RuntimeError("installed Agent V3 metadata OpenAPI identity is unexpected")
        limits = experience_metadata.get("agentic_limits")
        if (
            not isinstance(limits, dict)
            or limits.get("planner_mode") != "active"
            or limits.get("effective_execution_mode") != "uat_active"
            or limits.get("uat_adaptive_active") is not True
            or limits.get("active_execution_authorized_by_release") is not False
        ):
            raise RuntimeError(
                "installed Agent V3 metadata does not attest bounded UAT active mode"
            )
        if (
            experience_metadata.get("enhanced_experience_enabled") is not True
            or experience_metadata.get("uat_capability_status") != "valid"
            or experience_metadata.get("agent_commit") != expected_git_sha
        ):
            raise RuntimeError("installed Agent V3 metadata capability state is unexpected")
        capability_payload = experience_metadata.get("uat_capability_attestation")
        validate_agent_experience_payload("agent-uat-capability-v1", capability_payload)
        metadata_capability = AgentUatCapabilityAttestationV1.model_validate(capability_payload)
        if (
            metadata_capability.agent_commit != expected_git_sha
            or metadata_capability.model_id != MODEL_ID
            or metadata_capability.adaptive_active is not True
            or metadata_capability.auto_completion_enabled is not False
            or metadata_capability.capability_sha256
            != experience_metadata.get("uat_capability_sha256")
        ):
            raise RuntimeError("installed Agent V3 capability is not bound to the probe runtime")

        readiness = client.uat_readiness()
        if (
            readiness.get("status") != "uat-ready"
            or readiness.get("diagnostic_only") is not True
            or readiness.get("text_only") is not True
            or readiness.get("mock_fallback_allowed") is not False
            or readiness.get("auto_completion_allowed") is not False
            or readiness.get("evidence_mode") != "preserved-3law-v3-scenario-eval"
            or readiness.get("enhanced_experience_enabled") is not True
            or readiness.get("uat_adaptive_active") is not True
            or readiness.get("uat_capability_status") != "valid"
            or readiness.get("uat_capability_sha256") != metadata_capability.capability_sha256
            or readiness.get("uat_capability_file_sha256")
            != experience_metadata.get("uat_capability_file_sha256")
            or readiness.get("uat_capability_attestation")
            != metadata_capability.model_dump(mode="json")
        ):
            raise RuntimeError("installed Agent UAT readiness is not bound to V3 metadata")

        review_stream = _OrderedAgentStageStream(
            stages=(
                "request_validation",
                "document_analysis",
                "normalization",
                "document_pairing",
                "cross_validation",
                "evidence_retrieval",
                "compliance_review",
                "adaptive_investigation",
                "report_composition",
                "grounding_verification",
                "result_validation",
            ),
            skippable_stages=frozenset({"adaptive_investigation"}),
            retry_groups=(("report_composition", "grounding_verification"),),
        )
        review_events: list[AgentReviewStageEventV1] = []

        def observe_review_event(raw_event: dict[str, Any]) -> None:
            validate_agent_experience_payload("review-stage-event-v1", raw_event)
            event = AgentReviewStageEventV1.model_validate(raw_event)
            if event.sequence != len(review_events):
                raise RuntimeError("installed Agent review NDJSON sequence is discontinuous")
            review_stream.observe(event)
            review_events.append(event)

        experience_request = _review_request("run-cross-http-experience")
        raw_envelope = client.review_experience_stream(
            experience_request,
            on_event=observe_review_event,
        )
        review_stream.require_success()
        validate_agent_experience_payload(
            "review-experience-envelope-v1",
            raw_envelope,
        )
        envelope = ReviewExperienceEnvelopeV1.model_validate(raw_envelope)
        runtime = envelope.canonical_result.runtime_release
        capability = envelope.capability_attestation
        binding_checks = {
            "agent_commit": capability.agent_commit == runtime.agent_git_sha,
            "agent_code_snapshot_sha256": (
                capability.agent_code_snapshot_sha256 == runtime.agent_code_snapshot_sha256
            ),
            "model_id": capability.model_id == runtime.model_id,
            "runtime_tool_registry_id": (
                runtime.tool_registry_id == contract.provider_runtime_tool_registry_id
            ),
            "evidence_runtime_v3_sha256": (
                capability.evidence_runtime_v3_sha256 == runtime.evidence_runtime_v3_sha256
            ),
            "auto_completion_enabled": capability.auto_completion_enabled is False,
            "consumer_agent_commit": capability.agent_commit == contract.provider_commit,
            "prompt_bundle_sha256": (
                capability.prompt_bundle_sha256 == contract.experience_prompt_bundle_sha256
            ),
            "schema_bundle_sha256": (
                capability.schema_bundle_sha256 == contract.experience_schema_bundle_sha256
            ),
            "consumer_tool_registry_id": (
                capability.tool_registry_id == contract.uat_tool_registry_id
            ),
            "tool_registry_sha256": (
                capability.tool_registry_sha256 == contract.uat_tool_registry_sha256
            ),
        }
        binding_mismatches = [name for name, matched in binding_checks.items() if not matched]
        if binding_mismatches:
            raise RuntimeError(
                "installed Agent V3 capability binding mismatched: " + ", ".join(binding_mismatches)
            )
        validate_presentation_against_result(envelope)
        if (
            envelope.canonical_result.review_run_id != experience_request.review_run_id
            or envelope.canonical_result.runtime_release.agent_git_sha != expected_git_sha
            or envelope.capability_attestation != metadata_capability
            or not envelope.user_presentation.sections
        ):
            raise RuntimeError("installed Agent V3 review envelope lost its runtime binding")
        if [event.state for event in review_events if event.stage_code == "report_composition"] != [
            "started",
            "completed",
            "started",
            "completed",
        ]:
            raise RuntimeError(
                "installed Agent review did not traverse its bounded regeneration wire"
            )

        fallback_stream = _OrderedAgentStageStream(
            stages=(
                "request_validation",
                "document_analysis",
                "normalization",
                "document_pairing",
                "cross_validation",
                "evidence_retrieval",
                "compliance_review",
                "adaptive_investigation",
                "report_composition",
                "grounding_verification",
                "result_validation",
            ),
            skippable_stages=frozenset({"adaptive_investigation"}),
            retry_groups=(("report_composition", "grounding_verification"),),
        )
        fallback_events: list[AgentReviewStageEventV1] = []

        def observe_fallback_event(raw_event: dict[str, Any]) -> None:
            validate_agent_experience_payload("review-stage-event-v1", raw_event)
            event = AgentReviewStageEventV1.model_validate(raw_event)
            if event.sequence != len(fallback_events):
                raise RuntimeError("installed Agent fallback NDJSON sequence is discontinuous")
            fallback_stream.observe(event)
            fallback_events.append(event)

        fallback_request = _missing_document_fallback_request("run-cross-http-missing-documents")
        raw_fallback_envelope = client.review_experience_stream(
            fallback_request,
            on_event=observe_fallback_event,
        )
        fallback_stream.require_success()
        validate_agent_experience_payload(
            "review-experience-envelope-v1",
            raw_fallback_envelope,
        )
        fallback_envelope = ReviewExperienceEnvelopeV1.model_validate(raw_fallback_envelope)
        validate_presentation_against_result(fallback_envelope)
        required_document_missing = [
            issue
            for issue in fallback_envelope.canonical_result.validation_issues
            if issue.get("issue_code") == "REQUIRED_DOCUMENT_MISSING"
        ]
        if (
            len(required_document_missing) < 2
            or fallback_envelope.user_presentation.generation_audit.mode != "deterministic_fallback"
            or [
                event.state for event in fallback_events if event.stage_code == "report_composition"
            ]
            != ["started", "failed", "started", "failed", "started", "completed"]
        ):
            raise RuntimeError(
                "installed Agent missing-document fallback control did not exercise "
                "the bounded deterministic presentation path"
            )

        conversation_request = CaseConversationRequestV2(
            conversation_id="conversation-cross-repository-http",
            parent_message_id=None,
            question="이 심사 결과와 법령 근거의 의미, 다음 조치를 설명해 주세요.",
            bound_result_sha256=envelope.canonical_result.result_sha256,
            conversation_context_sha256=canonical_digest([]),
            recent_turns=[],
            review_result=envelope.canonical_result,
            locale="ko-KR",
        )
        conversation_payload = conversation_request.model_dump(mode="json")
        validate_agent_experience_payload(
            "case-conversation-request-v2",
            conversation_payload,
        )
        answer_stream = _OrderedAgentStageStream(
            stages=(
                "request_validation",
                "conversation_binding",
                "answer_planning",
                "answer_composition",
                "grounding_verification",
                "result_validation",
            ),
            retry_groups=(("answer_composition", "grounding_verification"),),
        )
        answer_events: list[AgentReviewStageEventV1] = []

        def observe_answer_event(raw_event: dict[str, Any]) -> None:
            validate_agent_experience_payload("review-stage-event-v1", raw_event)
            event = AgentReviewStageEventV1.model_validate(raw_event)
            if event.sequence != len(answer_events):
                raise RuntimeError("installed Agent answer NDJSON sequence is discontinuous")
            answer_stream.observe(event)
            answer_events.append(event)

        raw_answer_v2 = client.answer_v2_stream(
            conversation_payload,
            on_event=observe_answer_event,
        )
        answer_stream.require_success()
        validate_agent_experience_payload("assistant-answer-v2", raw_answer_v2)
        answer_v2 = AssistantAnswerV2.model_validate(raw_answer_v2)
        validate_answer_against_context(
            answer=answer_v2,
            request=conversation_request,
        )
        if not answer_v2.direct_answer.strip() or answer_v2.request_sha256 != canonical_digest(
            conversation_payload
        ):
            raise RuntimeError("installed Agent V3 answer lost its request binding")
        if [event.state for event in answer_events if event.stage_code == "answer_composition"] != [
            "started",
            "completed",
            "started",
            "completed",
        ]:
            raise RuntimeError(
                "installed Agent answer did not traverse its bounded regeneration wire"
            )

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
            "installed Agent wheel real-ASGI Backend HTTP conformance PASS: "
            "v2 review/answer/revision regression + v3 metadata/readiness/"
            "review-stream/answer-stream/missing-document-fallback "
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
    parser.add_argument("--agent-checkout", type=Path, required=True)
    parser.add_argument("--expected-git-sha", required=True)
    parser.add_argument("--expected-wheel-sha256", required=True)
    args = parser.parse_args()
    if args.serve:
        if args.port is None:
            parser.error("--port is required in server mode")
        _serve(
            port=args.port,
            agent_checkout=args.agent_checkout,
            expected_git_sha=args.expected_git_sha,
            expected_wheel_sha256=args.expected_wheel_sha256,
        )
        return
    if args.agent_python is None:
        parser.error("--agent-python is required in probe mode")
    _probe(
        agent_python=args.agent_python,
        agent_checkout=args.agent_checkout,
        expected_git_sha=args.expected_git_sha,
        expected_wheel_sha256=args.expected_wheel_sha256,
    )


if __name__ == "__main__":
    main()
