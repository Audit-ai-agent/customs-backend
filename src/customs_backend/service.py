from __future__ import annotations

import hashlib
import logging
import math
import re
import uuid
from dataclasses import dataclass
from datetime import date, timedelta
from typing import Any, Literal
from urllib.parse import urlsplit, urlunsplit

from pydantic import ValidationError

from customs_backend.agent_chat_semantics import (
    expected_answer_projection,
    validate_agent_answer_projection,
)
from customs_backend.assertion_signing import (
    RequirementPolicyIdentityResolver,
    SupportingAssertionSigner,
    UnavailableRequirementPolicyIdentityResolver,
    UnavailableSupportingAssertionSigner,
)
from customs_backend.domain import (
    AgentCallState,
    AgentAnswerResponse,
    AgentDocumentInput,
    AgentHumanDecision,
    AgentReviewRequest,
    AgentReviewResultV3,
    AgentRevisionRequest,
    AgentRevisionResult,
    Assessment,
    AssuranceLevel,
    AutoCompletionProfileBinding,
    AuditRecord,
    CaseCreate,
    CaseRecord,
    CaseStatus,
    ChatInteractionRecord,
    ChatInteractionState,
    ChatRecord,
    CorrectionProposalCreate,
    CorrectionProposalDecisionCreate,
    CorrectionProposalRecord,
    CorrectionProposalStatus,
    DeliveryReconciliationCreate,
    DocumentRecord,
    ExecutionStatus,
    GroundedQuestion,
    HumanDecisionCreate,
    HumanDecisionRecord,
    InputDocumentManifestEntry,
    JobRecord,
    JobStatus,
    MAX_TEXT_DOCUMENT_CHARACTERS,
    MAX_TEXT_DOCUMENT_UTF8_BYTES,
    ModelExecutionSummary,
    OutboxRecord,
    Principal,
    RequiredAction,
    ReviewDisposition,
    ReviewEnqueueRequest,
    RevisionAction,
    RevisionProposalCreate,
    ReviewRunRecord,
    ReviewStageTraceEntry,
    Role,
    StageEvent,
    SupportingDocumentCreate,
    SupportingDocumentRecord,
    SupportingVerificationDecisionRecord,
    TextDocumentCreate,
    VerificationAssertionCreate,
    VerificationAssertionRecord,
    canonical_digest,
    canonical_json_bytes,
    utc_now,
)
from customs_backend.ports import (
    AgentClient,
    AgentPreExecutionRetriesExhaustedError,
    AgentPreExecutionRetryableError,
    AgentResponseRejectedError,
    ConflictError,
    JobExecutionNotRunnableError,
    JobPublisher,
    NotFoundError,
    ObjectStore,
    WorkbenchRepository,
)
from customs_backend.release import (
    AgentReleasePin,
    AgentServicePin,
    AutoCompletionApprovalManifest,
    ReleaseConfigurationError,
    load_agent_contract_lock,
    load_domain_reviewer_trust_store,
    validate_agent_v3_payload,
)


LOGGER = logging.getLogger(__name__)
CHAT_INTERACTIONS_PER_REVIEW_RUN = 100
CHAT_INTERACTIONS_PER_ACTOR_PER_MINUTE = 10
_SAFE_RETRIEVAL_METHODS = frozenset({"exact_hs", "dense", "bm25", "graph"})
_SAFE_RETRIEVAL_SCORE_KEYS = frozenset(
    {"exact_hs", "dense", "bm25", "graph", "rrf", "lexical_overlap"}
)
_SAFE_EVIDENCE_SOURCE_TYPES = frozenset(
    {"law_article", "customs_requirement", "hsk_entry", "notice"}
)
_SHA256 = re.compile(r"^[a-f0-9]{64}$")
_EVIDENCE_REQUEST_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")


def _bounded_provenance_text(
    value: object,
    *,
    field: str,
    maximum: int,
    optional: bool = False,
) -> str | None:
    if value is None and optional:
        return None
    if not isinstance(value, str) or not value.strip() or len(value) > maximum:
        raise ConflictError(f"Agent {field} is not safe for public retrieval provenance")
    return value


def _bounded_hs_code(value: object, *, field: str, optional: bool = False) -> str | None:
    text = _bounded_provenance_text(value, field=field, maximum=10, optional=optional)
    if text is None:
        return None
    if len(text) not in {6, 10} or not text.isascii() or not text.isdigit():
        raise ConflictError(f"Agent {field} is not a supported HS code")
    return text


def _safe_public_evidence_uri(value: object) -> str:
    source_uri = _bounded_provenance_text(
        value,
        field="Evidence source URI",
        maximum=2048,
    )
    if source_uri is None:
        raise ConflictError("Agent Evidence source URI is missing")
    parsed = urlsplit(source_uri)
    if (
        parsed.scheme != "https"
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
    ):
        raise ConflictError("Agent Evidence source URI is not a safe HTTPS reference")
    # Query strings and fragments are not needed to identify the legal source and
    # can accidentally carry access tokens.  The public projection deliberately
    # removes them while the provider-owned result remains hash-bound internally.
    return urlunsplit((parsed.scheme, parsed.netloc, parsed.path, "", ""))


def _finite_score(value: object, *, field: str, optional: bool = False) -> float | None:
    if value is None and optional:
        return None
    if isinstance(value, bool) or not isinstance(value, int | float):
        raise ConflictError(f"Agent {field} is not numeric")
    score = float(value)
    if not math.isfinite(score) or abs(score) > 1_000_000_000:
        raise ConflictError(f"Agent {field} is outside the public score boundary")
    return score


def _safe_evidence_provenance(envelope: AgentReviewResultV3) -> dict[str, Any]:
    """Project only review-safe query and ranking facts from the Agent result.

    The browser must not infer an actual RAG run merely from a profile label.
    This projection is created only from the already validated, result-hash-bound
    retrieval request/results.  It contains no provider URL, credential, raw
    response, model embedding, database identity, or arbitrary executable input.
    """

    request = envelope.retrieval_request
    if request is None:
        if envelope.retrieval_results or envelope.rag_evidence:
            raise ConflictError("Agent retrieval provenance is missing its request")
        return {
            "query_intent": None,
            "items": [],
            "ranked_evidence": [],
        }
    if not isinstance(request, dict):
        raise ConflictError("Agent retrieval request is malformed")
    request_id = _bounded_provenance_text(
        request.get("request_id"),
        field="retrieval request ID",
        maximum=128,
    )
    if request_id is None or _EVIDENCE_REQUEST_ID.fullmatch(request_id) is None:
        raise ConflictError("Agent retrieval request ID is invalid")
    query_intent = _bounded_provenance_text(
        request.get("query_intent"),
        field="retrieval query intent",
        maximum=256,
    )
    request_items = request.get("items")
    if not isinstance(request_items, list) or not 1 <= len(request_items) <= 100:
        raise ConflictError("Agent retrieval query items are malformed")
    result_by_item: dict[str, dict[str, Any]] = {}
    for raw_result in envelope.retrieval_results:
        if not isinstance(raw_result, dict):
            raise ConflictError("Agent retrieval result is malformed")
        item_id = _bounded_provenance_text(
            raw_result.get("item_id"),
            field="retrieval result item ID",
            maximum=256,
        )
        if item_id is None:
            raise ConflictError("Agent retrieval result item ID is missing")
        if item_id in result_by_item:
            raise ConflictError("Agent retrieval result item IDs are duplicated")
        result_by_item[item_id] = raw_result

    query_items: list[dict[str, Any]] = []
    request_item_ids: set[str] = set()
    for raw_item in request_items:
        if not isinstance(raw_item, dict):
            raise ConflictError("Agent retrieval query item is malformed")
        item_id = _bounded_provenance_text(
            raw_item.get("item_id"),
            field="retrieval query item ID",
            maximum=256,
        )
        if item_id is None:
            raise ConflictError("Agent retrieval query item ID is missing")
        if item_id in request_item_ids:
            raise ConflictError("Agent retrieval query item IDs are duplicated")
        request_item_ids.add(item_id)
        result = result_by_item.get(item_id)
        if result is None:
            raise ConflictError("Agent retrieval query/result items differ")
        hs_resolution = _bounded_provenance_text(
            result.get("hs_resolution"),
            field="HS resolution",
            maximum=32,
        )
        if hs_resolution not in {
            "exact",
            "unique_descendant",
            "ambiguous",
            "invalid",
            "missing",
        }:
            raise ConflictError("Agent HS resolution is unsupported")
        coverage = _bounded_provenance_text(
            result.get("coverage"),
            field="retrieval coverage",
            maximum=16,
        )
        if coverage not in {"complete", "partial", "unknown"}:
            raise ConflictError("Agent retrieval coverage is unsupported")
        requirements_found = result.get("requirements_found")
        if not isinstance(requirements_found, bool):
            raise ConflictError("Agent requirements_found is not boolean")
        query_items.append(
            {
                "item_id": item_id,
                "query_description": _bounded_provenance_text(
                    raw_item.get("description"),
                    field="retrieval query description",
                    maximum=2000,
                ),
                "requested_hs_code": _bounded_hs_code(
                    raw_item.get("hs_code"),
                    field="requested HS code",
                    optional=True,
                ),
                "hs_resolution": hs_resolution,
                "resolved_hsk10": _bounded_hs_code(
                    result.get("resolved_hsk10"),
                    field="resolved HSK10",
                    optional=True,
                ),
                "coverage": coverage,
                "requirements_found": requirements_found,
            }
        )
    if set(result_by_item) != request_item_ids:
        raise ConflictError("Agent retrieval query/result items differ")

    ranked_evidence: list[dict[str, Any]] = []
    seen_evidence_ids: set[str] = set()
    for item in query_items:
        raw_result = result_by_item[item["item_id"]]
        raw_evidence = raw_result.get("evidence")
        if not isinstance(raw_evidence, list) or len(raw_evidence) > 1000:
            raise ConflictError("Agent ranked Evidence is malformed")
        for rank, evidence in enumerate(raw_evidence, start=1):
            if not isinstance(evidence, dict):
                raise ConflictError("Agent ranked Evidence item is malformed")
            evidence_id = _bounded_provenance_text(
                evidence.get("evidence_id"),
                field="Evidence ID",
                maximum=256,
            )
            if evidence_id is None:
                raise ConflictError("Agent Evidence ID is missing")
            if evidence_id in seen_evidence_ids:
                raise ConflictError("Agent ranked Evidence IDs are duplicated")
            seen_evidence_ids.add(evidence_id)
            source_type = _bounded_provenance_text(
                evidence.get("source_type"),
                field="Evidence source type",
                maximum=64,
            )
            if source_type not in _SAFE_EVIDENCE_SOURCE_TYPES:
                raise ConflictError("Agent Evidence source type is unsupported")
            methods = evidence.get("retrieval_methods")
            if (
                not isinstance(methods, list)
                or not methods
                or len(methods) != len(set(methods))
                or any(method not in _SAFE_RETRIEVAL_METHODS for method in methods)
            ):
                raise ConflictError("Agent Evidence retrieval methods are unsupported")
            raw_scores = evidence.get("raw_scores")
            if not isinstance(raw_scores, dict) or any(
                key not in _SAFE_RETRIEVAL_SCORE_KEYS for key in raw_scores
            ):
                raise ConflictError("Agent Evidence raw score methods are unsupported")
            score_projection = {
                key: _finite_score(value, field=f"Evidence raw score {key}")
                for key, value in sorted(raw_scores.items())
            }
            version_hash = _bounded_provenance_text(
                evidence.get("version_hash"),
                field="Evidence version hash",
                maximum=64,
            )
            if version_hash is None or _SHA256.fullmatch(version_hash) is None:
                raise ConflictError("Agent Evidence version hash is invalid")
            ranked_evidence.append(
                {
                    "evidence_id": evidence_id,
                    "item_id": item["item_id"],
                    "rank": rank,
                    "source_type": source_type,
                    "retrieval_methods": list(methods),
                    "score": _finite_score(
                        evidence.get("score"),
                        field="Evidence score",
                        optional=True,
                    ),
                    "raw_scores": score_projection,
                    "law_id": _bounded_provenance_text(
                        evidence.get("law_id"),
                        field="Evidence law ID",
                        maximum=128,
                        optional=True,
                    ),
                    "law_name": _bounded_provenance_text(
                        evidence.get("law_name"),
                        field="Evidence law name",
                        maximum=512,
                        optional=True,
                    ),
                    "article_no": _bounded_provenance_text(
                        evidence.get("article_no"),
                        field="Evidence article number",
                        maximum=128,
                        optional=True,
                    ),
                    "version_hash": version_hash,
                    "source_uri": _safe_public_evidence_uri(evidence.get("source_uri")),
                }
            )

    provider_evidence_ids = {
        item.get("evidence_id") for item in envelope.rag_evidence if isinstance(item, dict)
    }
    if (
        None in provider_evidence_ids
        or len(provider_evidence_ids) != len(envelope.rag_evidence)
        or provider_evidence_ids != seen_evidence_ids
    ):
        raise ConflictError("Agent ranked Evidence differs from public Evidence")
    return {
        "request_id": request_id,
        "query_intent": query_intent,
        "items": query_items,
        "ranked_evidence": ranked_evidence,
    }


