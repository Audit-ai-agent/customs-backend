from __future__ import annotations

import json
import hashlib
from typing import Any

from customs_backend.domain import AgentReviewRequest, canonical_digest
from customs_backend.release import load_agent_contract_lock


def _stable_id(prefix: str, value: object) -> str:
    return f"{prefix}_{canonical_digest(value)}"


def _source(document_id: str) -> dict[str, Any]:
    return {
        "document_id": document_id,
        "page": 1,
        "source_modality": "text",
        "bbox": None,
        "text_start": 0,
        "text_end": 1,
        "image_sha256": None,
        "image_width_px": None,
        "image_height_px": None,
        "coordinate_space": None,
        "ocr_ledger_id": None,
        "ocr_span_ids": [],
    }


def _fact(
    *,
    kind: str,
    reference_id: str,
    label: str,
    value: Any,
    document_id: str | None = None,
) -> dict[str, Any]:
    rendered = (
        value.strip()
        if isinstance(value, str)
        else json.dumps(
            value,
            ensure_ascii=False,
            allow_nan=False,
            separators=(",", ":"),
            sort_keys=True,
        )
    )
    payload = {
        "kind": kind,
        "reference_id": reference_id,
        "label": label,
        "value": (rendered or "(empty)")[:4000],
        "document_id": document_id,
    }
    digest = canonical_digest(payload)
    return {
        "fact_id": f"fact_{digest}",
        **payload,
        "source_sha256": digest,
    }


def _claim(text: str, citations: list[dict[str, str]]) -> dict[str, Any]:
    payload = {"text": text, "material": True, "citations": citations}
    return {"claim_id": _stable_id("claim", payload), **payload}


