from __future__ import annotations

import hashlib
from copy import deepcopy
from datetime import UTC, date, datetime

import pytest
from tests.product.agent_v3_fixture import build_agent_v3_result

from customs_backend.domain import (
    MAX_REVIEW_STAGE_TRACE_ENTRIES,
    AgentDocumentInput,
    AgentReviewRequest,
    AgentReviewResultV3,
    InputDocumentManifestEntry,
)
from customs_backend.service import (
    _auto_completion_content_gate_reasons,
    _sanitized_review_stage_trace,
)


def _complete_provider_and_manifest() -> tuple[
    AgentReviewResultV3, list[InputDocumentManifestEntry]
]:
    definitions = [
        ("doc-ci", "CI", "invoice"),
        ("doc-pl", "PL", "packing_list"),
        ("doc-bl", "BL", "bill_of_lading"),
        ("doc-co", "CO", "certificate_of_origin"),
    ]
    request = AgentReviewRequest(
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
    envelope = AgentReviewResultV3.model_validate(build_agent_v3_result(request))
    manifest = [
        InputDocumentManifestEntry(
            document_id=document.document_id,
            document_type=document_type,
            content_sha256=hashlib.sha256(document.content_text.encode("utf-8")).hexdigest(),
        )
        for document, (_document_id, document_type, _declared_type) in zip(
            request.documents, definitions, strict=True
        )
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
