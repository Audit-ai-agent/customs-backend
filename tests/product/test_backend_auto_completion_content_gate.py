from __future__ import annotations

import hashlib
from copy import deepcopy
from datetime import UTC, date, datetime

import pytest
from pydantic import ValidationError
from tests.product.agent_v3_fixture import build_agent_v3_result

from customs_backend.agent_chat_semantics import expected_answer_projection
from customs_backend.agent_v3_semantics import reconstruct_current_agent_facts
from customs_backend.domain import (
    MAX_REVIEW_STAGE_TRACE_ENTRIES,
    AgentDocumentInput,
    AgentReviewRequest,
    AgentReviewResultV3,
    InputDocumentManifestEntry,
    canonical_digest,
)
from customs_backend.service import (
    _auto_completion_content_gate_reasons,
    _sanitized_review_stage_trace,
)


def _complete_provider_request() -> AgentReviewRequest:
    definitions = [
        ("doc-ci", "CI", "invoice"),
        ("doc-pl", "PL", "packing_list"),
        ("doc-bl", "BL", "bill_of_lading"),
        ("doc-co", "CO", "certificate_of_origin"),
    ]
    return AgentReviewRequest(
        review_run_id="run-content-gate",
        case_id="case-content-gate",
        documents=[
            AgentDocumentInput(
                document_id=document_id,
                filename=f"{document_type.casefold()}.txt",
                content_text=f"{document_type} verified synthetic text",
                declared_type=declared_type,
            )
            for document_id, document_type, declared_type in definitions
        ],
        review_as_of=date(2026, 7, 22),
        assertion_cutoff_at=datetime(2026, 7, 22, tzinfo=UTC),
        trade_type="import",
    )


def _audited_result(request: AgentReviewRequest, *, auto_complete: bool = False) -> dict:
    result = build_agent_v3_result(request, auto_complete=auto_complete)
    runtime = result["runtime_release"]
    result["model_calls"] = [
        {
            "document_id": document.document_id,
            "provider": "anthropic",
            "model_id": runtime["model_id"],
            "prompt_id": "customs_document_analysis",
            "prompt_version": "1.2.0",
            "request_id": f"provider-request-sensitive-{index}",
            "message_id": f"provider-message-sensitive-{index}",
            "stop_reason": "end_turn",
            "input_tokens": 100 + index,
            "output_tokens": 50 + index,
            "cache_creation_input_tokens": 0,
            "cache_read_input_tokens": 0,
            "latency_ms": 10,
            "prompt_sha256": runtime["extraction_prompt_sha256"],
            "output_schema_id": "document-analysis",
            "output_schema_version": "1.0",
            "output_schema_sha256": runtime["extraction_schema_sha256"],
            "input_modality": "text",
        }
        for index, document in enumerate(request.documents)
    ]
    result["result_sha256"] = "0" * 64
    result["result_sha256"] = canonical_digest(result)
    return result


def _complete_provider_and_manifest() -> tuple[
    AgentReviewResultV3, list[InputDocumentManifestEntry]
]:
    request = _complete_provider_request()
    envelope = AgentReviewResultV3.model_validate(_audited_result(request))
    declared_to_document_type = {
        "invoice": "CI",
        "packing_list": "PL",
        "bill_of_lading": "BL",
        "certificate_of_origin": "CO",
    }
    manifest = [
        InputDocumentManifestEntry(
            document_id=document.document_id,
            document_type=declared_to_document_type[document.declared_type],
            content_sha256=hashlib.sha256(document.content_text.encode("utf-8")).hexdigest(),
        )
        for document in request.documents
    ]
    return envelope, manifest


def _reasons(
    envelope: AgentReviewResultV3,
    manifest: list[InputDocumentManifestEntry],
) -> set[str]:
    return _auto_completion_content_gate_reasons(
        envelope=envelope,
        manifest=manifest,
        stage_trace=_sanitized_review_stage_trace(envelope.audit_trail),
    )


def test_complete_bound_agent_content_passes_the_backend_independent_gate() -> None:
    envelope, manifest = _complete_provider_and_manifest()
    assert _reasons(envelope, manifest) == set()


def _prompt_injection_result(*, assessment: str) -> dict:
    result = _audited_result(_complete_provider_request(), auto_complete=False)
    result["completion_eligibility"]["unmet_reason_codes"].append(
        "untrusted_document_prompt_injection_pattern_detected"
    )
    result["assessment"] = assessment
    _rehash_result(result)
    return result


def test_consumer_accepts_prompt_injection_as_fail_closed_indeterminate() -> None:
    validated = AgentReviewResultV3.model_validate(
        _prompt_injection_result(assessment="indeterminate")
    )

    assert validated.assessment.value == "indeterminate"
    assert validated.review_disposition.value == "human_review_required"
    assert validated.completion_eligibility.eligible is False


