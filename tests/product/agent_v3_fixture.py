from __future__ import annotations

import json

from customs_backend.domain import AgentReviewRequest, canonical_digest
from customs_backend.release import load_agent_contract_lock


def _stable_id(prefix: str, value: object) -> str:
    return f"{prefix}_{canonical_digest(value)}"


def build_agent_v3_result(
    request: AgentReviewRequest,
    *,
    auto_complete: bool = True,
    knowledge_snapshot_id: str = "generation-1",
) -> dict:
    """Build a provider-valid, deterministic result without importing Agent code.

    The fixture intentionally exercises the provider-owned JSON Schema and the
    Backend kill switch. It is not a Mock fallback used by product runtime.
    """

    outcome_value = {
        "status": "completed",
        "requires_human_review": not auto_complete,
        "retrieval_status": "ok",
        "retrieval_coverage": "complete",
    }
    outcome_reference = _stable_id("outcome", outcome_value)
    rendered_outcome = json.dumps(
        outcome_value,
        ensure_ascii=False,
        allow_nan=False,
        separators=(",", ":"),
        sort_keys=True,
    )
    fact_payload = {
        "kind": "outcome",
        "reference_id": outcome_reference,
        "label": "deterministic_outcome",
        "value": rendered_outcome,
        "document_id": None,
    }
    fact = {
        "fact_id": _stable_id("fact", fact_payload),
        **fact_payload,
        "source_sha256": canonical_digest(fact_payload),
    }
    field_facts: list[dict] = []
    for document in request.documents:
        field_reference = f"field:{document.document_id}:invoice_number"
        field_fact_payload = {
            "kind": "field_source",
            "reference_id": field_reference,
            "label": "invoice_number",
            "value": "INV-001",
            "document_id": document.document_id,
        }
        field_facts.append(
            {
                "fact_id": _stable_id("fact", field_fact_payload),
                **field_fact_payload,
                "source_sha256": canonical_digest(field_fact_payload),
            }
        )
    entries = [*field_facts, fact]
    ledger = {
        "ledger_id": _stable_id("ledger", entries),
        "entries": entries,
    }
    citation = {"kind": "outcome", "reference_id": outcome_reference}
    claim_payload = {
        "text": "결정론적 심사 결과를 확인했습니다.",
        "material": True,
        "citations": [citation],
    }
    claim = {"claim_id": _stable_id("claim", claim_payload), **claim_payload}
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
    result = {
        "schema_version": "3.0",
        "case_id": request.case_id,
        "review_run_id": request.review_run_id,
        "review_as_of": request.review_as_of.isoformat(),
        "trade_type": request.trade_type,
        "jurisdiction": request.jurisdiction,
        "status": "completed",
        "requires_human_review": not auto_complete,
        "retrieval_status": "ok",
        "retrieval_coverage": "complete",
        "knowledge_snapshot_id": knowledge_snapshot_id,
        "retrieval_request": None,
        "retrieval_results": [],
        "retrieval_errors": [],
        "validation_issues": [],
        "regulatory_findings": [],
        "rag_evidence": [],
        "report": {
            "case_id": request.case_id,
            "overall_status": "completed",
            "summary": {
                "total_documents": len(request.documents),
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
            "document_summary": [],
            "issues": [],
            "rag_evidence": [],
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
        "document_results": [
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
                        "source": {
                            "document_id": document.document_id,
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
                        },
                    }
                ],
                "line_items": [
                    {
                        "item_id": f"{document.document_id}-item-0",
                        "fields": [
                            {
                                "name": "quantity",
                                "value": "1",
                                "normalized_value": "1",
                                "confidence": 1.0,
                                "confidence_basis": "rule_heuristic",
                                "source": {
                                    "document_id": document.document_id,
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
                                },
                            }
                        ],
                    }
                ],
            }
            for document in request.documents
        ],
        "execution_status": "succeeded",
        "assessment": "no_issue_detected",
        "review_disposition": (
            "auto_review_complete" if auto_complete else "human_review_required"
        ),
        "assurance_level": "production" if auto_complete else "production_candidate",
        "completion_eligibility": {
            "eligible": auto_complete,
            "profile_id": "profile-1" if auto_complete else None,
            "scope": ["KR/import/0303540000"] if auto_complete else [],
            "unmet_reason_codes": [] if auto_complete else ["test_fixture_not_released"],
        },
        "required_actions": [],
        "fact_ledger": ledger,
        "review_gaps": [],
        "review_plan": plan,
        "narrative_report": {
            "language": "ko-KR",
            "summary": [claim],
            "document_consistency": [claim],
            "regulatory_review": [claim],
            "required_actions": [],
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