def _is_sha256(value: object) -> bool:
    return (
        isinstance(value, str)
        and len(value) == 64
        and all(character in "0123456789abcdef" for character in value)
    )


def _bounded_model_execution_summary(
    calls: list[dict[str, Any]],
    *,
    expected_model_id: object,
    expected_prompt_sha256: object,
    expected_schema_sha256: object,
    expected_task_id: str | None = None,
) -> ModelExecutionSummary:
    """Reduce call audits to a bounded public projection without call identifiers."""

    providers = {call.get("provider") for call in calls}
    model_ids = {call.get("model_id") for call in calls if isinstance(call.get("model_id"), str)}
    provider: Literal["anthropic"] | None = (
        "anthropic" if calls and providers == {"anthropic"} else None
    )
    model_id = next(iter(model_ids)) if calls and len(model_ids) == 1 else None
    text_only = bool(calls) and all(call.get("input_modality", "text") == "text" for call in calls)
    input_tokens_total = sum(
        value
        for call in calls
        if isinstance((value := call.get("input_tokens")), int) and not isinstance(value, bool)
    )
    output_tokens_total = sum(
        value
        for call in calls
        if isinstance((value := call.get("output_tokens")), int) and not isinstance(value, bool)
    )
    prompt_hashes_complete = bool(calls) and all(
        _is_sha256(call.get("prompt_sha256")) for call in calls
    )
    schema_hashes_complete = bool(calls) and all(
        _is_sha256(call.get("output_schema_sha256")) for call in calls
    )
    runtime_identity_match = bool(calls) and all(
        call.get("provider") == "anthropic"
        and call.get("model_id") == expected_model_id
        and call.get("prompt_sha256") == expected_prompt_sha256
        and call.get("output_schema_sha256") == expected_schema_sha256
        and (expected_task_id is None or call.get("task_id") == expected_task_id)
        for call in calls
    )
    values: dict[str, Any] = {
        "provider": provider,
        "model_id": model_id,
        "text_only": text_only,
        "call_count": len(calls),
        "input_tokens_total": input_tokens_total,
        "output_tokens_total": output_tokens_total,
        "prompt_hashes_complete": prompt_hashes_complete,
        "schema_hashes_complete": schema_hashes_complete,
        "runtime_identity_match": runtime_identity_match,
    }
    reasons = ModelExecutionSummary.expected_unmet_reason_codes(**values)
    return ModelExecutionSummary(
        **values,
        audit_complete=not reasons,
        unmet_reason_codes=reasons,
    )


def _warn_terminal_state_not_persisted(*, boundary: str, error: Exception) -> None:
    """Record only an exception type; never serialize DB errors or request content."""

    LOGGER.warning(
        "terminal_state_persistence_failed",
        extra={"boundary": boundary, "error_type": type(error).__name__},
    )


def _id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex}"


def _scoped_idempotency_key(
    *, endpoint: str, principal: Principal, case_id: str, supplied_key: str
) -> str:
    """Prevent one caller's key from colliding across actors, endpoints or Cases."""

    return "idem_" + canonical_digest(
        {
            "endpoint": endpoint,
            "actor_id": principal.subject,
            "org_id": principal.org_id,
            "case_id": case_id,
            "supplied_key": supplied_key,
        }
    )