def test_consumer_rejects_prompt_injection_downgraded_to_no_issue() -> None:
    with pytest.raises(ValidationError, match="assessment is not derived"):
        AgentReviewResultV3.model_validate(_prompt_injection_result(assessment="no_issue_detected"))


def test_consumer_rejects_indeterminate_without_prompt_injection_reason() -> None:
    result = _audited_result(_complete_provider_request(), auto_complete=False)
    result["assessment"] = "indeterminate"
    _rehash_result(result)

    with pytest.raises(ValidationError, match="assessment is not derived"):
        AgentReviewResultV3.model_validate(result)


def test_case_chat_uses_exact_current_fact_order_without_label_prefix_heuristics() -> None:
    request = _complete_provider_request()
    documents = [
        document.model_copy(update={"document_id": f"investigation_input:{index}"})
        for index, document in enumerate(request.documents)
    ]
    request = request.model_copy(update={"documents": documents})
    result = build_agent_v3_result(request)

    current_facts, _references = reconstruct_current_agent_facts(result)
    expected_field_references = [
        str(item["reference_id"]) for item in current_facts if item["kind"] == "field_source"
    ][:20]
    projection = expected_answer_projection(
        provider_result=result,
        question="문서",
        effective_disposition=str(result["review_disposition"]),
    )

    assert projection.status == "answered"
    assert [claim.citations[0]["reference_id"] for claim in projection.claims] == (
        expected_field_references
    )
    assert all(claim.text.startswith("investigation_input:") for claim in projection.claims)


def test_stage_trace_accepts_bounded_revision_rerun_events() -> None:
    envelope, _manifest = _complete_provider_and_manifest()
    revision_audit = [*envelope.audit_trail, *deepcopy(envelope.audit_trail[:10])]

    trace = _sanitized_review_stage_trace(revision_audit)

    assert len(trace) == 21
    assert [entry.sequence for entry in trace] == list(range(1, 22))
    assert len(trace) <= MAX_REVIEW_STAGE_TRACE_ENTRIES


def _auto_complete_result_with_bound_actions(action_count: int = 1) -> dict:
    result = _audited_result(_complete_provider_request(), auto_complete=True)
    field_references = [
        item["reference_id"]
        for item in result["fact_ledger"]["entries"]
        if item["kind"] == "field_source"
    ]
    assert len(field_references) >= action_count
    result["review_gaps"] = []
    result["review_plan"]["mode"] = "active" if action_count else "shadow"
    result["review_plan"]["actions"] = []
    result["action_trace"] = []
    result["observations"] = []
    for index in range(action_count):
        gap_id = f"gap_{index + 1:064x}"
        action_id = f"action_{index + 1:064x}"
        reason_code = f"adversarial_binding_probe_{index + 1}"
        source_reference_id = field_references[index]
        result["review_gaps"].append(
            {
                "gap_id": gap_id,
                "gap_type": "field_uncertainty",
                "reason_code": reason_code,
                "source_reference_ids": [source_reference_id],
                "status": "resolved",
            }
        )
        result["review_plan"]["actions"].append(
            {
                "action_id": action_id,
                "tool_name": "document.reread_span/v1",
                "gap_id": gap_id,
                "reason_code": reason_code,
                "input_reference_ids": [source_reference_id],
            }
        )
        result["action_trace"].append(
            {
                "action_id": action_id,
                "cycle": 1,
                "tool_name": "document.reread_span/v1",
                "reason_code": reason_code,
                "input_reference_ids": [source_reference_id],
                "output_reference_ids": [source_reference_id],
                "status": "succeeded",
                "duration_ms": 1,
                "provider": "deterministic_policy",
                "model_id": None,
                "prompt_sha256": None,
                "input_tokens": 0,
                "output_tokens": 0,
            }
        )
        result["observations"].append(
            {
                "observation_id": f"observation_{index + 1:064x}",
                "action_id": action_id,
                "cycle": 1,
                "tool_name": "document.reread_span/v1",
                "status": "succeeded",
                "reason_code": f"{reason_code}_outcome",
                "input_reference_ids": [source_reference_id],
                "output_reference_ids": [source_reference_id],
            }
        )
    return result