def build_agent_v3_result(
    request: AgentReviewRequest,
    *,
    auto_complete: bool = True,
    blocked: bool = False,
    knowledge_snapshot_id: str = "generation-1",
) -> dict:
    """Build a semantically provider-valid result without importing Agent code.

    This fixture independently mirrors the public Agent V3 projection. It uses
    complete synthetic no-requirement Evidence; product runtime never falls back
    to this fixture.
    """

    if auto_complete and blocked:
        raise ValueError("a synthetic result cannot be both automatic and blocked")
    status = "blocked" if blocked else ("completed" if auto_complete else "needs_review")
    requires_human_review = not auto_complete
    document_results: list[dict[str, Any]] = []
    retrieval_items: list[dict[str, Any]] = []
    retrieval_results: list[dict[str, Any]] = []
    for document in request.documents:
        source = _source(document.document_id)
        item_id = f"{document.document_id}-item-0"
        document_results.append(
            {
                "document_id": document.document_id,
                "document_type": document.declared_type or "unknown",
                "input_modality": "text",
                "extraction_status": "completed",
                "extraction_confidence": 1.0,
                "extraction_provider": "deterministic_contract_fixture",
                "model_id": None,
                "prompt_version": None,
                "ocr_layout": None,
                "fields": [
                    {
                        "name": "invoice_number",
                        "value": "INV-001",
                        "normalized_value": "INV-001",
                        "confidence": 1.0,
                        "confidence_basis": "rule_heuristic",
                        "source": source,
                    }
                ],
                "line_items": [
                    {
                        "item_id": item_id,
                        "fields": [
                            {
                                "name": "quantity",
                                "value": "1",
                                "normalized_value": "1",
                                "confidence": 1.0,
                                "confidence_basis": "rule_heuristic",
                                "source": source,
                            }
                        ],
                    }
                ],
            }
        )
        retrieval_items.append(
            {
                "item_id": item_id,
                "description": "Synthetic no-requirement line item",
                "hs_code": "0303540000",
                "origin_country": "NO",
                "destination_country": "KR",
            }
        )
        retrieval_results.append(
            {
                "item_id": item_id,
                "hs_resolution": "exact",
                "resolved_hsk10": "0303540000",
                "candidate_hsk10s": [],
                "coverage": "complete",
                "requirements_found": False,
                "evidence": [],
                "errors": [],
            }
        )

    evidence_source_id = "fixture:hsk:0303540000"
    evidence_version_hash = "f" * 64
    evidence_id = (
        "ev_"
        + hashlib.sha256(
            f"{evidence_source_id}\x1f{evidence_version_hash}".encode("utf-8")
        ).hexdigest()
    )
    evidence = {
        "evidence_id": evidence_id,
        "source_record_id": evidence_source_id,
        "source_type": "hsk_entry",
        "item_id": retrieval_results[0]["item_id"],
        "title": "Synthetic HSK classification evidence",
        "text": "The synthetic item is bound to HSK 0303540000 for contract testing.",
        "law_id": None,
        "law_name": None,
        "article_no": None,
        "source_uri": "https://example.invalid/hsk/0303540000",
        "valid_from": None,
        "valid_to": None,
        "version_hash": evidence_version_hash,
        "matched_hs_code": "0303540000",
        "trade_type": request.trade_type,
        "required_document_code": None,
        "required_document_name": None,
        "agencies": [],
        "retrieval_methods": ["exact_hs"],
        "eligibility": "supporting",
        "score": 1.0,
        "raw_scores": {"exact_hs": 1.0},
    }
    retrieval_results[0]["evidence"] = [evidence]

    retrieval_request = {
        "schema_version": "1.0",
        "request_id": _stable_id(
            "request",
            {
                "case_id": request.case_id,
                "review_run_id": request.review_run_id,
                "items": [item["item_id"] for item in retrieval_items],
            },
        )[:128],
        "case_id": request.case_id,
        "review_run_id": request.review_run_id,
        "review_as_of": request.review_as_of.isoformat(),
        "jurisdiction": request.jurisdiction,
        "trade_type": request.trade_type,
        "top_k": 8,
        "items": retrieval_items,
        "uploaded_requirement_document_codes": [],
        "validation_issue_codes": [],
        "query_intent": "required_import_documents",
    }

    facts: list[dict[str, Any]] = []
    for document in document_results:
        document_id = document["document_id"]
        for field in document["fields"]:
            reference_id = _stable_id(
                "field",
                {
                    "document_id": document_id,
                    "field_path": f"fields.{field['name']}",
                    "source": field["source"],
                },
            )
            facts.append(
                _fact(
                    kind="field_source",
                    reference_id=reference_id,
                    label=f"{document_id}.fields.{field['name']}",
                    value=field["normalized_value"],
                    document_id=document_id,
                )
            )
        for line_item in document["line_items"]:
            for field in line_item["fields"]:
                reference_id = _stable_id(
                    "field",
                    {
                        "document_id": document_id,
                        "field_path": f"line_items.{line_item['item_id']}.{field['name']}",
                        "source": field["source"],
                    },
                )
                facts.append(
                    _fact(
                        kind="field_source",
                        reference_id=reference_id,
                        label=(f"{document_id}.line_items.{line_item['item_id']}.{field['name']}"),
                        value=field["normalized_value"],
                        document_id=document_id,
                    )
                )

    facts.append(
        _fact(
            kind="evidence",
            reference_id=evidence_id,
            label=evidence["title"],
            value=evidence["text"],
        )
    )

    outcome_value = {
        "status": status,
        "requires_human_review": requires_human_review,
        "retrieval_status": "ok",
        "retrieval_coverage": "complete",
    }
    outcome_reference = _stable_id("outcome", outcome_value)
    facts.append(
        _fact(
            kind="outcome",
            reference_id=outcome_reference,
            label="deterministic_outcome",
            value=outcome_value,
        )
    )
    facts.sort(key=lambda item: (item["kind"], item["reference_id"]))
    ledger = {"ledger_id": _stable_id("ledger", facts), "entries": facts}

    outcome_citation = [{"kind": "outcome", "reference_id": outcome_reference}]
    summary_claim = _claim(
        "현재 검토 범위에서 문서 불일치와 미충족 규정 요건이 발견되지 않았습니다.",
        outcome_citation,
    )
    document_claim = _claim(
        "결정론적 문서 교차검증에서 불일치가 발견되지 않았습니다.",
        outcome_citation,
    )
    regulation_claim = _claim(
        "조회된 규정 근거 범위에서 별도의 미충족 요건 Finding이 생성되지 않았습니다.",
        [{"kind": "evidence", "reference_id": evidence_id}],
    )
    action_claims = [
        _claim(
            (
                "승인된 자동완료 범위에서 추가 조치가 필요한 항목이 없습니다."
                if auto_complete
                else "담당자는 근거와 추출값을 확인한 뒤 수정 또는 승인을 기록해야 합니다."
            ),
            outcome_citation,
        )
    ]
    if blocked:
        action_claims.append(
            _claim(
                "차단 원인을 해소한 뒤 새로운 Review Run으로 다시 실행해야 합니다.",
                outcome_citation,
            )
        )

    plan_payload = {
        "mode": "shadow",
        "cycle_limit": 2,
        "tool_call_limit": 6,
        "actions": [],
        "provider": "anthropic" if auto_complete else "deterministic_policy",
        "model_id": "claude-test" if auto_complete else None,
        "prompt_sha256": "2" * 64 if auto_complete else None,
        "output_schema_sha256": "5" * 64 if auto_complete else None,
        "input_tokens": 20 if auto_complete else 0,
        "output_tokens": 10 if auto_complete else 0,
        "latency_ms": 1 if auto_complete else 0,
    }
    plan = {"plan_id": _stable_id("plan", plan_payload), **plan_payload}
    contract_lock = load_agent_contract_lock()
    runtime_release = {
        "agent_package_version": "0.3.0",
        "agent_git_sha": "f" * 40,
        "agent_source_snapshot_sha256": "c" * 64,
        "agent_code_snapshot_sha256": "d" * 64,
        "model_id": "claude-test",
        "model_revision": "claude-test-revision",
        "extraction_prompt_sha256": "1" * 64,
        "extraction_schema_sha256": "e" * 64,
        "coordinator_prompt_sha256": "2" * 64,
        "narrative_prompt_sha256": "3" * 64,
        "chat_prompt_sha256": "4" * 64,
        "coordinator_schema_sha256": "5" * 64,
        "narrative_schema_sha256": "6" * 64,
        "chat_schema_sha256": "7" * 64,
        "result_schema_sha256": contract_lock.review_result_v3_sha256,
        "policy_id": "policy-v3",
        "tool_registry_id": "tools-v1",
        "assertion_trust_store_sha256": (
            "a" * 64
            if auto_complete
            else "ccad11713d4c18b4f19246f32fc68cbd4faba2d66889f9c037845fdf66db3b13"
        ),
        "assertion_trust_store_state": "active" if auto_complete else "unreleased",
        "evidence_snapshot_id": knowledge_snapshot_id,
        "evidence_runtime_v3_sha256": "8" * 64 if auto_complete else None,
        "release_profile_id": "profile-1",
        "release_profile_state": "approved" if auto_complete else "unreleased",
        "release_profile_sha256": "9" * 64 if auto_complete else None,
        "release_profile_subject_sha256": "a" * 64 if auto_complete else None,
        "release_profile_evaluation_report_sha256": "b" * 64 if auto_complete else None,
        "reviewer_trust_store_sha256": "c" * 64 if auto_complete else None,
        "kill_switch_active": not auto_complete,
    }
    report_documents = [
        {
            "document_id": document["document_id"],
            "document_type": document["document_type"],
            "extraction_status": document["extraction_status"],
            "extraction_confidence": document["extraction_confidence"],
        }
        for document in document_results
    ]
    result: dict[str, Any] = {
        "schema_version": "3.0",
        "case_id": request.case_id,
        "review_run_id": request.review_run_id,
        "review_as_of": request.review_as_of.isoformat(),
        "trade_type": request.trade_type,
        "jurisdiction": request.jurisdiction,
        "status": status,
        "requires_human_review": requires_human_review,
        "retrieval_status": "ok",
        "retrieval_coverage": "complete",
        "knowledge_snapshot_id": knowledge_snapshot_id,
        "retrieval_request": retrieval_request,
        "retrieval_results": retrieval_results,
        "retrieval_errors": [],
        "validation_issues": [],
        "regulatory_findings": [],
        "rag_evidence": [evidence],
        "report": {
            "case_id": request.case_id,
            "overall_status": status,
            "summary": {
                "total_documents": len(document_results),
                "total_issues": 0,
                "critical": 0,
                "high": 0,
                "medium": 0,
                "low": 0,
                "average_extraction_confidence": 1.0,
                "outcome_reasons": [],
                "retrieval_status": "ok",
                "retrieval_coverage": "complete",
                "knowledge_snapshot_id": knowledge_snapshot_id,
            },
            "document_summary": report_documents,
            "issues": [],
            "rag_evidence": [
                {
                    **evidence,
                    "raw_scores": [{"method": "exact_hs", "score": 1.0}],
                }
            ],
            "regulatory_findings": [],
            "disclaimer": "Synthetic provider-contract fixture; not a legal determination.",
        },
        "audit_trail": [
            {
                "stage": stage,
                "event": "completed",
                "message": "deterministic contract fixture stage completed",
                "metadata": {},
            }
            for stage in (
                "load_case",
                "classify_documents",
                "extract_documents",
                "normalize_documents",
                "pair_documents",
                "validate_cross_documents",
                "retrieve_regulations",
                "compliance_review",
                "generate_xai_report",
                "route_to_human_or_complete",
                "redact_sensitive_inputs",
            )
        ],
        "model_calls": [],
        "model_errors": [],
        "model_conflicts": [],
        "document_results": document_results,
        "execution_status": "failed" if blocked else "succeeded",
        "assessment": "no_issue_detected",
        "review_disposition": (
            "blocked"
            if blocked
            else ("auto_review_complete" if auto_complete else "human_review_required")
        ),
        "assurance_level": "production" if auto_complete else "production_candidate",
        "completion_eligibility": {
            "eligible": auto_complete,
            "profile_id": "profile-1" if auto_complete else None,
            "scope": ["KR/import/0303540000"] if auto_complete else [],
            "unmet_reason_codes": [] if auto_complete else ["test_fixture_not_released"],
        },
        "required_actions": (
            []
            if auto_complete
            else [
                {
                    "action_code": "obtain_production_release_approval",
                    "message": (
                        "Claude extraction과 법령 범위에 대한 독립 검수 및 "
                        "Release 승인이 필요합니다."
                    ),
                    "source_reference_ids": [outcome_reference],
                }
            ]
        ),
        "fact_ledger": ledger,
        "review_gaps": [],
        "review_plan": plan,
        "narrative_report": {
            "language": "ko-KR",
            "summary": [summary_claim],
            "document_consistency": [document_claim],
            "regulatory_review": [regulation_claim],
            "required_actions": action_claims,
            "generation_mode": "claude" if auto_complete else "deterministic_fallback",
            "citation_verification": "passed",
            "model_call": (
                {
                    "task_id": "customs_narrative_report",
                    "provider": "anthropic",
                    "model_id": runtime_release["model_id"],
                    "prompt_sha256": runtime_release["narrative_prompt_sha256"],
                    "output_schema_sha256": runtime_release["narrative_schema_sha256"],
                    "request_id_sha256": "d" * 64,
                    "message_id_sha256": "e" * 64,
                    "input_tokens": 20,
                    "output_tokens": 10,
                    "latency_ms": 1,
                }
                if auto_complete
                else None
            ),
        },
        "action_trace": [],
        "observations": [],
        "runtime_release": runtime_release,
        "input_manifest_sha256": request.input_manifest_sha256,
        "result_sha256": "0" * 64,
    }
    result["result_sha256"] = canonical_digest(result)
    return result