MAX_REVIEW_PRIMARY_DOCUMENTS = 6
MAX_REVIEW_TEXT_CHARACTERS = 500_000
MAX_AGENT_REQUEST_BODY_BYTES = 2 * 1024 * 1024
APPROVED_AUTO_COMPLETION_PRIMARY_TYPES: tuple[Literal["CI", "PL", "BL", "CO"], ...] = (
    "CI",
    "PL",
    "BL",
    "CO",
)
MANDATORY_AGENT_STAGE_SEQUENCE = (
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


def _primary_review_documents(
    documents: list[DocumentRecord],
    supporting_documents: list[SupportingDocumentRecord],
) -> list[DocumentRecord]:
    by_id = {item.document_id: item for item in documents}
    supporting_ids = {item.document_id for item in supporting_documents}
    for supporting in supporting_documents:
        source = by_id.get(supporting.document_id)
        if (
            source is None
            or source.document_type != "SUPPORTING"
            or source.content_sha256 != supporting.source_sha256
        ):
            raise ConflictError(
                "supporting metadata is not bound to its immutable SUPPORTING source"
            )
    if any(
        item.document_type == "SUPPORTING" and item.document_id not in supporting_ids
        for item in documents
    ):
        raise ConflictError("every SUPPORTING source requires registered supporting metadata")
    return [item for item in documents if item.document_id not in supporting_ids]


def _validated_primary_review_documents(
    documents: list[DocumentRecord],
    supporting_documents: list[SupportingDocumentRecord],
) -> list[DocumentRecord]:
    primary_documents = _primary_review_documents(documents, supporting_documents)
    if not primary_documents:
        raise ConflictError("at least one primary text document is required")
    if len(primary_documents) > MAX_REVIEW_PRIMARY_DOCUMENTS:
        raise ConflictError("Agent text release accepts at most 6 primary documents")
    if any(
        item.character_count > MAX_TEXT_DOCUMENT_CHARACTERS
        or item.byte_size > MAX_TEXT_DOCUMENT_UTF8_BYTES
        for item in primary_documents
    ):
        raise ConflictError("Case contains a document outside the Agent text release")
    if sum(item.character_count for item in primary_documents) > MAX_REVIEW_TEXT_CHARACTERS:
        raise ConflictError("Case primary text exceeds the Agent release total limit")
    return primary_documents


def _public_input_document_manifest(
    primary_documents: list[DocumentRecord],
) -> list[InputDocumentManifestEntry]:
    return sorted(
        [
            InputDocumentManifestEntry(
                document_id=item.document_id,
                document_type=item.document_type,
                content_sha256=item.content_sha256,
            )
            for item in primary_documents
            if item.document_type != "SUPPORTING"
        ],
        key=lambda item: item.document_id,
    )


def _public_input_manifest_digest(
    *,
    case_id: str,
    case_revision: int,
    review_as_of: date,
    trade_type: str,
    documents: list[InputDocumentManifestEntry],
) -> str:
    return canonical_digest(
        {
            "case_id": case_id,
            "case_revision": case_revision,
            "review_as_of": review_as_of.isoformat(),
            "trade_type": trade_type,
            "documents": [item.model_dump(mode="json") for item in documents],
        }
    )


def _sanitized_review_stage_trace(audit_trail: list[dict[str, Any]]) -> list[ReviewStageTraceEntry]:
    trace: list[ReviewStageTraceEntry] = []
    for raw in audit_trail:
        if not isinstance(raw, dict) or raw.get("stage") not in MANDATORY_AGENT_STAGE_SEQUENCE:
            continue
        raw_metadata = raw.get("metadata")
        metadata: dict[str, Any] = raw_metadata if isinstance(raw_metadata, dict) else {}
        reasons: set[str] = set()
        for key in ("reason_codes", "outcome_reasons"):
            values = metadata.get(key, [])
            if isinstance(values, list):
                reasons.update(str(value) for value in values if isinstance(value, str) and value)
        error_code = metadata.get("error_code")
        if isinstance(error_code, str) and error_code:
            reasons.add(error_code)
        trace.append(
            ReviewStageTraceEntry(
                sequence=len(trace) + 1,
                stage=raw["stage"],
                status=raw.get("event", "failed"),
                reason_ids=sorted(reasons),
            )
        )
    return trace


def _auto_completion_content_gate_reasons(
    *,
    envelope: AgentReviewResultV3,
    manifest: list[InputDocumentManifestEntry],
    stage_trace: list[ReviewStageTraceEntry],
) -> set[str]:
    reasons: set[str] = set()
    model_execution_summary = _bounded_model_execution_summary(
        envelope.model_calls,
        expected_model_id=envelope.runtime_release.model_id,
        expected_prompt_sha256=envelope.runtime_release.extraction_prompt_sha256,
        expected_schema_sha256=envelope.runtime_release.extraction_schema_sha256,
    )
    manifest_ids = {item.document_id for item in manifest}
    model_document_ids = [call.get("document_id") for call in envelope.model_calls]
    if (
        not model_execution_summary.audit_complete
        or model_execution_summary.call_count < len(manifest)
        or any(not isinstance(document_id, str) for document_id in model_document_ids)
        or len(model_document_ids) != len(set(model_document_ids))
        or set(model_document_ids) != manifest_ids
    ):
        reasons.add("backend_model_execution_audit_incomplete")
    manifest_by_id = {item.document_id: item for item in manifest}
    if len(manifest_by_id) != len(manifest) or sorted(
        item.document_type for item in manifest
    ) != sorted(APPROVED_AUTO_COMPLETION_PRIMARY_TYPES):
        reasons.add("backend_primary_document_profile_mismatch")

    results = envelope.document_results
    result_ids = [str(item.get("document_id")) for item in results if isinstance(item, dict)]
    if (
        len(results) != len(manifest)
        or len(result_ids) != len(set(result_ids))
        or set(result_ids) != set(manifest_by_id)
    ):
        reasons.add("backend_document_result_manifest_mismatch")
    else:
        for result in results:
            if not isinstance(result, dict):
                reasons.add("backend_document_result_manifest_mismatch")
                continue
            document_id = str(result.get("document_id"))
            source = manifest_by_id[document_id]
            if (
                result.get("document_type") != _DECLARED_TYPE_MAP.get(source.document_type)
                or result.get("input_modality") != "text"
                or result.get("extraction_status") != "completed"
            ):
                reasons.add("backend_document_result_type_or_status_mismatch")
            fields = result.get("fields")
            if not isinstance(fields, list) or not fields:
                reasons.add("backend_material_field_grounding_missing")
                continue
            for field in fields:
                field_source = field.get("source") if isinstance(field, dict) else None
                if (
                    not isinstance(field_source, dict)
                    or field_source.get("document_id") != document_id
                    or field_source.get("source_modality") != "text"
                    or not isinstance(field_source.get("text_start"), int)
                    or not isinstance(field_source.get("text_end"), int)
                    or field_source["text_start"] < 0
                    or field_source["text_end"] <= field_source["text_start"]
                ):
                    reasons.add("backend_material_field_grounding_missing")

    ledger_documents = {
        str(item.get("document_id"))
        for item in envelope.fact_ledger.get("entries", [])
        if isinstance(item, dict)
        and item.get("kind") == "field_source"
        and isinstance(item.get("document_id"), str)
    }
    if set(manifest_by_id) - ledger_documents:
        reasons.add("backend_material_fact_ledger_coverage_missing")

    if [item.stage for item in stage_trace] != list(MANDATORY_AGENT_STAGE_SEQUENCE) or any(
        item.status != "completed" for item in stage_trace
    ):
        reasons.add("backend_mandatory_stage_trace_invalid")

    for section in ("summary", "document_consistency", "regulatory_review"):
        claims = envelope.narrative_report.get(section)
        if (
            not isinstance(claims, list)
            or not claims
            or any(not isinstance(claim, dict) or not claim.get("citations") for claim in claims)
        ):
            reasons.add(f"backend_narrative_{section}_citation_missing")
    if not any(
        isinstance(item, dict) and item.get("eligibility") != "blocked"
        for item in envelope.rag_evidence
    ):
        reasons.add("backend_regulatory_evidence_missing")
    return reasons


def _current_outcome_reference(fact_ledger: dict[str, Any]) -> str:
    entries = fact_ledger.get("entries")
    if not isinstance(entries, list):
        raise ConflictError("Agent FactLedger is malformed")
    matches = [
        item.get("reference_id")
        for item in entries
        if isinstance(item, dict)
        and item.get("kind") == "outcome"
        and item.get("label") == "deterministic_outcome"
        and isinstance(item.get("reference_id"), str)
    ]
    if len(matches) != 1:
        raise ConflictError("Agent FactLedger requires exactly one current outcome")
    return str(matches[0])


def _backend_policy_narrative_overlay(
    provider_narrative: dict[str, Any],
    *,
    fact_ledger: dict[str, Any],
    disposition: ReviewDisposition,
) -> dict[str, Any]:
    """Replace stale provider action prose after a Backend state downgrade.

    Provider summary/document/regulatory claims have already passed the independent
    semantic projection.  Only the effective-action section is replaced; it cites
    the canonical current outcome and uses one immutable Backend-owned sentence.
    """

    outcome_reference = _current_outcome_reference(fact_ledger)
    text = (
        "차단 원인을 해소한 뒤 새로운 Review Run으로 다시 실행해야 합니다."
        if disposition == ReviewDisposition.blocked
        else (
            "Backend의 유효한 Release·입력·자동완료 Gate가 충족되지 않아 담당자 검토가 필요합니다."
        )
    )
    citations = [{"kind": "outcome", "reference_id": outcome_reference}]
    claim_payload = {"text": text, "material": True, "citations": citations}
    overlay = {
        key: value
        for key, value in provider_narrative.items()
        if key not in {"generation_mode", "generated_by", "model_call", "required_actions"}
    }
    overlay.update(
        {
            "required_actions": [
                {
                    "claim_id": f"claim_{canonical_digest(claim_payload)}",
                    **claim_payload,
                }
            ],
            "generated_by": "backend_policy_overlay",
            "citation_verification": "passed",
        }
    )
    return overlay


_DECLARED_TYPE_MAP: dict[
    str,
    Literal[
        "invoice",
        "packing_list",
        "bill_of_lading",
        "certificate_of_origin",
        "unknown",
    ],
] = {
    "CI": "invoice",
    "PL": "packing_list",
    "BL": "bill_of_lading",
    "CO": "certificate_of_origin",
    "UNKNOWN": "unknown",
}


def _agent_review_request_from_snapshot(
    *,
    object_store: ObjectStore,
    review_run_id: str,
    case_id: str,
    snapshot: dict[str, Any],
) -> AgentReviewRequest:
    records = [DocumentRecord.model_validate(value) for value in snapshot.get("documents", [])]
    if not records:
        raise ConflictError("job has no immutable document snapshot")
    supporting = [
        SupportingDocumentRecord.model_validate(value)
        for value in snapshot.get("supporting_documents", [])
    ]
    primary = _validated_primary_review_documents(records, supporting)
    by_id = {item.document_id: item for item in records}
    for document in supporting:
        source = by_id[document.document_id]
        object_store.get_text(key=source.object_key, expected_sha256=document.source_sha256)
    review_as_of = snapshot.get("review_as_of")
    assertion_cutoff = snapshot.get("assertion_cutoff_at")
    if not review_as_of:
        raise ConflictError("job immutable snapshot is missing review_as_of")
    if not assertion_cutoff:
        raise ConflictError("job immutable snapshot is missing assertion_cutoff_at")
    return AgentReviewRequest(
        review_run_id=review_run_id,
        case_id=case_id,
        documents=[
            AgentDocumentInput(
                document_id=record.document_id,
                filename=record.filename,
                content_text=object_store.get_text(
                    key=record.object_key,
                    expected_sha256=record.content_sha256,
                ),
                declared_type=_DECLARED_TYPE_MAP.get(record.document_type),
                requirement_document_codes=[],
            )
            for record in primary
        ],
        review_as_of=review_as_of,
        assertion_cutoff_at=assertion_cutoff,
        trade_type=snapshot.get("trade_type", "import"),
        uploaded_requirement_document_codes=[],
        supporting_documents=supporting,
        document_language="unknown",
    )


@dataclass(frozen=True)
class DocumentView:
    record: DocumentRecord
    content_text: str


class WorkbenchService:
    def __init__(
        self,
        *,
        repository: WorkbenchRepository,
        object_store: ObjectStore,
        publisher: JobPublisher,
        agent: AgentClient,
        auto_completion_enabled: bool = False,
        approved_profile_id: str | None = None,
        service_pin: AgentServicePin | None = None,
        release_pin: AgentReleasePin | None = None,
        auto_completion_approval: AutoCompletionApprovalManifest | None = None,
        human_decision_policy_release: Literal["backend-human-decision-policy/v1"] = (
            "backend-human-decision-policy/v1"
        ),
        assertion_signer: SupportingAssertionSigner | None = None,
        requirement_policy_resolver: RequirementPolicyIdentityResolver | None = None,
        job_lease_seconds: int = 300,
        job_max_attempts: int = 3,
        outbox_lease_seconds: int = 30,
    ) -> None:
        self.repository = repository
        self.object_store = object_store
        self.publisher = publisher
        self.agent = agent
        self.auto_completion_enabled = auto_completion_enabled
        self.approved_profile_id = approved_profile_id
        self.service_pin = service_pin
        self.release_pin = release_pin
        self.auto_completion_approval = auto_completion_approval
        self.human_decision_policy_release = human_decision_policy_release
        self.assertion_signer = assertion_signer or UnavailableSupportingAssertionSigner()
        self.requirement_policy_resolver = (
            requirement_policy_resolver or UnavailableRequirementPolicyIdentityResolver()
        )
        self.job_lease_seconds = job_lease_seconds
        self.job_max_attempts = job_max_attempts
        self.outbox_lease_seconds = outbox_lease_seconds
        if self.release_pin is not None and (
            self.service_pin is None or self.release_pin.service_pin() != self.service_pin
        ):
            raise ValueError("Agent release pin must extend the exact service pin")
        if self.auto_completion_enabled and (
            self.service_pin is None
            or self.release_pin is None
            or self.auto_completion_approval is None
            or self.approved_profile_id is None
            or self.auto_completion_approval.profile_id != self.approved_profile_id
            or self.auto_completion_approval.release_pin != self.release_pin
            or self.auto_completion_approval.backend_decision_policy_release
            != self.human_decision_policy_release
        ):
            raise ValueError("auto completion requires one exact two-reviewer release approval")
        self.assert_auto_completion_ready()

    def assert_auto_completion_ready(self) -> None:
        if not self.auto_completion_enabled:
            return
        if self.auto_completion_approval is None:
            raise ReleaseConfigurationError("auto-completion approval is unavailable")
        self.auto_completion_approval.verify_with_trust_store(load_domain_reviewer_trust_store())
        if (
            self.release_pin is None
            or not self.assertion_signer.ready
            or not self.requirement_policy_resolver.ready
            or self.assertion_signer.trust_store_sha256
            != self.release_pin.assertion_trust_store_sha256
        ):
            raise ReleaseConfigurationError(
                "auto completion requires a release-bound assertion signer and policy resolver"
            )

    def _auto_completion_approval_is_current(self) -> bool:
        try:
            self.assert_auto_completion_ready()
        except ReleaseConfigurationError:
            return False
        return True

    @staticmethod
    def _audit(
        principal: Principal,
        action: str,
        *,
        case_id: str | None = None,
        review_run_id: str | None = None,
        detail: dict[str, Any] | None = None,
    ) -> AuditRecord:
        return AuditRecord(
            audit_id=_id("audit"),
            org_id=principal.org_id,
            case_id=case_id,
            review_run_id=review_run_id,
            actor_id=principal.subject,
            action=action,
            detail=detail or {},
            created_at=utc_now(),
        )

    def create_case(self, principal: Principal, request: CaseCreate) -> CaseRecord:
        if request.assigned_reviewer_subjects and Role.admin not in principal.roles:
            raise ValueError("only an admin may assign additional reviewers")
        now = utc_now()
        record = CaseRecord(
            case_id=_id("case"),
            org_id=principal.org_id,
            title=request.title,
            external_reference=request.external_reference,
            status=CaseStatus.open,
            revision=0,
            document_manifest_digest=canonical_digest([]),
            metadata=request.metadata,
            owner_subject=principal.subject,
            assigned_reviewer_subjects=sorted(
                {principal.subject, *request.assigned_reviewer_subjects}
            ),
            created_by=principal.subject,
            created_at=now,
            updated_at=now,
        )
        return self.repository.create_case(
            record,
            self._audit(principal, "case.created", case_id=record.case_id),
        )

    def get_case(self, principal: Principal, case_id: str) -> CaseRecord:
        record = self.repository.get_case(principal.org_id, case_id)
        if record is None:
            raise NotFoundError("case not found")
        self._require_case_access(principal, record)
        return record

    @staticmethod
    def _require_case_access(principal: Principal, record: CaseRecord) -> None:
        if principal.roles.intersection({Role.admin, Role.approver, Role.auditor}):
            return
        if (
            Role.reviewer in principal.roles
            and principal.subject in record.assigned_reviewer_subjects
        ):
            return
        raise NotFoundError("case not found")

    def list_cases(self, principal: Principal) -> list[CaseRecord]:
        records = self.repository.list_cases(principal.org_id)
        if principal.roles.intersection({Role.admin, Role.approver, Role.auditor}):
            return records
        return [
            record for record in records if principal.subject in record.assigned_reviewer_subjects
        ]

    def add_document(
        self,
        principal: Principal,
        case_id: str,
        request: TextDocumentCreate,
    ) -> tuple[CaseRecord, DocumentRecord]:
        case = self.get_case(principal, case_id)
        if case.status == CaseStatus.closed:
            raise ConflictError("documents cannot be added to a closed case")
        payload = request.content_text.encode("utf-8")
        digest = hashlib.sha256(payload).hexdigest()
        document_id = _id("doc")
        object_key = f"{principal.org_id}/{case_id}/{document_id}/{digest}.txt"
        record = DocumentRecord(
            document_id=document_id,
            case_id=case_id,
            org_id=principal.org_id,
            document_type=request.document_type,
            filename=request.filename,
            media_type=request.media_type,
            content_sha256=digest,
            object_key=object_key,
            byte_size=len(payload),
            character_count=len(request.content_text),
            created_by=principal.subject,
            created_at=utc_now(),
        )
        existing = self.repository.list_documents(principal.org_id, case_id)
        manifest = [
            {"document_id": item.document_id, "content_sha256": item.content_sha256}
            for item in [*existing, record]
        ]
        new_manifest_digest = canonical_digest(
            sorted(manifest, key=lambda item: item["document_id"])
        )
        self.object_store.put_text(
            key=object_key, content=request.content_text, content_sha256=digest
        )
        updated = self.repository.add_document(
            record,
            expected_revision=case.revision,
            new_manifest_digest=new_manifest_digest,
            audit=self._audit(
                principal,
                "document.added",
                case_id=case_id,
                detail={"document_id": document_id, "content_sha256": digest},
            ),
        )
        return updated, record

    def list_document_views(self, principal: Principal, case_id: str) -> list[DocumentView]:
        self.get_case(principal, case_id)
        records = self.repository.list_documents(principal.org_id, case_id)
        return [
            DocumentView(
                record=record,
                content_text=self.object_store.get_text(
                    key=record.object_key,
                    expected_sha256=record.content_sha256,
                ),
            )
            for record in records
        ]

    def list_document_records(self, principal: Principal, case_id: str) -> list[DocumentRecord]:
        self.get_case(principal, case_id)
        return self.repository.list_documents(principal.org_id, case_id)

    def get_document_view(
        self, principal: Principal, case_id: str, document_id: str
    ) -> DocumentView:
        record = next(
            (
                item
                for item in self.list_document_records(principal, case_id)
                if item.document_id == document_id
            ),
            None,
        )
        if record is None:
            raise NotFoundError("document not found")
        return DocumentView(
            record=record,
            content_text=self.object_store.get_text(
                key=record.object_key,
                expected_sha256=record.content_sha256,
            ),
        )

    def add_supporting_document(
        self,
        principal: Principal,
        case_id: str,
        request: SupportingDocumentCreate,
    ) -> SupportingDocumentRecord:
        case = self.get_case(principal, case_id)
        source = next(
            (
                item
                for item in self.repository.list_documents(principal.org_id, case_id)
                if item.document_id == request.document_id
            ),
            None,
        )
        if source is None or source.content_sha256 != request.source_sha256:
            raise ConflictError("supporting document must bind an immutable Case document hash")
        if source.document_type != "SUPPORTING":
            raise ConflictError("supporting metadata may bind only a SUPPORTING source document")
        record = SupportingDocumentRecord(
            **request.model_dump(),
            case_id=case_id,
            org_id=principal.org_id,
            verification_assertions=[],
            created_by=principal.subject,
            created_at=utc_now(),
        )
        return self.repository.save_supporting_document(
            record,
            self._audit(
                principal,
                "supporting_document.registered",
                case_id=case_id,
                detail={
                    "document_id": record.document_id,
                    "document_code": record.document_code,
                    "source_sha256": record.source_sha256,
                },
            ),
            expected_revision=case.revision,
        )

    def list_supporting_documents(
        self, principal: Principal, case_id: str
    ) -> list[SupportingDocumentRecord]:
        self.get_case(principal, case_id)
        return self.repository.list_supporting_documents(principal.org_id, case_id)

    def assert_supporting_document(
        self,
        principal: Principal,
        case_id: str,
        document_id: str,
        request: VerificationAssertionCreate,
    ) -> SupportingDocumentRecord:
        case = self.get_case(principal, case_id)
        supporting_document = self.repository.get_supporting_document(
            principal.org_id, case_id, document_id
        )
        if supporting_document is None:
            raise NotFoundError("supporting document not found")
        source_document = next(
            (
                item
                for item in self.repository.list_documents(principal.org_id, case_id)
                if item.document_id == document_id
            ),
            None,
        )
        if principal.subject in {
            supporting_document.created_by,
            source_document.created_by if source_document is not None else "",
        }:
            # Enforce maker-checker before any policy resolution or signing. The
            # repository repeats this check under its transaction/row lock so a
            # concurrent write cannot bypass it.
            raise ConflictError("supporting verification requires an independent approver")
        asserted_at = utc_now()
        authority_id = (
            "backend:principal-sha256:"
            + hashlib.sha256(principal.subject.encode("utf-8")).hexdigest()
        )
        assertion_payload: dict[str, Any] = {
            "method": request.method,
            "subject_document_id": document_id,
            "asserted_by": principal.subject,
            "asserted_at": asserted_at.isoformat().replace("+00:00", "Z"),
            "authority_id": authority_id,
            "authority_release": self.human_decision_policy_release,
            "source_reference_sha256": supporting_document.source_sha256,
        }
        policy_identity = self.requirement_policy_resolver.resolve(supporting_document)
        decision_record = (
            SupportingVerificationDecisionRecord(
                case_id=case_id,
                document_id=document_id,
                source_sha256=supporting_document.source_sha256,
                supporting_document_claims_sha256=supporting_document.claims_sha256,
                actor_id=principal.subject,
                policy_source_sha256=policy_identity.policy_source_sha256,
                policy_release=policy_identity.policy_release,
                decided_at=asserted_at,
            )
            if policy_identity is not None
            else None
        )
        receipt = (
            self.assertion_signer.sign_human_decision(
                case_id=case_id,
                supporting_document=supporting_document,
                decision_record=decision_record,
                authority_id=authority_id,
                authority_release=self.human_decision_policy_release,
                signed_at=asserted_at,
            )
            if decision_record is not None
            else None
        )
        if receipt is not None:
            assertion_payload["receipt"] = receipt.model_dump(
                mode="json",
                exclude={"signature_base64url"},
                exclude_none=True,
            )
        assertion = VerificationAssertionRecord(
            assertion_id=f"assert_{canonical_digest(assertion_payload)}",
            method=request.method,
            subject_document_id=document_id,
            asserted_by=principal.subject,
            asserted_at=asserted_at,
            authority_id=authority_id,
            authority_release=self.human_decision_policy_release,
            source_reference_sha256=supporting_document.source_sha256,
            receipt=receipt,
            decision_record=decision_record,
        )
        return self.repository.add_verification_assertion(
            org_id=principal.org_id,
            case_id=case_id,
            document_id=document_id,
            assertion=assertion,
            audit=self._audit(
                principal,
                "supporting_document.asserted",
                case_id=case_id,
                detail={
                    "document_id": document_id,
                    "assertion_id": assertion.assertion_id,
                    "method": assertion.method,
                    "receipt_status": "signed" if receipt is not None else "unavailable",
                    "decision_record_sha256": (
                        decision_record.sha256 if decision_record is not None else None
                    ),
                },
            ),
            expected_revision=case.revision,
        )

    def enqueue_review(
        self,
        principal: Principal,
        case_id: str,
        request: ReviewEnqueueRequest,
        idempotency_key: str,
    ) -> tuple[JobRecord, bool]:
        # Authorization is checked before entering the repository transaction;
        # the repository then locks the Case and snapshots all three aggregate
        # components in the same transaction that creates the immutable Job.
        self.get_case(principal, case_id)
        now = utc_now()
        scoped_idempotency_key = _scoped_idempotency_key(
            endpoint="review.enqueue/v1",
            principal=principal,
            case_id=case_id,
            supplied_key=idempotency_key,
        )
        idempotency_request_digest = canonical_digest(
            {"case_id": case_id, "request": request.model_dump(mode="json")}
        )

        def build(
            case: CaseRecord,
            documents: list[DocumentRecord],
            supporting_documents: list[SupportingDocumentRecord],
        ) -> tuple[JobRecord, StageEvent, OutboxRecord, AuditRecord]:
            if case.status not in {CaseStatus.open, CaseStatus.in_review}:
                raise ConflictError("reviews cannot be enqueued for a closed case")
            primary_documents = _validated_primary_review_documents(documents, supporting_documents)
            resolved_review_as_of = request.review_as_of or now.date()
            input_document_manifest = _public_input_document_manifest(primary_documents)
            input_manifest_digest = _public_input_manifest_digest(
                case_id=case_id,
                case_revision=case.revision,
                review_as_of=resolved_review_as_of,
                trade_type=request.trade_type,
                documents=input_document_manifest,
            )
            request_payload = {
                **request.model_dump(mode="json"),
                "review_as_of": resolved_review_as_of.isoformat(),
                "case_id": case_id,
                "case_revision": case.revision,
                "input_document_manifest": [
                    item.model_dump(mode="json") for item in input_document_manifest
                ],
                "input_manifest_digest": input_manifest_digest,
                "documents": [item.model_dump(mode="json") for item in documents],
                "supporting_documents": [
                    item.model_dump(mode="json") for item in supporting_documents
                ],
                "assertion_cutoff_at": now.isoformat(),
            }
            logical_request_sha256 = canonical_digest(
                {
                    key: value
                    for key, value in request_payload.items()
                    if key != "assertion_cutoff_at"
                }
            )
            job = JobRecord(
                job_id=_id("job"),
                case_id=case_id,
                org_id=principal.org_id,
                idempotency_key=scoped_idempotency_key,
                idempotency_request_digest=idempotency_request_digest,
                request_digest=canonical_digest(request_payload),
                case_revision=case.revision,
                logical_request_sha256=logical_request_sha256,
                kind="review",
                request=request_payload,
                status=JobStatus.queued,
                review_run_id=_id("run"),
                created_by=principal.subject,
                created_at=now,
                updated_at=now,
            )
            event = StageEvent(
                event_id=_id("event"),
                job_id=job.job_id,
                sequence=1,
                stage="queued",
                state="completed",
                message_code="review.queued",
                occurred_at=now,
            )
            outbox = OutboxRecord(
                outbox_id=_id("outbox"),
                org_id=principal.org_id,
                topic="review.requested.v1",
                aggregate_id=job.job_id,
                payload={"job_id": job.job_id, "org_id": principal.org_id},
                created_at=now,
            )
            audit = self._audit(
                principal,
                "review.enqueued",
                case_id=case_id,
                detail={"job_id": job.job_id, "request_digest": job.request_digest},
            )
            return job, event, outbox, audit

        return self.repository.enqueue_review_snapshot(
            org_id=principal.org_id,
            case_id=case_id,
            idempotency_key=scoped_idempotency_key,
            idempotency_request_digest=idempotency_request_digest,
            build=build,
        )

    def create_correction_proposal(
        self,
        principal: Principal,
        parent_run_id: str,
        request: CorrectionProposalCreate,
        idempotency_key: str,
    ) -> CorrectionProposalRecord:
        parent = self.get_run(principal, parent_run_id)
        case = self.get_case(principal, parent.case_id)
        scoped_idempotency_key = _scoped_idempotency_key(
            endpoint="correction-proposal.create/v1",
            principal=principal,
            case_id=case.case_id,
            supplied_key=idempotency_key,
        )
        idempotency_request_digest = canonical_digest(
            {"review_run_id": parent_run_id, "proposal": request.model_dump(mode="json")}
        )
        existing = self.repository.get_correction_proposal_by_idempotency_key(
            principal.org_id, scoped_idempotency_key
        )
        if existing is not None:
            if existing.idempotency_request_digest != idempotency_request_digest:
                raise ConflictError("proposal idempotency key was reused with another request")
            return existing
        if parent.result_digest != request.parent_result_digest:
            raise ConflictError("parent result digest is stale")
        if parent.agent_input_manifest_sha256 != request.parent_input_manifest_sha256:
            raise ConflictError("parent input manifest is stale")
        if case.revision != request.case_revision:
            raise ConflictError("case revision is stale")
        documents = {
            item.document_id
            for item in self.repository.list_documents(principal.org_id, case.case_id)
        }
        unknown = sorted({item.document_id for item in request.corrections} - documents)
        if unknown:
            raise ValueError(f"correction references unknown document: {', '.join(unknown)}")
        provider_result = self.repository.get_provider_result(principal.org_id, parent_run_id)
        if provider_result is None:
            raise ConflictError("provider result is unavailable for a correction proposal")
        allowed_paths = {
            (str(document.get("document_id")), f"fields.{field.get('name')}")
            for document in provider_result.get("document_results", [])
            if isinstance(document, dict)
            for field in document.get("fields", [])
            if isinstance(field, dict) and isinstance(field.get("name"), str)
        }
        allowed_paths.update(
            {
                (
                    str(document.get("document_id")),
                    f"line_items.{index}.{field.get('name')}",
                )
                for document in provider_result.get("document_results", [])
                if isinstance(document, dict)
                for index, item in enumerate(document.get("line_items", []))
                if isinstance(item, dict)
                for field in item.get("fields", [])
                if isinstance(field, dict) and isinstance(field.get("name"), str)
            }
        )
        invalid_paths = sorted(
            {
                (item.document_id, item.field_path)
                for item in request.corrections
                if (item.document_id, item.field_path) not in allowed_paths
            }
        )
        if invalid_paths:
            raise ValueError("correction must reference an extracted parent result field")
        proposal = CorrectionProposalRecord(
            proposal_id=_id("proposal"),
            org_id=principal.org_id,
            case_id=case.case_id,
            review_run_id=parent_run_id,
            idempotency_key=scoped_idempotency_key,
            idempotency_request_digest=idempotency_request_digest,
            parent_result_digest=request.parent_result_digest,
            parent_input_manifest_sha256=request.parent_input_manifest_sha256,
            case_revision=request.case_revision,
            corrections=request.corrections,
            status=CorrectionProposalStatus.pending,
            created_by=principal.subject,
            created_at=utc_now(),
        )
        return self.repository.create_correction_proposal(
            proposal,
            self._audit(
                principal,
                "correction_proposal.created",
                case_id=case.case_id,
                review_run_id=parent_run_id,
                detail={"proposal_id": proposal.proposal_id},
            ),
        )

    def list_correction_proposals(
        self, principal: Principal, review_run_id: str
    ) -> list[CorrectionProposalRecord]:
        self.get_run(principal, review_run_id)
        return self.repository.list_correction_proposals(principal.org_id, review_run_id)

    def create_revision_proposal(
        self,
        principal: Principal,
        parent_run_id: str,
        request: RevisionProposalCreate,
        idempotency_key: str,
    ) -> CorrectionProposalRecord:
        parent = self.get_run(principal, parent_run_id)
        case = self.get_case(principal, parent.case_id)
        scoped_idempotency_key = _scoped_idempotency_key(
            endpoint="revision-proposal.create/v1",
            principal=principal,
            case_id=case.case_id,
            supplied_key=idempotency_key,
        )
        idempotency_request_digest = canonical_digest(
            {"review_run_id": parent_run_id, "proposal": request.model_dump(mode="json")}
        )
        existing = self.repository.get_correction_proposal_by_idempotency_key(
            principal.org_id, scoped_idempotency_key
        )
        if existing is not None:
            if existing.idempotency_request_digest != idempotency_request_digest:
                raise ConflictError("proposal idempotency key was reused with another request")
            return existing
        if parent.result_digest != request.parent_result_digest:
            raise ConflictError("parent result digest is stale")
        if parent.agent_input_manifest_sha256 != request.parent_input_manifest_sha256:
            raise ConflictError("parent input manifest is stale")
        if case.revision != request.case_revision:
            raise ConflictError("case revision is stale")
        provider_result = self.repository.get_provider_result(principal.org_id, parent_run_id)
        if provider_result is None:
            raise ConflictError("provider result is unavailable for a revision proposal")
        allowed_paths = {
            (str(document.get("document_id")), f"fields.{field.get('name')}")
            for document in provider_result.get("document_results", [])
            if isinstance(document, dict)
            for field in document.get("fields", [])
            if isinstance(field, dict) and isinstance(field.get("name"), str)
        }
        allowed_paths.update(
            {
                (
                    str(document.get("document_id")),
                    f"line_items.{index}.{field.get('name')}",
                )
                for document in provider_result.get("document_results", [])
                if isinstance(document, dict)
                for index, item in enumerate(document.get("line_items", []))
                if isinstance(item, dict)
                for field in item.get("fields", [])
                if isinstance(field, dict) and isinstance(field.get("name"), str)
            }
        )
        allowed_confirmations = {
            str(item.get("reference_id"))
            for item in provider_result.get("fact_ledger", {}).get("entries", [])
            if isinstance(item, dict) and item.get("kind") == "field_source"
        }
        if any(
            (action.correction.document_id, action.correction.field_path) not in allowed_paths
            for action in request.actions
            if action.correction is not None
        ):
            raise ValueError("correction must reference an extracted parent result field")
        if any(
            action.confirmed_reference_id not in allowed_confirmations
            for action in request.actions
            if action.confirmed_reference_id is not None
        ):
            raise ValueError("confirmation must reference a parent FactLedger field")
        parent_job = (
            self.repository.get_job(principal.org_id, parent.job_id)
            if parent.job_id is not None
            else None
        )
        if parent_job is None:
            raise ConflictError("parent run has no immutable input snapshot")
        existing_support_ids = {
            str(item.get("document_id"))
            for item in parent_job.request.get("supporting_documents", [])
            if isinstance(item, dict)
        }
        requested_support_ids = [
            action.supporting_document_id
            for action in request.actions
            if action.supporting_document_id is not None
        ]
        if len(requested_support_ids) != len(set(requested_support_ids)):
            raise ValueError("supporting-document additions must be unique")
        for document_id in requested_support_ids:
            if document_id in existing_support_ids:
                raise ValueError("supporting document is already present in the parent review")
            supporting = self.repository.get_supporting_document(
                principal.org_id,
                case.case_id,
                document_id,
            )
            if supporting is None:
                raise ValueError("supporting-document addition is not registered in this Case")
        proposal = CorrectionProposalRecord(
            proposal_id=_id("proposal"),
            org_id=principal.org_id,
            case_id=case.case_id,
            review_run_id=parent_run_id,
            idempotency_key=scoped_idempotency_key,
            idempotency_request_digest=idempotency_request_digest,
            parent_result_digest=request.parent_result_digest,
            parent_input_manifest_sha256=request.parent_input_manifest_sha256,
            case_revision=request.case_revision,
            actions=request.actions,
            status=CorrectionProposalStatus.pending,
            created_by=principal.subject,
            created_at=utc_now(),
        )
        return self.repository.create_correction_proposal(
            proposal,
            self._audit(
                principal,
                "revision_proposal.created",
                case_id=case.case_id,
                review_run_id=parent_run_id,
                detail={
                    "proposal_id": proposal.proposal_id,
                    "decision_types": [item.decision for item in request.actions],
                },
            ),
        )

    def decide_correction_proposal(
        self,
        principal: Principal,
        proposal_id: str,
        request: CorrectionProposalDecisionCreate,
        idempotency_key: str,
    ) -> tuple[CorrectionProposalRecord, HumanDecisionRecord, JobRecord | None]:
        proposal = self.repository.get_correction_proposal(principal.org_id, proposal_id)
        if proposal is None:
            raise NotFoundError("correction proposal not found")
        if proposal.created_by == principal.subject:
            raise ConflictError("correction proposal requires an independent decision maker")
        scoped_idempotency_key = _scoped_idempotency_key(
            endpoint="revision-proposal.decide/v1",
            principal=principal,
            case_id=proposal.case_id,
            supplied_key=idempotency_key,
        )
        idempotency_request_digest = canonical_digest(
            {
                "proposal_id": proposal.proposal_id,
                "decision": request.model_dump(mode="json"),
            }
        )

        def committed_replay() -> (
            tuple[CorrectionProposalRecord, HumanDecisionRecord, JobRecord | None] | None
        ):
            previous = self.repository.get_decision_by_idempotency_key(
                principal.org_id,
                scoped_idempotency_key,
            )
            if previous is None:
                return None
            if (
                previous.proposal_id != proposal.proposal_id
                or previous.decided_by != principal.subject
                or previous.idempotency_request_digest != idempotency_request_digest
            ):
                raise ConflictError("decision idempotency key was reused with another request")
            decided = self.repository.get_correction_proposal(
                principal.org_id, proposal.proposal_id
            )
            if decided is None or decided.status == CorrectionProposalStatus.pending:
                raise ConflictError("idempotent decision lineage is incomplete")
            child = (
                self.repository.get_job(principal.org_id, decided.child_job_id)
                if decided.child_job_id is not None
                else None
            )
            if decided.status == CorrectionProposalStatus.approved and child is None:
                raise ConflictError("idempotent approval references a missing child Job")
            return decided, previous, child

        replay = committed_replay()
        if replay is not None:
            return replay
        parent = self.get_run(principal, proposal.review_run_id)
        case = self.get_case(principal, proposal.case_id)
        if (
            proposal.status != CorrectionProposalStatus.pending
            or proposal.parent_result_digest != request.parent_result_digest
            or parent.result_digest != request.parent_result_digest
            or proposal.case_revision != request.case_revision
            or case.revision != request.case_revision
        ):
            raise ConflictError("correction proposal decision is stale")
        now = utc_now()
        decision = HumanDecisionRecord(
            decision_id=_id("decision"),
            review_run_id=parent.review_run_id,
            case_id=parent.case_id,
            org_id=principal.org_id,
            decision=request.decision,
            reason=request.reason,
            proposal_id=proposal.proposal_id,
            idempotency_key=scoped_idempotency_key,
            idempotency_request_digest=idempotency_request_digest,
            decided_by=principal.subject,
            created_at=now,
        )
        job: JobRecord | None = None
        event: StageEvent | None = None
        outbox: OutboxRecord | None = None
        updated = proposal.model_copy(
            update={
                "status": (
                    CorrectionProposalStatus.approved
                    if request.decision == "approve"
                    else CorrectionProposalStatus.rejected
                ),
                "decided_by": principal.subject,
                "decided_at": now,
                "decision_reason": request.reason,
            }
        )
        if request.decision == "approve":
            parent_job = (
                self.repository.get_job(principal.org_id, parent.job_id)
                if parent.job_id is not None
                else None
            )
            if parent_job is None:
                raise ConflictError("parent run has no immutable input snapshot")
            _validated_primary_review_documents(
                [
                    DocumentRecord.model_validate(value)
                    for value in parent_job.request.get("documents", [])
                ],
                [
                    SupportingDocumentRecord.model_validate(value)
                    for value in parent_job.request.get("supporting_documents", [])
                ],
            )
            review_run_id = _id("run")
            decision_wires: list[dict[str, Any]] = []
            actions = proposal.actions or [
                RevisionAction(
                    decision="correct_field",
                    reason=correction.reason,
                    correction=correction,
                )
                for correction in proposal.corrections
            ]
            additions: list[SupportingDocumentRecord] = []
            for action in actions:
                correction_wire = (
                    {
                        "document_id": action.correction.document_id,
                        "field_path": action.correction.field_path,
                        "value": action.correction.new_value,
                        "confidence": action.correction.confidence,
                    }
                    if action.correction is not None
                    else None
                )
                supporting_document = (
                    self.repository.get_supporting_document(
                        principal.org_id,
                        case.case_id,
                        action.supporting_document_id,
                    )
                    if action.supporting_document_id is not None
                    else None
                )
                if action.supporting_document_id is not None and supporting_document is None:
                    raise ConflictError("approved supporting-document addition is unavailable")
                if supporting_document is not None:
                    additions.append(supporting_document)
                decision_payload = {
                    "decision": action.decision,
                    "actor_id": principal.subject,
                    "actor_role": "approver",
                    "decided_at": now.isoformat().replace("+00:00", "Z"),
                    "reason": action.reason,
                    "confirmed_reference_id": action.confirmed_reference_id,
                    "correction": correction_wire,
                    "supporting_document": (
                        supporting_document.to_agent_wire()
                        if supporting_document is not None
                        else None
                    ),
                }
                decision_wires.append(
                    {
                        "decision_id": f"decision_{canonical_digest(decision_payload)}",
                        **decision_payload,
                    }
                )
            child_documents = list(parent_job.request.get("documents", []))
            child_supporting = list(parent_job.request.get("supporting_documents", []))
            current_documents = {
                item.document_id: item
                for item in self.repository.list_documents(principal.org_id, case.case_id)
            }
            for supporting in additions:
                source = current_documents.get(supporting.document_id)
                if source is None or source.content_sha256 != supporting.source_sha256:
                    raise ConflictError("supporting-document source changed before approval")
                if not any(
                    isinstance(item, dict) and item.get("document_id") == source.document_id
                    for item in child_documents
                ):
                    child_documents.append(source.model_dump(mode="json"))
                child_supporting.append(supporting.model_dump(mode="json"))
            child_document_records = [
                DocumentRecord.model_validate(value) for value in child_documents
            ]
            child_supporting_records = [
                SupportingDocumentRecord.model_validate(value) for value in child_supporting
            ]
            child_primary = _validated_primary_review_documents(
                child_document_records,
                child_supporting_records,
            )
            child_manifest = _public_input_document_manifest(child_primary)
            child_review_as_of = date.fromisoformat(str(parent_job.request["review_as_of"]))
            child_trade_type = str(parent_job.request["trade_type"])
            request_payload = {
                **parent_job.request,
                "case_revision": case.revision,
                "input_document_manifest": [
                    item.model_dump(mode="json") for item in child_manifest
                ],
                "input_manifest_digest": _public_input_manifest_digest(
                    case_id=case.case_id,
                    case_revision=case.revision,
                    review_as_of=child_review_as_of,
                    trade_type=child_trade_type,
                    documents=child_manifest,
                ),
                "documents": child_documents,
                "supporting_documents": child_supporting,
                "proposal_id": proposal.proposal_id,
                "parent_result_digest": parent.result_digest,
                "parent_provider_result_sha256": parent.provider_result_sha256,
                "parent_input_manifest_sha256": parent.agent_input_manifest_sha256,
                "human_decisions": decision_wires,
            }
            parent_result = self.repository.get_provider_result(
                principal.org_id, parent.review_run_id
            )
            if parent_result is None:
                raise ConflictError("parent provider result is unavailable")
            preflight_revision = AgentRevisionRequest(
                parent_result_sha256=parent.provider_result_sha256,
                parent_input_manifest_sha256=parent.agent_input_manifest_sha256,
                parent_result=parent_result,
                parent_request=_agent_review_request_from_snapshot(
                    object_store=self.object_store,
                    review_run_id=parent.review_run_id,
                    case_id=parent_job.case_id,
                    snapshot=parent_job.request,
                ),
                revised_request=_agent_review_request_from_snapshot(
                    object_store=self.object_store,
                    review_run_id=review_run_id,
                    case_id=case.case_id,
                    snapshot=request_payload,
                ),
                decisions=[AgentHumanDecision.model_validate(value) for value in decision_wires],
            )
            if (
                len(canonical_json_bytes(preflight_revision.to_agent_wire()))
                > MAX_AGENT_REQUEST_BODY_BYTES
            ):
                raise ConflictError(
                    "approved revision exceeds the Agent 2 MiB transport limit; "
                    "reduce the Case through a new bounded review"
                )
            job = JobRecord(
                job_id=_id("job"),
                case_id=case.case_id,
                org_id=principal.org_id,
                idempotency_key=scoped_idempotency_key,
                idempotency_request_digest=idempotency_request_digest,
                request_digest=canonical_digest(request_payload),
                case_revision=case.revision,
                logical_request_sha256=canonical_digest(request_payload),
                kind="revision",
                parent_run_id=parent.review_run_id,
                request=request_payload,
                status=JobStatus.queued,
                review_run_id=review_run_id,
                created_by=principal.subject,
                created_at=now,
                updated_at=now,
            )
            event = StageEvent(
                event_id=_id("event"),
                job_id=job.job_id,
                sequence=1,
                stage="queued",
                state="completed",
                message_code="revision.queued",
                occurred_at=now,
            )
            outbox = OutboxRecord(
                outbox_id=_id("outbox"),
                org_id=principal.org_id,
                topic="review.revision.requested.v2",
                aggregate_id=job.job_id,
                payload={"job_id": job.job_id, "org_id": principal.org_id},
                created_at=now,
            )
            updated = updated.model_copy(update={"child_job_id": job.job_id})
        try:
            stored = self.repository.decide_correction_proposal(
                proposal=updated,
                decision=decision,
                audit=self._audit(
                    principal,
                    "correction_proposal.decided",
                    case_id=proposal.case_id,
                    review_run_id=proposal.review_run_id,
                    detail={
                        "proposal_id": proposal.proposal_id,
                        "decision": request.decision,
                        "child_job_id": job.job_id if job else None,
                    },
                ),
                job=job,
                event=event,
                outbox=outbox,
            )
        except ConflictError:
            replay = committed_replay()
            if replay is not None:
                return replay
            raise
        return stored, decision, job

    def _publish_outbox(self, outbox: OutboxRecord) -> bool:
        if outbox.lease_id is None:
            raise ConflictError("outbox must be claimed before publication")
        try:
            self.publisher.publish(
                org_id=outbox.org_id,
                job_id=outbox.aggregate_id,
                outbox_id=outbox.outbox_id,
            )
        except Exception:
            self.repository.release_outbox_claim(outbox.outbox_id, lease_id=outbox.lease_id)
            return False
        self.repository.mark_outbox_published(outbox.outbox_id, lease_id=outbox.lease_id)
        return True

    def dispatch_pending_outbox(self, limit: int = 100) -> int:
        sent = 0
        lease_id = _id("outboxlease")
        for outbox in self.repository.claim_pending_outbox(
            lease_id=lease_id,
            lease_expires_at=utc_now() + timedelta(seconds=self.outbox_lease_seconds),
            limit=limit,
        ):
            if self._publish_outbox(outbox):
                sent += 1
        return sent

    def execute_job(self, *, org_id: str, job_id: str) -> ReviewRunRecord:
        job = self.repository.get_job(org_id, job_id)
        if job is None:
            raise NotFoundError("job not found")
        if job.status == JobStatus.succeeded:
            run = self.repository.get_run(org_id, job.review_run_id)
            if run is None:
                raise ConflictError("completed job references a missing immutable run")
            return run
        if job.status in {
            JobStatus.failed,
            JobStatus.delivery_uncertain,
            JobStatus.response_received_rejected,
            JobStatus.response_persistence_uncertain,
        }:
            raise JobExecutionNotRunnableError("job cannot be executed from its current state")
        system = Principal(subject="system:review-worker", org_id=org_id, roles=frozenset())
        sequence = len(self.repository.list_stage_events(org_id, job_id)) + 1
        if job.agent_call_state == AgentCallState.started:
            if job.lease_expires_at is not None and job.lease_expires_at > utc_now():
                raise JobExecutionNotRunnableError("job is already claimed by another worker")
            self.repository.mark_agent_delivery_uncertain(
                org_id=org_id,
                job_id=job_id,
                expected_lease_id=None,
                error_code="agent_call_interrupted",
                event=StageEvent(
                    event_id=_id("event"),
                    job_id=job_id,
                    sequence=sequence,
                    stage="failed",
                    state="failed",
                    message_code="review.delivery_uncertain",
                    occurred_at=utc_now(),
                    detail={"error_code": "agent_call_interrupted"},
                ),
                audit=self._audit(
                    system,
                    "job.delivery_uncertain",
                    case_id=job.case_id,
                    detail={"job_id": job_id, "error_code": "agent_call_interrupted"},
                ),
            )
            raise JobExecutionNotRunnableError(
                "Agent delivery state is uncertain; human recovery is required"
            )
        lease_id = _id("joblease")
        running = self.repository.claim_job(
            org_id=org_id,
            job_id=job_id,
            lease_id=lease_id,
            lease_expires_at=utc_now() + timedelta(seconds=self.job_lease_seconds),
            event=StageEvent(
                event_id=_id("event"),
                job_id=job_id,
                sequence=sequence,
                stage="document_analysis",
                state="started",
                message_code="agent.started",
                occurred_at=utc_now(),
            ),
            audit=self._audit(
                system,
                "review.started",
                case_id=job.case_id,
                detail={"job_id": job_id, "attempt": job.attempt_count + 1},
            ),
        )
        if running is None:
            current = self.repository.get_job(org_id, job_id)
            if current is not None and current.status == JobStatus.succeeded:
                completed = self.repository.get_run(org_id, current.review_run_id)
                if completed is not None:
                    return completed
            raise JobExecutionNotRunnableError("job is already claimed by another worker")
        call_started = False
        response_received = False
        response_validated = False
        try:
            case = self.repository.get_case(org_id, job.case_id)
            if case is None:
                raise NotFoundError("case not found")
            agent_request = _agent_review_request_from_snapshot(
                object_store=self.object_store,
                review_run_id=running.review_run_id,
                case_id=job.case_id,
                snapshot=running.request,
            )
            agent_request_wire = agent_request.to_agent_wire()
            validate_agent_v3_payload("review_request", agent_request_wire)
            if len(canonical_json_bytes(agent_request_wire)) > MAX_AGENT_REQUEST_BODY_BYTES:
                raise ConflictError("Agent review request exceeds the 2 MiB transport limit")
            agent_request_wire_sha256 = canonical_digest(agent_request_wire)
            agent_input_manifest_sha256 = agent_request.input_manifest_sha256
            if job.kind == "revision":
                if running.parent_run_id is None:
                    raise ConflictError("revision job has no parent run")
                parent_result = self.repository.get_provider_result(org_id, running.parent_run_id)
                if parent_result is None:
                    raise ConflictError("revision parent provider result is unavailable")
                parent_run = self.repository.get_run(org_id, running.parent_run_id)
                parent_job = (
                    self.repository.get_job(org_id, parent_run.job_id)
                    if parent_run is not None and parent_run.job_id is not None
                    else None
                )
                if parent_job is None:
                    raise ConflictError("revision parent input snapshot is unavailable")
                parent_agent_request = _agent_review_request_from_snapshot(
                    object_store=self.object_store,
                    review_run_id=running.parent_run_id,
                    case_id=parent_job.case_id,
                    snapshot=parent_job.request,
                )
                decisions = [
                    AgentHumanDecision.model_validate(value)
                    for value in running.request.get("human_decisions", [])
                ]
                revision_request = AgentRevisionRequest(
                    parent_result_sha256=running.request["parent_provider_result_sha256"],
                    parent_input_manifest_sha256=running.request["parent_input_manifest_sha256"],
                    parent_result=parent_result,
                    parent_request=parent_agent_request,
                    revised_request=agent_request,
                    decisions=decisions,
                )
                revision_request_wire = revision_request.to_agent_wire()
                if len(canonical_json_bytes(revision_request_wire)) > MAX_AGENT_REQUEST_BODY_BYTES:
                    raise ConflictError("Agent revision request exceeds the 2 MiB transport limit")
                running = self.repository.start_agent_call_once(
                    org_id=org_id,
                    job_id=job_id,
                    expected_lease_id=lease_id,
                    endpoint="revision",
                    request_sha256=canonical_digest(revision_request_wire),
                    audit=self._audit(
                        system,
                        "agent_call.started",
                        case_id=job.case_id,
                        detail={"job_id": job_id, "endpoint": "revision"},
                    ),
                )
                call_started = True
                raw_revision_response = self.agent.revise(revision_request)
                response_received = True
                try:
                    revision_response = AgentRevisionResult.model_validate(raw_revision_response)
                except ValidationError as exc:
                    raise ConflictError(
                        "Agent revision response violates the trusted Backend policy boundary"
                    ) from exc
                if revision_response.parent_result_sha256 != running.request[
                    "parent_provider_result_sha256"
                ] or revision_response.applied_decision_ids != [
                    item.decision_id for item in decisions
                ]:
                    raise ConflictError("Agent revision response is not bound to its approval")
                result = revision_response.child_result.model_dump(mode="json")
            else:
                running = self.repository.start_agent_call_once(
                    org_id=org_id,
                    job_id=job_id,
                    expected_lease_id=lease_id,
                    endpoint="review",
                    request_sha256=agent_request_wire_sha256,
                    audit=self._audit(
                        system,
                        "agent_call.started",
                        case_id=job.case_id,
                        detail={"job_id": job_id, "endpoint": "review"},
                    ),
                )
                call_started = True
                result = self.agent.review(agent_request)
                response_received = True
            run = self._run_from_agent(
                system,
                job=running,
                case=case,
                result=result,
                expected_agent_input_sha256=agent_input_manifest_sha256,
            )
            response_validated = True
            succeeded = running.model_copy(
                update={
                    "status": JobStatus.succeeded,
                    "agent_call_state": AgentCallState.completed,
                    "lease_id": None,
                    "lease_expires_at": None,
                    "updated_at": utc_now(),
                }
            )
            return self.repository.complete_job(
                run=run,
                case_drift_run=self._case_drift_run(run),
                provider_result=result,
                job=succeeded,
                event=StageEvent(
                    event_id=_id("event"),
                    job_id=job_id,
                    sequence=sequence + 1,
                    stage="completed",
                    state="completed",
                    message_code="review.completed",
                    occurred_at=utc_now(),
                    detail={"review_run_id": run.review_run_id},
                ),
                run_audit=self._audit(
                    system,
                    "review.completed",
                    case_id=job.case_id,
                    review_run_id=run.review_run_id,
                    detail={"result_digest": run.result_digest},
                ),
                job_audit=self._audit(
                    system,
                    "job.completed",
                    case_id=job.case_id,
                    review_run_id=run.review_run_id,
                    detail={"job_id": job_id},
                ),
                expected_lease_id=lease_id,
            )
        except AgentPreExecutionRetryableError as exc:
            if not call_started:
                raise
            endpoint = running.agent_call_endpoint
            request_sha256 = running.agent_call_request_sha256
            if endpoint is None or request_sha256 is None:
                raise ConflictError("Agent call binding was lost before safe retry") from exc
            terminal_failure = running.attempt_count >= self.job_max_attempts
            self.repository.mark_agent_pre_execution_retryable(
                org_id=org_id,
                job_id=job_id,
                expected_lease_id=lease_id,
                expected_endpoint=endpoint,
                expected_request_sha256=request_sha256,
                error_code=exc.error_code,
                terminal=terminal_failure,
                event=StageEvent(
                    event_id=_id("event"),
                    job_id=job_id,
                    sequence=sequence + 1,
                    stage="failed",
                    state="failed",
                    message_code=(
                        "review.agent_pre_execution_retry_exhausted"
                        if terminal_failure
                        else "review.agent_pre_execution_retry_scheduled"
                    ),
                    occurred_at=utc_now(),
                    detail={
                        "error_code": exc.error_code,
                        "agent_request_id": exc.request_id,
                    },
                ),
                audit=self._audit(
                    system,
                    (
                        "job.agent_pre_execution_retry_exhausted"
                        if terminal_failure
                        else "job.agent_pre_execution_retry_scheduled"
                    ),
                    case_id=job.case_id,
                    detail={
                        "job_id": job_id,
                        "error_code": exc.error_code,
                        "agent_request_id": exc.request_id,
                        "attempt": running.attempt_count,
                    },
                ),
            )
            if terminal_failure:
                raise AgentPreExecutionRetriesExhaustedError(
                    "Agent remained unready for all bounded pre-execution attempts"
                ) from exc
            raise
        except Exception as exc:
            if call_started:
                if response_received and response_validated:
                    error_code = f"agent_response_persistence_uncertain:{type(exc).__name__}"
                    try:
                        self.repository.mark_agent_response_persistence_uncertain(
                            org_id=org_id,
                            job_id=job_id,
                            expected_lease_id=lease_id,
                            error_code=error_code,
                            event=StageEvent(
                                event_id=_id("event"),
                                job_id=job_id,
                                sequence=sequence + 1,
                                stage="failed",
                                state="failed",
                                message_code="review.response_persistence_uncertain",
                                occurred_at=utc_now(),
                                detail={"error_code": error_code},
                            ),
                            audit=self._audit(
                                system,
                                "job.response_persistence_uncertain",
                                case_id=job.case_id,
                                detail={"job_id": job_id, "error_code": error_code},
                            ),
                        )
                    except Exception as state_error:
                        # The persisted call-start barrier still prevents a second
                        # external request if the database outage also blocks this
                        # more precise terminal transition.
                        _warn_terminal_state_not_persisted(
                            boundary="review_response_persistence_uncertain",
                            error=state_error,
                        )
                    raise
                if response_received or isinstance(exc, AgentResponseRejectedError):
                    error_code = f"agent_response_rejected:{type(exc).__name__}"
                    try:
                        self.repository.mark_agent_response_rejected(
                            org_id=org_id,
                            job_id=job_id,
                            expected_lease_id=lease_id,
                            error_code=error_code,
                            event=StageEvent(
                                event_id=_id("event"),
                                job_id=job_id,
                                sequence=sequence + 1,
                                stage="failed",
                                state="failed",
                                message_code="review.response_received_rejected",
                                occurred_at=utc_now(),
                                detail={"error_code": error_code},
                            ),
                            audit=self._audit(
                                system,
                                "job.response_received_rejected",
                                case_id=job.case_id,
                                detail={"job_id": job_id, "error_code": error_code},
                            ),
                        )
                    except Exception as state_error:
                        _warn_terminal_state_not_persisted(
                            boundary="review_response_rejected",
                            error=state_error,
                        )
                    raise
                self.repository.mark_agent_delivery_uncertain(
                    org_id=org_id,
                    job_id=job_id,
                    expected_lease_id=lease_id,
                    error_code="agent_delivery_uncertain",
                    event=StageEvent(
                        event_id=_id("event"),
                        job_id=job_id,
                        sequence=sequence + 1,
                        stage="failed",
                        state="failed",
                        message_code="review.delivery_uncertain",
                        occurred_at=utc_now(),
                        detail={"error_code": "agent_delivery_uncertain"},
                    ),
                    audit=self._audit(
                        system,
                        "job.delivery_uncertain",
                        case_id=job.case_id,
                        detail={"job_id": job_id, "error_code": "agent_delivery_uncertain"},
                    ),
                )
                raise
            terminal_failure = running.attempt_count >= self.job_max_attempts
            failed = running.model_copy(
                update={
                    "status": JobStatus.failed if terminal_failure else JobStatus.queued,
                    "error_code": type(exc).__name__,
                    "lease_id": None,
                    "lease_expires_at": None,
                    "updated_at": utc_now(),
                }
            )
            self.repository.update_job(
                failed,
                StageEvent(
                    event_id=_id("event"),
                    job_id=job_id,
                    sequence=sequence + 1,
                    stage="failed",
                    state="failed",
                    message_code=(
                        "review.failed" if terminal_failure else "review.retry_scheduled"
                    ),
                    occurred_at=utc_now(),
                    detail={"error_code": type(exc).__name__},
                ),
                self._audit(
                    system,
                    "job.failed",
                    case_id=job.case_id,
                    detail={"job_id": job_id, "error_code": type(exc).__name__},
                ),
                expected_lease_id=lease_id,
            )
            raise

    def _run_from_agent(
        self,
        principal: Principal,
        *,
        job: JobRecord,
        case: CaseRecord,
        result: dict[str, Any],
        expected_agent_input_sha256: str,
    ) -> ReviewRunRecord:
        validate_agent_v3_payload("review_result", result)
        try:
            envelope = AgentReviewResultV3.model_validate(result)
        except ValidationError as exc:
            raise ConflictError(
                "Agent result violates the trusted Backend policy boundary"
            ) from exc
        try:
            expected_review_as_of = date.fromisoformat(str(job.request["review_as_of"]))
            expected_trade_type = str(job.request["trade_type"])
        except (KeyError, TypeError, ValueError) as exc:
            raise ConflictError("immutable Job omitted its review policy binding") from exc
        snapshot_documents = [
            DocumentRecord.model_validate(value) for value in job.request.get("documents", [])
        ]
        supporting_snapshot = [
            SupportingDocumentRecord.model_validate(value)
            for value in job.request.get("supporting_documents", [])
        ]
        primary_snapshot = _validated_primary_review_documents(
            snapshot_documents, supporting_snapshot
        )
        input_document_manifest = _public_input_document_manifest(primary_snapshot)
        stored_manifest = [
            InputDocumentManifestEntry.model_validate(value)
            for value in job.request.get("input_document_manifest", [])
        ]
        input_manifest_digest = _public_input_manifest_digest(
            case_id=case.case_id,
            case_revision=int(job.request["case_revision"]),
            review_as_of=expected_review_as_of,
            trade_type=expected_trade_type,
            documents=input_document_manifest,
        )
        if (
            stored_manifest != input_document_manifest
            or job.request.get("input_manifest_digest") != input_manifest_digest
        ):
            raise ConflictError("immutable Job public input manifest is inconsistent")
        if (
            envelope.runtime_release.result_schema_sha256
            != load_agent_contract_lock().review_result_v3_sha256
        ):
            raise ConflictError("Agent result advertises another provider Schema")
        if (
            envelope.case_id != case.case_id
            or envelope.review_run_id != job.review_run_id
            or envelope.input_manifest_sha256 != expected_agent_input_sha256
        ):
            raise ConflictError("Agent result is not bound to the immutable job input")
        if (
            envelope.review_as_of != expected_review_as_of
            or envelope.trade_type != expected_trade_type
        ):
            raise ConflictError("Agent result changed the immutable review policy input")
        if envelope.knowledge_snapshot_id != envelope.runtime_release.evidence_snapshot_id:
            raise ConflictError("Agent result snapshot differs from its runtime release identity")
        if self.service_pin is not None and not self.service_pin.matches_common_runtime(
            envelope.runtime_release
        ):
            raise ConflictError("Agent runtime differs from the Backend service pin")
        if self.release_pin is not None:
            if not self.release_pin.matches_runtime(envelope.runtime_release):
                raise ConflictError("Agent runtime release differs from the approved Backend pin")
            if envelope.knowledge_snapshot_id != self.release_pin.evidence_snapshot_id:
                raise ConflictError("Agent result snapshot differs from the approved Backend pin")
        elif self.service_pin is not None and not self.service_pin.matches_unreleased_runtime(
            envelope.runtime_release
        ):
            raise ConflictError("Agent runtime must remain unreleased in candidate mode")
        execution = envelope.execution_status
        assessment = envelope.assessment
        disposition = envelope.review_disposition
        assurance = envelope.assurance_level
        eligibility = envelope.completion_eligibility
        required_actions = list(envelope.required_actions)
        outcome_reference = _current_outcome_reference(envelope.fact_ledger)
        backend_gate_reasons: set[str] = set()
        stage_trace = _sanitized_review_stage_trace(envelope.audit_trail)
        model_execution_summary = _bounded_model_execution_summary(
            envelope.model_calls,
            expected_model_id=envelope.runtime_release.model_id,
            expected_prompt_sha256=envelope.runtime_release.extraction_prompt_sha256,
            expected_schema_sha256=envelope.runtime_release.extraction_schema_sha256,
        )
        narrative_call = envelope.narrative_report.get("model_call")
        narrative_execution_summary = _bounded_model_execution_summary(
            [narrative_call] if isinstance(narrative_call, dict) else [],
            expected_model_id=envelope.runtime_release.model_id,
            expected_prompt_sha256=envelope.runtime_release.narrative_prompt_sha256,
            expected_schema_sha256=envelope.runtime_release.narrative_schema_sha256,
            expected_task_id="customs_narrative_report",
        )
        content_gate_reasons = _auto_completion_content_gate_reasons(
            envelope=envelope,
            manifest=input_document_manifest,
            stage_trace=stage_trace,
        )
        if self.release_pin is None and assurance == AssuranceLevel.production:
            assurance = AssuranceLevel.production_candidate
            backend_gate_reasons.add("backend_release_pin_missing")
            required_actions.append(
                RequiredAction(
                    action_code="confirm_backend_release_pin",
                    message="승인된 Backend-Agent exact release pin이 없어 Production 보증을 적용하지 않았습니다.",
                    source_reference_ids=[outcome_reference],
                )
            )
        if backend_gate_reasons:
            assurance = AssuranceLevel.production_candidate
            eligibility = eligibility.model_copy(
                update={
                    "eligible": False,
                    "unmet_reason_codes": sorted(
                        {*eligibility.unmet_reason_codes, *backend_gate_reasons}
                    ),
                }
            )
        supporting_verified = bool(supporting_snapshot) and all(
            document.verification_assertions
            and all(
                assertion.method in {"trusted_connector", "human_approver"}
                and assertion.source_reference_sha256 == document.source_sha256
                and assertion.receipt is not None
                and (
                    (
                        assertion.method == "human_approver"
                        and assertion.authority_release == self.human_decision_policy_release
                        and assertion.authority_id.startswith("backend:principal-sha256:")
                        and assertion.decision_record is not None
                    )
                    or (
                        assertion.method == "trusted_connector"
                        and assertion.authority_id.startswith("connector:")
                        and assertion.authority_release.startswith("connector-policy:")
                    )
                )
                for assertion in document.verification_assertions
            )
            for document in supporting_snapshot
        )
        approved_required_document_codes = (
            sorted({document.document_code for document in supporting_snapshot})
            if supporting_verified
            else []
        )
        profile_binding = (
            AutoCompletionProfileBinding(
                profile_id=self.approved_profile_id,
                approved_document_types=list(APPROVED_AUTO_COMPLETION_PRIMARY_TYPES),
                approved_required_document_codes=approved_required_document_codes,
                approval_digest=self.auto_completion_approval.approval_digest,
            )
            if self.approved_profile_id
            and self.auto_completion_approval is not None
            and self._auto_completion_approval_is_current()
            else AutoCompletionProfileBinding()
        )
        if disposition == ReviewDisposition.auto_review_complete and (
            not self.auto_completion_enabled
            or not self._auto_completion_approval_is_current()
            or self.auto_completion_approval is None
            or self.release_pin is None
            or execution != ExecutionStatus.succeeded
            or assessment != Assessment.no_issue_detected
            or not eligibility.eligible
            or assurance != AssuranceLevel.production
            or required_actions
            or envelope.retrieval_status != "ok"
            or envelope.retrieval_coverage != "complete"
            or envelope.validation_issues
            or envelope.regulatory_findings
            or any(gap.status == "open" for gap in envelope.review_gaps)
            or not supporting_verified
            or not self.approved_profile_id
            or eligibility.profile_id != self.approved_profile_id
            or backend_gate_reasons
            or content_gate_reasons
        ):
            disposition = ReviewDisposition.human_review_required
            assurance = AssuranceLevel.production_candidate
            eligibility = eligibility.model_copy(
                update={
                    "eligible": False,
                    "unmet_reason_codes": sorted(
                        {
                            *eligibility.unmet_reason_codes,
                            "backend_auto_completion_kill_switch_or_release_gate",
                            *content_gate_reasons,
                        }
                    ),
                }
            )
            if not any(
                item.action_code == "complete_backend_human_review" for item in required_actions
            ):
                required_actions.append(
                    RequiredAction(
                        action_code="complete_backend_human_review",
                        message=(
                            "Backend Release·입력·자동완료 Gate를 확인하고 "
                            "담당자 검토를 기록해야 합니다."
                        ),
                        source_reference_ids=[outcome_reference],
                    )
                )
        # Provider output is retained in the private repository only.  Public Case
        # responses receive an explicit allowlist so provider status, model traces,
        # prompts, retrieval requests, and other diagnostic payload cannot leak or
        # contradict the Backend's effective disposition.
        provider_public = envelope.model_dump(mode="json")
        ledger_entries = provider_public.get("fact_ledger", {}).get("entries", [])
        if not isinstance(ledger_entries, list):
            raise ConflictError("Agent FactLedger is malformed")

        def bind_public_references(
            values: list[dict[str, Any]],
            *,
            kind: str,
            prefix: str,
            code_key: str,
        ) -> list[dict[str, Any]]:
            enriched: list[dict[str, Any]] = []
            used: set[str] = set()
            for index, value in enumerate(values):
                reference_id = f"{prefix}_{canonical_digest({'index': index, kind: value})}"
                matches = [
                    entry
                    for entry in ledger_entries
                    if isinstance(entry, dict)
                    and entry.get("kind") == kind
                    and entry.get("reference_id") == reference_id
                    and entry.get("label") == value.get(code_key)
                    and entry.get("value") == value.get("message")
                ]
                if len(matches) != 1 or reference_id in used:
                    raise ConflictError(
                        f"Agent {kind} is not exactly bound to its canonical FactLedger entry"
                    )
                used.add(reference_id)
                enriched.append({**value, "reference_id": reference_id})
            ledger_ids = {
                str(entry.get("reference_id"))
                for entry in ledger_entries
                if isinstance(entry, dict) and entry.get("kind") == kind
            }
            if ledger_ids != used:
                raise ConflictError(f"Agent {kind} FactLedger contains unmatched entries")
            return enriched

        provider_public["validation_issues"] = bind_public_references(
            provider_public["validation_issues"],
            kind="issue",
            prefix="issue",
            code_key="issue_code",
        )
        provider_public["regulatory_findings"] = bind_public_references(
            provider_public["regulatory_findings"],
            kind="finding",
            prefix="finding",
            code_key="finding_code",
        )
        detail = {
            key: provider_public[key]
            for key in {
                "review_as_of",
                "trade_type",
                "jurisdiction",
                "retrieval_status",
                "retrieval_coverage",
                "knowledge_snapshot_id",
                "retrieval_results",
                "validation_issues",
                "regulatory_findings",
                "rag_evidence",
                "document_results",
                "fact_ledger",
                "review_gaps",
                "review_plan",
            }
        }
        detail["review_stage_trace"] = [item.model_dump(mode="json") for item in stage_trace]
        detail["model_execution_summary"] = model_execution_summary.model_dump(mode="json")
        detail["narrative_execution_summary"] = narrative_execution_summary.model_dump(mode="json")
        detail["evidence_provenance"] = _safe_evidence_provenance(envelope)
        effective_narrative = envelope.narrative_report
        if (
            disposition != envelope.review_disposition
            or assurance != envelope.assurance_level
            or eligibility != envelope.completion_eligibility
            or required_actions != list(envelope.required_actions)
        ):
            effective_narrative = _backend_policy_narrative_overlay(
                envelope.narrative_report,
                fact_ledger=envelope.fact_ledger,
                disposition=disposition,
            )
        effective_summary = {
            "provider_result_sha256": envelope.result_sha256,
            "input_manifest_digest": input_manifest_digest,
            "input_document_manifest": [
                item.model_dump(mode="json") for item in input_document_manifest
            ],
            "execution_status": execution.value,
            "assessment": assessment.value,
            "review_disposition": disposition.value,
            "assurance_level": assurance.value,
            "completion_eligibility": eligibility.model_dump(mode="json"),
            "required_actions": [item.model_dump(mode="json") for item in required_actions],
            "review_stage_trace": [item.model_dump(mode="json") for item in stage_trace],
            "auto_completion_profile": profile_binding.model_dump(mode="json"),
            "model_execution_summary": model_execution_summary.model_dump(mode="json"),
            "narrative_execution_summary": narrative_execution_summary.model_dump(mode="json"),
        }
        digest = canonical_digest(effective_summary)
        return ReviewRunRecord(
            review_run_id=job.review_run_id,
            case_id=case.case_id,
            org_id=case.org_id,
            job_id=job.job_id,
            parent_run_id=job.parent_run_id,
            case_revision=int(job.request["case_revision"]),
            review_as_of=expected_review_as_of,
            trade_type=expected_trade_type,
            input_document_manifest=input_document_manifest,
            input_manifest_digest=input_manifest_digest,
            agent_input_manifest_sha256=envelope.input_manifest_sha256,
            provider_result_sha256=envelope.result_sha256,
            result_digest=digest,
            execution_status=execution,
            assessment=assessment,
            review_disposition=disposition,
            assurance_level=assurance,
            completion_eligibility=eligibility,
            required_actions=required_actions,
            narrative_report=effective_narrative,
            action_trace=envelope.action_trace,
            review_stage_trace=stage_trace,
            result_detail=detail,
            release_identity={
                **envelope.runtime_release.model_dump(mode="json"),
                "backend_auto_completion_enabled": self.auto_completion_enabled,
                "backend_auto_completion_approval_digest": (
                    self.auto_completion_approval.approval_digest
                    if self.auto_completion_approval is not None
                    else None
                ),
            },
            auto_completion_profile=profile_binding,
            created_by=principal.subject,
            created_at=utc_now(),
        )

    @staticmethod
    def _case_drift_run(run: ReviewRunRecord) -> ReviewRunRecord:
        """Prepare the fail-closed alternative selected under the Case row lock."""

        reasons = sorted({*run.completion_eligibility.unmet_reason_codes, "case_revision_drift"})
        eligibility = run.completion_eligibility.model_copy(
            update={"eligible": False, "unmet_reason_codes": reasons}
        )
        actions = list(run.required_actions)
        outcome_reference = _current_outcome_reference(run.result_detail["fact_ledger"])
        if not any(item.action_code == "rerun_after_case_revision_change" for item in actions):
            actions.append(
                RequiredAction(
                    action_code="rerun_after_case_revision_change",
                    message="심사 대기 중 Case 입력이 변경되어 현재 revision으로 재심사가 필요합니다.",
                    source_reference_ids=[outcome_reference],
                )
            )
        disposition = (
            ReviewDisposition.human_review_required
            if run.review_disposition == ReviewDisposition.auto_review_complete
            else run.review_disposition
        )
        assurance_level = (
            AssuranceLevel.production_candidate
            if run.assurance_level == AssuranceLevel.production
            else run.assurance_level
        )
        effective_summary = {
            "provider_result_sha256": run.provider_result_sha256,
            "input_manifest_digest": run.input_manifest_digest,
            "input_document_manifest": [
                item.model_dump(mode="json") for item in run.input_document_manifest
            ],
            "execution_status": run.execution_status.value,
            "assessment": run.assessment.value,
            "review_disposition": disposition.value,
            "assurance_level": assurance_level.value,
            "completion_eligibility": eligibility.model_dump(mode="json"),
            "required_actions": [item.model_dump(mode="json") for item in actions],
            "review_stage_trace": [item.model_dump(mode="json") for item in run.review_stage_trace],
            "auto_completion_profile": run.auto_completion_profile.model_dump(mode="json"),
            "model_execution_summary": run.result_detail["model_execution_summary"],
            "narrative_execution_summary": run.result_detail["narrative_execution_summary"],
        }
        return run.model_copy(
            update={
                "review_disposition": disposition,
                "assurance_level": assurance_level,
                "completion_eligibility": eligibility,
                "required_actions": actions,
                "narrative_report": _backend_policy_narrative_overlay(
                    run.narrative_report,
                    fact_ledger=run.result_detail["fact_ledger"],
                    disposition=disposition,
                ),
                "result_digest": canonical_digest(effective_summary),
            }
        )

    def get_job(self, principal: Principal, job_id: str) -> JobRecord:
        job = self.repository.get_job(principal.org_id, job_id)
        if job is None:
            raise NotFoundError("job not found")
        self.get_case(principal, job.case_id)
        return job

    def reconcile_delivery_uncertain(
        self,
        principal: Principal,
        job_id: str,
        request: DeliveryReconciliationCreate,
    ) -> JobRecord:
        job = self.get_job(principal, job_id)
        return self.repository.reconcile_delivery_uncertain(
            org_id=principal.org_id,
            job_id=job.job_id,
            reconciled_by=principal.subject,
            evidence_reference_sha256=request.evidence_reference_sha256,
            audit=self._audit(
                principal,
                "agent_delivery.reconciled",
                case_id=job.case_id,
                detail={
                    "job_id": job.job_id,
                    "resolution": request.resolution,
                    "evidence_reference_sha256": request.evidence_reference_sha256,
                    "reason": request.reason,
                },
            ),
        )

    def get_run(self, principal: Principal, review_run_id: str) -> ReviewRunRecord:
        run = self.repository.get_run(principal.org_id, review_run_id)
        if run is None:
            raise NotFoundError("review run not found")
        self.get_case(principal, run.case_id)
        return run

    def decide(
        self,
        principal: Principal,
        review_run_id: str,
        request: HumanDecisionCreate,
    ) -> HumanDecisionRecord:
        run = self.get_run(principal, review_run_id)
        case = self.get_case(principal, run.case_id)
        if case.revision != run.case_revision:
            raise ConflictError("review decision is stale because the Case changed")
        if request.target_revision_id:
            target = self.repository.get_run(principal.org_id, request.target_revision_id)
            if (
                target is None
                or target.parent_run_id != review_run_id
                or target.case_revision != case.revision
            ):
                raise ValueError("target_revision_id must be a current direct child run")
        record = HumanDecisionRecord(
            decision_id=_id("decision"),
            review_run_id=review_run_id,
            case_id=run.case_id,
            org_id=principal.org_id,
            decision=request.decision,
            reason=request.reason,
            target_revision_id=request.target_revision_id,
            decided_by=principal.subject,
            created_at=utc_now(),
        )
        return self.repository.save_run_decision(
            record,
            self._audit(
                principal,
                "human_decision.recorded",
                case_id=run.case_id,
                review_run_id=review_run_id,
                detail={"decision": request.decision, "decision_id": record.decision_id},
            ),
            expected_case_revision=run.case_revision,
        )

    def answer(
        self,
        principal: Principal,
        review_run_id: str,
        request: GroundedQuestion,
        idempotency_key: str,
    ) -> ChatRecord:
        run = self.get_run(principal, review_run_id)
        scoped_idempotency_key = _scoped_idempotency_key(
            endpoint="case.answer/v1",
            principal=principal,
            case_id=run.case_id,
            supplied_key=idempotency_key,
        )
        idempotency_request_digest = canonical_digest(
            {
                "review_run_id": review_run_id,
                "question": request.model_dump(mode="json"),
            }
        )
        if run.result_digest != request.bound_result_digest:
            raise ConflictError("chat binding digest is stale")
        now = utc_now()
        provider_result = self.repository.get_provider_result(principal.org_id, review_run_id)
        if provider_result is None:
            raise ConflictError("provider result is unavailable for grounded chat")
        expected_answer = expected_answer_projection(
            provider_result=provider_result,
            question=request.question,
            effective_disposition=run.review_disposition.value,
        )
        question_payload = {
            "review_run_id": review_run_id,
            "provider_result_sha256": run.provider_result_sha256,
            "question": request.question,
        }
        question_id = f"question_{canonical_digest(question_payload)}"
        proposed_interaction = ChatInteractionRecord(
            interaction_id=(
                f"interaction_{canonical_digest({'idempotency_key': scoped_idempotency_key})}"
            ),
            review_run_id=review_run_id,
            case_id=run.case_id,
            org_id=principal.org_id,
            idempotency_key=scoped_idempotency_key,
            idempotency_request_digest=idempotency_request_digest,
            agent_request_sha256=canonical_digest(
                {
                    "question_id": question_id,
                    "question": request.question,
                    "provider_result_sha256": run.provider_result_sha256,
                }
            ),
            created_by=principal.subject,
            created_at=now,
            updated_at=now,
        )
        interaction, created = self.repository.reserve_chat_interaction(
            proposed_interaction,
            actor_window_started_at=now - timedelta(minutes=1),
            actor_window_limit=CHAT_INTERACTIONS_PER_ACTOR_PER_MINUTE,
            review_run_limit=CHAT_INTERACTIONS_PER_REVIEW_RUN,
        )
        if not created:
            if interaction.idempotency_request_digest != idempotency_request_digest:
                raise ConflictError("chat idempotency key was reused with another request")
            if interaction.state == ChatInteractionState.completed and interaction.message_id:
                existing = self.repository.get_chat_by_idempotency_key(
                    principal.org_id, scoped_idempotency_key
                )
                if existing is None or existing.message_id != interaction.message_id:
                    raise ConflictError("completed chat interaction references a missing message")
                return existing
            raise ConflictError(f"chat interaction is not retryable: {interaction.state.value}")

        started = self.repository.start_chat_call_once(
            org_id=principal.org_id,
            interaction_id=interaction.interaction_id,
        )
        try:
            answer = (
                AgentAnswerResponse.model_validate(expected_answer.deterministic_wire(question_id))
                if expected_answer.backend_owned_outcome
                else self.agent.answer(
                    provider_result=provider_result,
                    question=request.question,
                    question_id=question_id,
                )
            )
        except AgentResponseRejectedError as exc:
            self.repository.mark_chat_interaction_terminal(
                org_id=principal.org_id,
                interaction_id=started.interaction_id,
                state=ChatInteractionState.response_received_rejected,
                error_code=f"agent_response_rejected:{type(exc).__name__}",
                audit=self._audit(
                    principal,
                    "case_chat.response_received_rejected",
                    case_id=run.case_id,
                    review_run_id=run.review_run_id,
                ),
            )
            raise
        except Exception as exc:
            self.repository.mark_chat_interaction_terminal(
                org_id=principal.org_id,
                interaction_id=started.interaction_id,
                state=ChatInteractionState.delivery_uncertain,
                error_code=f"agent_delivery_uncertain:{type(exc).__name__}",
                audit=self._audit(
                    principal,
                    "case_chat.delivery_uncertain",
                    case_id=run.case_id,
                    review_run_id=run.review_run_id,
                ),
            )
            raise

        try:
            try:
                validate_agent_answer_projection(
                    expected=expected_answer,
                    question_id=question_id,
                    answer=answer,
                    runtime_release=run.release_identity,
                )
            except ValueError as exc:
                raise ConflictError(
                    "Agent answer violates the trusted Backend semantic boundary"
                ) from exc
            ledger_references = {
                (str(item.get("kind")), str(item.get("reference_id")))
                for item in provider_result.get("fact_ledger", {}).get("entries", [])
                if isinstance(item, dict)
            }
            if any(
                (item.get("kind"), item.get("reference_id")) not in ledger_references
                for item in answer.citations
            ):
                raise ConflictError("Agent answer cites evidence outside the bound FactLedger")
            model_call = answer.model_call.model_dump(mode="json") if answer.model_call else None
            model_execution_summary = _bounded_model_execution_summary(
                [model_call] if model_call is not None else [],
                expected_model_id=run.release_identity.get("model_id"),
                expected_prompt_sha256=run.release_identity.get("chat_prompt_sha256"),
                expected_schema_sha256=run.release_identity.get("chat_schema_sha256"),
                expected_task_id="customs_case_answer",
            )
            record = ChatRecord(
                message_id=(
                    f"message_{canonical_digest({'idempotency_key': scoped_idempotency_key})}"
                ),
                review_run_id=review_run_id,
                case_id=run.case_id,
                org_id=principal.org_id,
                question=request.question,
                answer=answer.answer,
                answer_status=answer.status,
                citations=answer.citations,
                generation_mode=answer.generation_mode,
                model_execution_summary=model_execution_summary,
                bound_result_digest=run.result_digest,
                idempotency_key=scoped_idempotency_key,
                idempotency_request_digest=idempotency_request_digest,
                created_by=principal.subject,
                created_at=now,
            )
        except Exception as exc:
            self.repository.mark_chat_interaction_terminal(
                org_id=principal.org_id,
                interaction_id=started.interaction_id,
                state=ChatInteractionState.response_received_rejected,
                error_code=f"agent_response_rejected:{type(exc).__name__}",
                audit=self._audit(
                    principal,
                    "case_chat.response_received_rejected",
                    case_id=run.case_id,
                    review_run_id=run.review_run_id,
                ),
            )
            raise

        try:
            return self.repository.complete_chat_interaction(
                interaction=started,
                message=record,
                audit=self._audit(
                    principal,
                    "case_chat.answered",
                    case_id=run.case_id,
                    review_run_id=run.review_run_id,
                    detail={
                        "message_id": record.message_id,
                        "answer_status": record.answer_status,
                    },
                ),
            )
        except Exception as exc:
            try:
                self.repository.mark_chat_interaction_terminal(
                    org_id=principal.org_id,
                    interaction_id=started.interaction_id,
                    state=ChatInteractionState.response_persistence_uncertain,
                    error_code=f"chat_response_persistence_uncertain:{type(exc).__name__}",
                    audit=self._audit(
                        principal,
                        "case_chat.response_persistence_uncertain",
                        case_id=run.case_id,
                        review_run_id=run.review_run_id,
                    ),
                )
            except Exception as state_error:
                # The durable call-start barrier remains closed even if the database
                # cannot record the more precise terminal state during an outage.
                _warn_terminal_state_not_persisted(
                    boundary="chat_response_persistence_uncertain",
                    error=state_error,
                )
            raise