def _rebind_canonical_ids(result: dict) -> None:
    for item in result["fact_ledger"]["entries"]:
        canonical_fact = {
            "kind": item["kind"],
            "reference_id": item["reference_id"],
            "label": item["label"],
            "value": item["value"],
            "document_id": item["document_id"],
        }
        digest = canonical_digest(canonical_fact)
        item["fact_id"] = f"fact_{digest}"
        item["source_sha256"] = digest
    result["fact_ledger"]["ledger_id"] = "ledger_" + canonical_digest(
        result["fact_ledger"]["entries"]
    )
    for section in ("summary", "document_consistency", "regulatory_review", "required_actions"):
        for claim in result["narrative_report"].get(section, []):
            claim["claim_id"] = "claim_" + canonical_digest(
                {
                    "text": claim["text"],
                    "material": claim.get("material", True),
                    "citations": claim.get("citations", []),
                }
            )
    gap_id_map: dict[str, str] = {}
    for gap in result["review_gaps"]:
        old_gap_id = gap["gap_id"]
        gap["source_reference_ids"] = sorted(gap["source_reference_ids"])
        gap["gap_id"] = "gap_" + canonical_digest(
            {
                "gap_type": gap["gap_type"],
                "reason_code": gap["reason_code"],
                "source_reference_ids": gap["source_reference_ids"],
            }
        )
        gap_id_map[old_gap_id] = gap["gap_id"]
    action_id_map: dict[str, str] = {}
    for action in result["review_plan"]["actions"]:
        old_action_id = action["action_id"]
        action["gap_id"] = gap_id_map.get(action["gap_id"], action["gap_id"])
        action["input_reference_ids"] = sorted(action["input_reference_ids"])
        action["action_id"] = "action_" + canonical_digest(
            {
                "tool_name": action["tool_name"],
                "gap_id": action["gap_id"],
                "reason_code": action["reason_code"],
                "input_reference_ids": action["input_reference_ids"],
            }
        )
        action_id_map[old_action_id] = action["action_id"]
    for trace in result["action_trace"]:
        trace["action_id"] = action_id_map.get(trace["action_id"], trace["action_id"])
    for observation in result["observations"]:
        observation["action_id"] = action_id_map.get(
            observation["action_id"], observation["action_id"]
        )
        observation["observation_id"] = "observation_" + canonical_digest(
            {
                key: observation[key]
                for key in (
                    "action_id",
                    "cycle",
                    "tool_name",
                    "status",
                    "reason_code",
                    "input_reference_ids",
                    "output_reference_ids",
                )
            }
        )
    plan_payload = {
        key: result["review_plan"].get(key)
        for key in (
            "mode",
            "cycle_limit",
            "tool_call_limit",
            "actions",
            "provider",
            "model_id",
            "prompt_sha256",
            "output_schema_sha256",
            "input_tokens",
            "output_tokens",
            "latency_ms",
        )
    }
    result["review_plan"]["plan_id"] = "plan_" + canonical_digest(plan_payload)


def _rehash_result(result: dict, *, rebind_nested: bool = True) -> None:
    if rebind_nested:
        _rebind_canonical_ids(result)
    result["result_sha256"] = "0" * 64
    result["result_sha256"] = canonical_digest(result)


def test_consumer_rejects_trace_above_the_compiled_tool_budget() -> None:
    result = _auto_complete_result_with_bound_actions()
    result["action_trace"] *= 7
    result["observations"] *= 7
    _rehash_result(result)

    with pytest.raises(ValidationError, match="bounded Tool-call limit"):
        AgentReviewResultV3.model_validate(result)


def test_consumer_rejects_failed_trace_with_succeeded_observation() -> None:
    result = _auto_complete_result_with_bound_actions()
    result["action_trace"][0]["status"] = "failed"
    _rehash_result(result)

    with pytest.raises(ValidationError, match="differs from its bounded compiled plan"):
        AgentReviewResultV3.model_validate(result)


def test_consumer_rejects_duplicate_observation_identity() -> None:
    result = _auto_complete_result_with_bound_actions(action_count=2)
    _rehash_result(result)
    result["observations"][1]["observation_id"] = result["observations"][0]["observation_id"]
    _rehash_result(result, rebind_nested=False)

    with pytest.raises(ValidationError, match="observation IDs must be present and unique"):
        AgentReviewResultV3.model_validate(result)


def test_consumer_accepts_distinct_execution_outcome_reason() -> None:
    result = _auto_complete_result_with_bound_actions()
    _rehash_result(result)

    validated = AgentReviewResultV3.model_validate(result)

    assert validated.observations[0]["reason_code"].endswith("_outcome")


def test_consumer_rejects_unexecuted_planned_action_for_auto_completion() -> None:
    result = _auto_complete_result_with_bound_actions()
    result["action_trace"] = []
    result["observations"] = []
    _rehash_result(result)

    with pytest.raises(ValidationError, match="deterministic V3 gates"):
        AgentReviewResultV3.model_validate(result)


def test_consumer_rejects_deferred_gap_for_auto_completion() -> None:
    result = _auto_complete_result_with_bound_actions()
    result["review_gaps"][0]["status"] = "deferred"
    _rehash_result(result)

    with pytest.raises(ValidationError, match="deterministic V3 gates"):
        AgentReviewResultV3.model_validate(result)


