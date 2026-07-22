from __future__ import annotations

import hashlib
from copy import deepcopy
from datetime import UTC, date, datetime

import pytest
from pydantic import ValidationError
from tests.product.agent_v3_fixture import build_agent_v3_result

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


def test_stage_trace_accepts_bounded_revision_rerun_events() -> None:
    envelope, _manifest = _complete_provider_and_manifest()
    revision_audit = [*envelope.audit_trail, *deepcopy(envelope.audit_trail[:10])]

    trace = _sanitized_review_stage_trace(revision_audit)

    assert len(trace) == 21
    assert [entry.sequence for entry in trace] == list(range(1, 22))
    assert len(trace) <= MAX_REVIEW_STAGE_TRACE_ENTRIES


def _auto_complete_result_with_bound_actions(action_count: int = 1) -> dict:
    result = _audited_result(_complete_provider_request(), auto_complete=True)
    result["review_gaps"] = []
    result["review_plan"]["actions"] = []
    result["action_trace"] = []
    result["observations"] = []
    for index in range(action_count):
        gap_id = f"gap_{index + 1:064x}"
        action_id = f"action_{index + 1:064x}"
        reason_code = f"adversarial_binding_probe_{index + 1}"
        result["review_gaps"].append(
            {
                "gap_id": gap_id,
                "gap_type": "field_uncertainty",
                "reason_code": reason_code,
                "source_reference_ids": [],
                "status": "resolved",
            }
        )
        result["review_plan"]["actions"].append(
            {
                "action_id": action_id,
                "tool_name": "document.reread_span/v1",
                "gap_id": gap_id,
                "reason_code": reason_code,
                "input_reference_ids": [],
            }
        )
        result["action_trace"].append(
            {
                "action_id": action_id,
                "cycle": 1,
                "tool_name": "document.reread_span/v1",
                "reason_code": reason_code,
                "input_reference_ids": [],
                "output_reference_ids": [],
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
                "input_reference_ids": [],
                "output_reference_ids": [],
            }
        )
    return result


def _rehash_result(result: dict) -> None:
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
    result["observations"][1]["observation_id"] = result["observations"][0]["observation_id"]
    _rehash_result(result)

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
    result["review_gaps"][1]["gap_id"] = result["review_gaps"][0]["gap_id"]
    result["review_plan"]["actions"][1]["gap_id"] = result["review_gaps"][0]["gap_id"]
    _rehash_result(result)

    with pytest.raises(ValidationError, match="review gap IDs must be unique"):
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