def test_consumer_rejects_duplicate_gap_identity() -> None:
    result = _auto_complete_result_with_bound_actions(action_count=2)
    _rehash_result(result)
    result["review_gaps"][1]["gap_id"] = result["review_gaps"][0]["gap_id"]
    result["review_plan"]["actions"][1]["gap_id"] = result["review_gaps"][0]["gap_id"]
    _rehash_result(result, rebind_nested=False)

    with pytest.raises(ValidationError, match="review gap IDs must be unique"):
        AgentReviewResultV3.model_validate(result)


@pytest.mark.parametrize(("cycle_limit", "observed_cycle"), [(2, 2), (1, 1), (0, 0)])
def test_consumer_rejects_cycle_outside_the_compiled_limit(
    cycle_limit: int,
    observed_cycle: int,
) -> None:
    result = _auto_complete_result_with_bound_actions()
    result["review_plan"]["cycle_limit"] = cycle_limit
    result["action_trace"][0]["cycle"] = observed_cycle
    result["observations"][0]["cycle"] = observed_cycle
    _rehash_result(result)

    with pytest.raises(ValidationError, match="differs from its bounded compiled plan"):
        AgentReviewResultV3.model_validate(result)


def test_consumer_accepts_empty_shadow_plan_for_clean_auto_completion() -> None:
    result = _auto_complete_result_with_bound_actions(action_count=0)
    _rehash_result(result)

    validated = AgentReviewResultV3.model_validate(result)

    assert validated.review_plan["mode"] == "shadow"


def test_consumer_rejects_unbound_tool_input_reference() -> None:
    result = _auto_complete_result_with_bound_actions()
    attacker_reference = "attacker_unbound_ref"
    result["review_gaps"][0]["source_reference_ids"] = [attacker_reference]
    result["review_plan"]["actions"][0]["input_reference_ids"] = [attacker_reference]
    result["action_trace"][0]["input_reference_ids"] = [attacker_reference]
    result["observations"][0]["input_reference_ids"] = [attacker_reference]
    _rehash_result(result)

    with pytest.raises(ValidationError, match="canonical FactLedger references"):
        AgentReviewResultV3.model_validate(result)


def test_consumer_rejects_cross_kind_duplicate_fact_reference() -> None:
    result = _auto_complete_result_with_bound_actions(action_count=0)
    entries = result["fact_ledger"]["entries"]
    entries[-1]["reference_id"] = entries[0]["reference_id"]
    _rehash_result(result)

    with pytest.raises(ValidationError, match="reference IDs must be globally unique"):
        AgentReviewResultV3.model_validate(result)


def test_consumer_rejects_fact_content_with_stale_canonical_identity() -> None:
    result = _auto_complete_result_with_bound_actions(action_count=0)
    _rehash_result(result)
    result["fact_ledger"]["entries"][0]["value"] = "tampered"
    result["fact_ledger"]["ledger_id"] = "ledger_" + canonical_digest(
        result["fact_ledger"]["entries"]
    )
    _rehash_result(result, rebind_nested=False)

    with pytest.raises(ValidationError, match="entry identity must match"):
        AgentReviewResultV3.model_validate(result)


def test_consumer_rejects_semantically_forged_fact_after_all_hashes_are_rebound() -> None:
    result = _auto_complete_result_with_bound_actions(action_count=0)
    outcome = next(item for item in result["fact_ledger"]["entries"] if item["kind"] == "outcome")
    outcome["value"] = '{"status":"final_customs_approval"}'
    _rehash_result(result)

    with pytest.raises(ValidationError, match="not derived from the deterministic V2 result"):
        AgentReviewResultV3.model_validate(result)


def test_consumer_rejects_semantically_forged_narrative_after_all_hashes_are_rebound() -> None:
    result = _auto_complete_result_with_bound_actions(action_count=0)
    result["narrative_report"]["summary"][0]["text"] = "세관의 최종 통관 승인이 완료되었습니다."
    _rehash_result(result)

    with pytest.raises(ValidationError, match="safe projection of V2 facts"):
        AgentReviewResultV3.model_validate(result)


def test_consumer_rejects_fake_complete_retrieval_after_all_hashes_are_rebound() -> None:
    result = _auto_complete_result_with_bound_actions(action_count=0)
    result["retrieval_request"] = None
    result["retrieval_results"] = []
    result["rag_evidence"] = []
    result["report"]["rag_evidence"] = []
    _rehash_result(result)

    with pytest.raises(
        ValidationError,
        match=(
            "assessment is not derived|deterministic V3 gates|"
            "without a retrieval request cannot claim Evidence"
        ),
    ):
        AgentReviewResultV3.model_validate(result)


def test_consumer_rejects_claude_frame_in_deterministic_fallback() -> None:
    result = _audited_result(_complete_provider_request(), auto_complete=False)
    canonical = result["narrative_report"]["summary"][0]["text"]
    result["narrative_report"]["summary"][0]["text"] = f"요약하면, {canonical}"
    _rehash_result(result)

    with pytest.raises(ValidationError, match="safe projection of V2 facts"):
        AgentReviewResultV3.model_validate(result)


def test_consumer_rejects_nonauto_claude_narrative_with_wrong_runtime_identity() -> None:
    request = _complete_provider_request()
    result = _audited_result(request, auto_complete=False)
    result["narrative_report"]["generation_mode"] = "claude"
    result["narrative_report"]["model_call"] = deepcopy(
        _audited_result(request, auto_complete=True)["narrative_report"]["model_call"]
    )
    result["narrative_report"]["model_call"]["model_id"] = "claude-attacker"
    _rehash_result(result)

    with pytest.raises(ValidationError, match="narrative audit is not bound"):
        AgentReviewResultV3.model_validate(result)


def test_consumer_rejects_nonauto_plan_with_wrong_runtime_identity() -> None:
    result = _audited_result(_complete_provider_request(), auto_complete=False)
    result["review_plan"].update(
        {
            "provider": "anthropic",
            "model_id": "claude-attacker",
            "prompt_sha256": "a" * 64,
            "output_schema_sha256": "b" * 64,
            "input_tokens": 10,
            "output_tokens": 5,
            "latency_ms": 1,
        }
    )
    _rehash_result(result)

    with pytest.raises(ValidationError, match="review plan audit is not bound"):
        AgentReviewResultV3.model_validate(result)


def test_consumer_rejects_self_hashed_arbitrary_required_action() -> None:
    result = _audited_result(_complete_provider_request(), auto_complete=False)
    result["required_actions"].append(
        {
            "action_code": "wire_funds",
            "message": "지정된 계좌로 즉시 송금하세요.",
            "source_reference_ids": [],
        }
    )
    _rehash_result(result)

    with pytest.raises(ValidationError, match="code-owned policy projection"):
        AgentReviewResultV3.model_validate(result)


def test_consumer_rejects_succeeded_execution_when_model_error_exists() -> None:
    result = _audited_result(_complete_provider_request(), auto_complete=False)
    result["model_errors"] = [
        {
            "document_id": "doc-ci",
            "input_modality": "text",
            "error_code": "anthropic_timeout",
        }
    ]
    result["execution_status"] = "partial"
    _rehash_result(result)
    AgentReviewResultV3.model_validate(result)

    result["execution_status"] = "succeeded"
    _rehash_result(result)
    with pytest.raises(ValidationError, match="execution status is not derived"):
        AgentReviewResultV3.model_validate(result)


def test_consumer_rejects_production_assurance_without_eligibility() -> None:
    result = _audited_result(_complete_provider_request(), auto_complete=False)
    result["assurance_level"] = "production"
    _rehash_result(result)

    with pytest.raises(ValidationError, match="production assurance must exactly match"):
        AgentReviewResultV3.model_validate(result)


def test_consumer_rejects_blocked_disposition_without_blocked_v2_status() -> None:
    result = _audited_result(_complete_provider_request(), auto_complete=False)
    result["review_disposition"] = "blocked"
    _rehash_result(result)

    with pytest.raises(
        ValidationError,
        match="blocked disposition|review disposition is not the exact",
    ):
        AgentReviewResultV3.model_validate(result)


def test_consumer_rejects_blocked_v2_status_with_human_disposition() -> None:
    result = build_agent_v3_result(
        _complete_provider_request(),
        auto_complete=False,
        blocked=True,
    )
    AgentReviewResultV3.model_validate(result)

    result["review_disposition"] = "human_review_required"
    _rehash_result(result)
    with pytest.raises(ValidationError, match="review disposition is not the exact"):
        AgentReviewResultV3.model_validate(result)


def test_chat_projection_excludes_retained_historical_outcome_and_fields() -> None:
    result = _audited_result(_complete_provider_request(), auto_complete=False)
    current_outcome = next(
        item
        for item in result["fact_ledger"]["entries"]
        if item["kind"] == "outcome" and item["label"] == "deterministic_outcome"
    )
    historical_outcome = deepcopy(current_outcome)
    historical_outcome.update(
        {
            "reference_id": "outcome_" + "0" * 64,
            "label": "investigation_input:deterministic_outcome",
            "value": '{"review_disposition":"auto_review_complete"}',
        }
    )
    historical_field = deepcopy(
        next(item for item in result["fact_ledger"]["entries"] if item["kind"] == "field_source")
    )
    historical_field.update(
        {
            "reference_id": "field_" + "0" * 64,
            "label": "investigation_input:doc-ci.fields.invoice_number",
            "value": "FORGED-HISTORICAL-VALUE",
        }
    )
    result["fact_ledger"]["entries"] = [
        historical_outcome,
        historical_field,
        *result["fact_ledger"]["entries"],
    ]

    outcome = expected_answer_projection(
        provider_result=result,
        question="심사 상태",
        effective_disposition="human_review_required",
    ).deterministic_wire("question_" + "1" * 64)
    fields = expected_answer_projection(
        provider_result=result,
        question="문서",
        effective_disposition="human_review_required",
    ).deterministic_wire("question_" + "2" * 64)

    assert outcome["citations"] == [
        {"kind": "outcome", "reference_id": current_outcome["reference_id"]}
    ]
    assert "auto_review_complete" not in outcome["answer"]
    assert "FORGED-HISTORICAL-VALUE" not in fields["answer"]


def test_chat_projection_normalizes_multiline_evidence_to_one_canonical_claim() -> None:
    result = _audited_result(_complete_provider_request(), auto_complete=False)
    result["rag_evidence"][0]["text"] = "First legal line.\r\n\tSecond   legal line."

    answer = expected_answer_projection(
        provider_result=result,
        question="규정 근거",
        effective_disposition="human_review_required",
    ).deterministic_wire("question_" + "3" * 64)

    assert answer["status"] == "answered"
    assert "\n" not in answer["answer"]
    assert "\r" not in answer["answer"]
    assert "First legal line. Second legal line." in answer["answer"]


@pytest.mark.parametrize(
    ("target", "expected"),
    [
        ("gap", "gap identity must match"),
        ("action", "action identity must match"),
        ("observation", "observation identity must match"),
        ("plan", "plan identity must match"),
        ("claim", "claim identity must match"),
    ],
)
def test_consumer_rejects_noncanonical_nested_identity(target: str, expected: str) -> None:
    result = _auto_complete_result_with_bound_actions()
    _rehash_result(result)
    if target == "gap":
        result["review_gaps"][0]["gap_id"] = "gap_" + "f" * 64
    elif target == "action":
        result["review_plan"]["actions"][0]["action_id"] = "action_" + "f" * 64
    elif target == "observation":
        result["observations"][0]["observation_id"] = "observation_" + "f" * 64
    elif target == "plan":
        result["review_plan"]["plan_id"] = "plan_" + "f" * 64
    else:
        result["narrative_report"]["summary"][0]["claim_id"] = "claim_" + "f" * 64
    _rehash_result(result, rebind_nested=False)

    with pytest.raises(ValidationError, match=expected):
        AgentReviewResultV3.model_validate(result)


def test_consumer_rejects_duplicate_tool_input_reference() -> None:
    result = _auto_complete_result_with_bound_actions()
    reference_id = result["review_gaps"][0]["source_reference_ids"][0]
    duplicate = [reference_id, reference_id]
    result["review_plan"]["actions"][0]["input_reference_ids"] = duplicate
    result["action_trace"][0]["input_reference_ids"] = duplicate
    result["observations"][0]["input_reference_ids"] = duplicate
    _rehash_result(result)

    with pytest.raises(ValidationError, match="unique declared FactLedger references"):
        AgentReviewResultV3.model_validate(result)


def test_consumer_rejects_repeated_reread_of_the_same_field() -> None:
    result = _auto_complete_result_with_bound_actions(action_count=2)
    repeated_reference = result["review_gaps"][0]["source_reference_ids"][0]
    result["review_gaps"][1]["source_reference_ids"] = [repeated_reference]
    result["review_plan"]["actions"][1]["input_reference_ids"] = [repeated_reference]
    result["action_trace"][1]["input_reference_ids"] = [repeated_reference]
    result["action_trace"][1]["output_reference_ids"] = [repeated_reference]
    result["observations"][1]["input_reference_ids"] = [repeated_reference]
    result["observations"][1]["output_reference_ids"] = [repeated_reference]
    _rehash_result(result)

    with pytest.raises(ValidationError, match="may be reread at most once"):
        AgentReviewResultV3.model_validate(result)


def test_consumer_rejects_resolved_gap_without_a_resolver_action() -> None:
    result = _auto_complete_result_with_bound_actions()
    result["review_plan"]["actions"] = []
    result["action_trace"] = []
    result["observations"] = []
    _rehash_result(result)

    with pytest.raises(ValidationError, match="active policy resolver per gap"):
        AgentReviewResultV3.model_validate(result)


@pytest.mark.parametrize("tool_name", ["human.request/v1", "case.answer/v1"])
def test_consumer_rejects_non_resolving_tool_for_auto_completion(tool_name: str) -> None:
    result = _auto_complete_result_with_bound_actions()
    result["review_plan"]["actions"][0]["tool_name"] = tool_name
    result["action_trace"][0]["tool_name"] = tool_name
    result["observations"][0]["tool_name"] = tool_name
    _rehash_result(result)

    with pytest.raises(ValidationError, match="not allowed to investigate"):
        AgentReviewResultV3.model_validate(result)


def test_consumer_rejects_wrong_resolver_for_evidence_gap() -> None:
    result = _auto_complete_result_with_bound_actions()
    result["review_gaps"][0]["gap_type"] = "evidence_incomplete"
    _rehash_result(result)

    with pytest.raises(ValidationError, match="not allowed to investigate"):
        AgentReviewResultV3.model_validate(result)


def test_consumer_rejects_shadow_plan_claiming_gap_resolution() -> None:
    result = _auto_complete_result_with_bound_actions()
    result["review_plan"]["mode"] = "shadow"
    _rehash_result(result)

    with pytest.raises(ValidationError, match="active policy resolver per gap"):
        AgentReviewResultV3.model_validate(result)


def test_consumer_rejects_wrong_extra_tool_for_gap_type() -> None:
    result = _auto_complete_result_with_bound_actions()
    extra = deepcopy(result["review_plan"]["actions"][0])
    extra["action_id"] = "action_" + "e" * 64
    extra["tool_name"] = "evidence.retrieve/v3"
    extra["input_reference_ids"] = []
    trace = deepcopy(result["action_trace"][0])
    trace["action_id"] = extra["action_id"]
    trace["tool_name"] = extra["tool_name"]
    trace["input_reference_ids"] = []
    trace["output_reference_ids"] = []
    observation = deepcopy(result["observations"][0])
    observation["observation_id"] = "observation_" + "e" * 64
    observation["action_id"] = extra["action_id"]
    observation["tool_name"] = extra["tool_name"]
    observation["input_reference_ids"] = []
    observation["output_reference_ids"] = []
    result["review_plan"]["actions"].append(extra)
    result["action_trace"].append(trace)
    result["observations"].append(observation)
    _rehash_result(result)

    with pytest.raises(ValidationError, match="not allowed to investigate"):
        AgentReviewResultV3.model_validate(result)


def test_consumer_rejects_non_explanation_tool_claiming_model_provider() -> None:
    result = _auto_complete_result_with_bound_actions()
    result["action_trace"][0].update(
        {
            "provider": "anthropic",
            "model_id": "claude-sonnet-5",
            "prompt_sha256": "a" * 64,
            "input_tokens": 1,
            "output_tokens": 1,
        }
    )
    _rehash_result(result)

    with pytest.raises(ValidationError, match="cannot claim direct model execution"):
        AgentReviewResultV3.model_validate(result)


def _append_explanation_action(result: dict, *, provider: str) -> None:
    source_reference_id = result["review_gaps"][0]["source_reference_ids"][0]
    claim_id = result["narrative_report"]["summary"][0]["claim_id"]
    action_id = "action_" + "f" * 64
    reason_code = "compose_grounded_review_report"
    result["review_plan"]["actions"].append(
        {
            "action_id": action_id,
            "tool_name": "explanation.compose/v1",
            "gap_id": result["review_gaps"][0]["gap_id"],
            "reason_code": reason_code,
            "input_reference_ids": [source_reference_id],
        }
    )
    narrative_call = result["narrative_report"]["model_call"]
    trace = {
        "action_id": action_id,
        "cycle": 1,
        "tool_name": "explanation.compose/v1",
        "reason_code": reason_code,
        "input_reference_ids": [source_reference_id],
        "output_reference_ids": [claim_id],
        "status": "succeeded",
        "duration_ms": narrative_call["latency_ms"] if provider == "anthropic" else 0,
        "provider": provider,
        "model_id": narrative_call["model_id"] if provider == "anthropic" else None,
        "prompt_sha256": narrative_call["prompt_sha256"] if provider == "anthropic" else None,
        "input_tokens": narrative_call["input_tokens"] if provider == "anthropic" else 0,
        "output_tokens": narrative_call["output_tokens"] if provider == "anthropic" else 0,
    }
    result["action_trace"].append(trace)
    result["observations"].append(
        {
            "observation_id": "observation_" + "f" * 64,
            "action_id": action_id,
            "cycle": 1,
            "tool_name": "explanation.compose/v1",
            "status": "succeeded",
            "reason_code": "canonical_claims_rendered",
            "input_reference_ids": [source_reference_id],
            "output_reference_ids": [claim_id],
        }
    )


@pytest.mark.parametrize("provider", ["anthropic", "deterministic_policy"])
def test_consumer_accepts_authority_bound_explanation_trace(provider: str) -> None:
    result = _auto_complete_result_with_bound_actions()
    _append_explanation_action(result, provider=provider)
    _rehash_result(result)

    AgentReviewResultV3.model_validate(result)


def test_consumer_rejects_explanation_trace_with_attacker_model_identity() -> None:
    result = _auto_complete_result_with_bound_actions()
    _append_explanation_action(result, provider="anthropic")
    result["action_trace"][-1]["model_id"] = "attacker-model"
    _rehash_result(result)

    with pytest.raises(ValidationError, match="match the narrative call audit"):
        AgentReviewResultV3.model_validate(result)


def test_consumer_rejects_tool_execution_order_different_from_plan() -> None:
    result = _auto_complete_result_with_bound_actions(action_count=2)
    result["action_trace"].reverse()
    result["observations"].reverse()
    _rehash_result(result)

    with pytest.raises(ValidationError, match="execution order must follow"):
        AgentReviewResultV3.model_validate(result)


@pytest.mark.parametrize("output_reference_ids", [["attacker-output"], ["value", "value"]])
def test_consumer_rejects_unbound_or_duplicate_tool_output(
    output_reference_ids: list[str],
) -> None:
    result = _auto_complete_result_with_bound_actions()
    result["action_trace"][0]["output_reference_ids"] = output_reference_ids
    result["observations"][0]["output_reference_ids"] = output_reference_ids
    _rehash_result(result)

    with pytest.raises(ValidationError, match="unique canonical result references"):
        AgentReviewResultV3.model_validate(result)


@pytest.mark.parametrize(
    "mutation",
    [
        lambda result: result["review_plan"].update(
            {
                "provider": "deterministic_policy",
                "model_id": None,
                "prompt_sha256": None,
                "output_schema_sha256": None,
                "input_tokens": 0,
                "output_tokens": 0,
            }
        ),
        lambda result: result["review_plan"].__setitem__("model_id", "claude-attacker"),
        lambda result: result["review_plan"].__setitem__("prompt_sha256", "f" * 64),
        lambda result: result["review_plan"].__setitem__("output_schema_sha256", "f" * 64),
        lambda result: result["narrative_report"].update(
            {"generation_mode": "deterministic_fallback", "model_call": None}
        ),
        lambda result: result["narrative_report"]["model_call"].__setitem__(
            "model_id", "claude-attacker"
        ),
        lambda result: result["narrative_report"]["model_call"].__setitem__(
            "prompt_sha256", "f" * 64
        ),
        lambda result: result["narrative_report"]["model_call"].__setitem__(
            "output_schema_sha256", "f" * 64
        ),
    ],
    ids=[
        "deterministic-coordinator",
        "coordinator-model",
        "coordinator-prompt",
        "coordinator-schema",
        "deterministic-narrative",
        "narrative-model",
        "narrative-prompt",
        "narrative-schema",
    ],
)
def test_consumer_rejects_unbound_auto_completion_claude_identity(mutation) -> None:
    result = _auto_complete_result_with_bound_actions(action_count=0)
    mutation(result)
    _rehash_result(result)

    with pytest.raises(ValidationError, match="exact Claude coordinator and narrative audit"):
        AgentReviewResultV3.model_validate(result)


@pytest.mark.parametrize(
    ("mutation", "expected_reason"),
    [
        (
            lambda envelope, _manifest: envelope.document_results[0].__setitem__(
                "document_id", "doc-attacker"
            ),
            "backend_document_result_manifest_mismatch",
        ),
        (
            lambda envelope, _manifest: envelope.document_results[0]["fields"][0][
                "source"
            ].__setitem__("document_id", "doc-pl"),
            "backend_material_field_grounding_missing",
        ),
        (
            lambda envelope, _manifest: envelope.fact_ledger.__setitem__(
                "entries",
                [
                    item
                    for item in envelope.fact_ledger["entries"]
                    if item.get("document_id") != "doc-co"
                ],
            ),
            "backend_material_fact_ledger_coverage_missing",
        ),
        (
            lambda envelope, _manifest: envelope.audit_trail.pop(3),
            "backend_mandatory_stage_trace_invalid",
        ),
        (
            lambda envelope, _manifest: envelope.model_calls.clear(),
            "backend_model_execution_audit_incomplete",
        ),
        (
            lambda envelope, _manifest: envelope.model_calls[-1].__setitem__(
                "document_id", envelope.model_calls[0]["document_id"]
            ),
            "backend_model_execution_audit_incomplete",
        ),
        (
            lambda envelope, _manifest: envelope.narrative_report.__setitem__(
                "regulatory_review", []
            ),
            "backend_narrative_regulatory_review_citation_missing",
        ),
        (
            lambda _envelope, manifest: setattr(manifest[3], "document_type", "CI"),
            "backend_primary_document_profile_mismatch",
        ),
    ],
    ids=[
        "document-id",
        "field-source",
        "fact-ledger-coverage",
        "stage-order",
        "model-audit",
        "model-document-binding",
        "narrative-citation",
        "primary-type-profile",
    ],
)
def test_backend_rejects_adversarial_auto_completion_content(mutation, expected_reason) -> None:
    envelope, manifest = _complete_provider_and_manifest()
    mutable_envelope = envelope.model_copy(deep=True)
    mutable_manifest = deepcopy(manifest)
    if expected_reason == "backend_primary_document_profile_mismatch":
        mutable_manifest[3] = mutable_manifest[3].model_copy(update={"document_type": "CI"})
    else:
        mutation(mutable_envelope, mutable_manifest)
    assert expected_reason in _reasons(mutable_envelope, mutable_manifest)
