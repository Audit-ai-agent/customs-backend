from __future__ import annotations

import hashlib
import logging
import math
import re
import uuid
from dataclasses import dataclass
from datetime import date, timedelta
from typing import Any, Literal, cast
from urllib.parse import parse_qsl, unquote, urlsplit, urlunsplit

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
from customs_backend.experience import (
    REVIEW_TIMED_STAGE_CODES,
    AssistantAnswerV2,
    AgentAnswerStageEventV1,
    AssistantConversationRecord,
    AssistantJobRecord,
    AssistantJobState,
    AssistantMessageCreateV2,
    AssistantMessageRecord,
    AssistantStageCode,
    AssistantStageEvent,
    AgentReviewStageEventV1,
    CaseConversationRequestV2,
    ConversationCreateV2,
    ConversationTurnV2,
    JobExperienceView,
    PublicReviewExperienceV1,
    ReviewEta,
    ReviewExperienceEnvelopeV1,
    ReviewPresentationRecord,
    ReviewStageEta,
    ReviewStageEtaSet,
    build_deterministic_review_experience,
    public_review_experience,
    validate_answer_against_context,
    validate_presentation_against_result,
)
from customs_backend.experience_contract import (
    validate_agent_experience_contract_bundle,
    validate_agent_experience_payload,
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
from customs_backend.uat_evidence_trace import (
    UatEvidenceTraceError,
    UatEvidenceTraceReader,
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
_SENSITIVE_PUBLIC_TEXT = re.compile(
    r"(?i)(?:authorization\s*:|bearer\s+|api[_-]?key\s*[:=]|"
    r"access[_-]?token\s*[:=]|token\s*=|password\s*[:=]|secret\s*[:=])"
)
_SENSITIVE_URI_QUERY_KEYS = frozenset(
    {
        "apikey",
        "accesstoken",
        "authorization",
        "credential",
        "key",
        "password",
        "secret",
        "servicekey",
        "token",
    }
)


@dataclass
class _OrderedAgentStageStream:
    stages: tuple[str, ...]
    skippable_stages: frozenset[str] = frozenset()
    operation_id: str | None = None
    next_stage_index: int = 0
    active_stage: str | None = None
    event_count: int = 0
    terminal: tuple[str, str] | None = None

    def observe(self, event: AgentReviewStageEventV1) -> None:
        self.event_count += 1
        if self.event_count > 100:
            raise ConflictError("Agent stream exceeded the bounded event count")
        if self.operation_id is None:
            self.operation_id = event.operation_id
        elif self.operation_id != event.operation_id:
            raise ConflictError("Agent stream changed operation identity")
        if self.terminal is not None:
            raise ConflictError("Agent stage event followed its terminal")
        if event.stage_code in {"completed", "failed"}:
            if (
                self.active_stage is not None
                or self.next_stage_index != len(self.stages)
                or event.stage_code != "completed"
                or event.state != "completed"
            ):
                raise ConflictError("Agent stream terminal is premature or unsuccessful")
            self.terminal = (event.stage_code, event.state)
            return
        if self.next_stage_index >= len(self.stages):
            raise ConflictError("Agent stream contains a stage after its ordered workflow")
        expected = self.stages[self.next_stage_index]
        if event.stage_code != expected:
            raise ConflictError("Agent stream stage order is inconsistent")
        if self.active_stage is None:
            if event.state == "started":
                self.active_stage = event.stage_code
                return
            if event.state == "completed" or (
                event.state == "skipped" and event.stage_code in self.skippable_stages
            ):
                self.next_stage_index += 1
                return
            raise ConflictError("Agent stream stage did not start or complete safely")
        if self.active_stage != event.stage_code:
            raise ConflictError("Agent stream changed stage before completion")
        if event.state == "completed" or (
            event.state == "skipped" and event.stage_code in self.skippable_stages
        ):
            self.active_stage = None
            self.next_stage_index += 1
            return
        raise ConflictError("Agent stream repeated or failed an active stage")

    def require_success(self) -> None:
        if (
            self.active_stage is not None
            or self.next_stage_index != len(self.stages)
            or self.terminal != ("completed", "completed")
        ):
            raise ConflictError("Agent stream omitted an ordered stage or successful terminal")


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

    def decoded_component(raw: str) -> str:
        decoded = raw
        for _attempt in range(8):
            try:
                expanded = unquote(decoded, errors="strict")
            except UnicodeDecodeError:
                raise ConflictError(
                    "Agent Evidence source URI has invalid percent encoding"
                ) from None
            if expanded == decoded:
                return decoded
            decoded = expanded
        raise ConflictError("Agent Evidence source URI is excessively encoded")

    decoded_path = decoded_component(parsed.path)
    decoded_query = decoded_component(parsed.query)
    decoded_fragment = decoded_component(parsed.fragment)
    decoded_query_pairs = parse_qsl(decoded_query, keep_blank_values=True)
    query_keys = {re.sub(r"[^a-z0-9]", "", key.casefold()) for key, _value in decoded_query_pairs}
    if query_keys & _SENSITIVE_URI_QUERY_KEYS or _SENSITIVE_PUBLIC_TEXT.search(
        decoded_path
        + "?"
        + "&".join(f"{key}={query_value}" for key, query_value in decoded_query_pairs)
        + "#"
        + decoded_fragment
    ):
        # The full provider result is retained as an immutable audit record.  A
        # credential-like URI therefore has to be rejected before persistence;
        # removing it only from the Browser projection would still leak it into
        # the database.
        raise ConflictError("Agent Evidence source URI contains credential material")
    # Query strings and fragments are not needed to identify the legal source and
    # may contain non-secret provider parameters.  Those parameters remain
    # provider-owned and hash-bound, while the Browser projection omits them.
    return urlunsplit((parsed.scheme, parsed.netloc, parsed.path, "", ""))


def _safe_evidence_display_text(
    value: object,
    *,
    field: str,
    maximum: int,
) -> str:
    text = _bounded_provenance_text(value, field=field, maximum=maximum)
    if text is None:
        raise ConflictError(f"Agent {field} is missing")
    normalized = " ".join(text.split())
    if not normalized or _SENSITIVE_PUBLIC_TEXT.search(normalized):
        raise ConflictError(f"Agent {field} contains unsafe public text")
    return normalized


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
            # V2/V3 explicitly permits a partial or failed retrieval response to
            # contain only the request items for which the Provider produced a
            # typed result.  Do not fabricate an ``unknown`` item here: the
            # top-level status/coverage already carries the incomplete outcome,
            # and the browser projection must only describe observed results.
            continue
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
    result_item_ids = set(result_by_item)
    if not result_item_ids.issubset(request_item_ids):
        raise ConflictError("Agent retrieval query/result items differ")
    if envelope.retrieval_status == "ok" and result_item_ids != request_item_ids:
        raise ConflictError("Agent ok retrieval does not cover every query item")
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
                    "title": _safe_evidence_display_text(
                        evidence.get("title"),
                        field="Evidence title",
                        maximum=1000,
                    ),
                    "excerpt": _safe_evidence_display_text(
                        evidence.get("text"),
                        field="Evidence excerpt",
                        maximum=4000,
                    ),
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


def _assistant_event(
    *,
    assistant_job_id: str,
    sequence: int,
    stage_code: AssistantStageCode,
    state: Literal["started", "completed", "skipped", "failed"],
    message_key: str,
    duration_ms: int = 0,
    safe_counts: dict[str, int] | None = None,
) -> AssistantStageEvent:
    occurred_at = utc_now()
    bounded_counts = safe_counts or {}
    payload = {
        "assistant_job_id": assistant_job_id,
        "sequence": sequence,
        "stage_code": stage_code,
        "state": state,
        "occurred_at": occurred_at.isoformat().replace("+00:00", "Z"),
        "duration_ms": duration_ms,
        "safe_counts": bounded_counts,
        "message_key": message_key,
    }
    return AssistantStageEvent(
        event_id=f"assistant_event_{canonical_digest(payload)}",
        assistant_job_id=assistant_job_id,
        sequence=sequence,
        stage_code=stage_code,
        state=state,
        occurred_at=occurred_at,
        duration_ms=duration_ms,
        safe_counts=bounded_counts,
        message_key=message_key,
    )


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
        expected_evidence_runtime_v3_sha256: str | None = None,
        job_lease_seconds: int = 300,
        job_max_attempts: int = 3,
        outbox_lease_seconds: int = 30,
        enhanced_agent_experience: bool = False,
        uat_adaptive_active: bool = False,
        evidence_profile: str = "unknown",
        expected_uat_capability_sha256: str | None = None,
        uat_evidence_trace_reader: UatEvidenceTraceReader | None = None,
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
        if (
            expected_evidence_runtime_v3_sha256 is not None
            and _SHA256.fullmatch(expected_evidence_runtime_v3_sha256) is None
        ):
            raise ValueError("expected Evidence runtime V3 identity must be a SHA-256 digest")
        self.expected_evidence_runtime_v3_sha256 = expected_evidence_runtime_v3_sha256
        self.job_lease_seconds = job_lease_seconds
        self.job_max_attempts = job_max_attempts
        self.outbox_lease_seconds = outbox_lease_seconds
        self.enhanced_agent_experience = enhanced_agent_experience
        self.uat_adaptive_active = uat_adaptive_active
        self.evidence_profile = evidence_profile
        self.uat_evidence_trace_reader = uat_evidence_trace_reader
        if (
            expected_uat_capability_sha256 is not None
            and _SHA256.fullmatch(expected_uat_capability_sha256) is None
        ):
            raise ValueError("expected UAT capability identity must be a SHA-256 digest")
        self.expected_uat_capability_sha256 = expected_uat_capability_sha256
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

    def require_enhanced_experience(self) -> None:
        if not self.enhanced_agent_experience:
            raise ConflictError("enhanced Agent experience is disabled for this runtime")

    def _assert_uat_capability_is_current(self, capability: Any) -> None:
        capability_now = utc_now()
        if (
            capability.expires_at <= capability_now
            or capability.issued_at > capability_now + timedelta(minutes=5)
            or capability.expires_at > capability_now + timedelta(hours=24, minutes=5)
            or capability.adaptive_active != self.uat_adaptive_active
            or (
                self.expected_uat_capability_sha256 is not None
                and capability.capability_sha256 != self.expected_uat_capability_sha256
            )
        ):
            raise ValueError("Agent UAT capability is expired or differs from Backend mode")

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
        *,
        experience_api_version: Literal["3.0"] | None = None,
        evidence_profile: str | None = None,
    ) -> tuple[JobRecord, bool]:
        # Authorization is checked before entering the repository transaction;
        # the repository then locks the Case and snapshots all three aggregate
        # components in the same transaction that creates the immutable Job.
        self.get_case(principal, case_id)
        now = utc_now()
        scoped_idempotency_key = _scoped_idempotency_key(
            endpoint=(
                "review.enqueue/v2" if experience_api_version == "3.0" else "review.enqueue/v1"
            ),
            principal=principal,
            case_id=case_id,
            supplied_key=idempotency_key,
        )
        idempotency_payload: dict[str, Any] = {
            "case_id": case_id,
            "request": request.model_dump(mode="json"),
        }
        if experience_api_version is not None:
            idempotency_payload.update(
                {
                    "experience_api_version": experience_api_version,
                    "evidence_profile": evidence_profile,
                }
            )
        idempotency_request_digest = canonical_digest(idempotency_payload)

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
            if experience_api_version is not None:
                request_payload["experience_api_version"] = experience_api_version
                request_payload["evidence_profile"] = evidence_profile
                request_payload["document_count"] = len(primary_documents)
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
                topic=(
                    "review.experience.requested.v3"
                    if experience_api_version == "3.0"
                    else "review.requested.v1"
                ),
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
            if outbox.topic == "assistant.message.requested.v2":
                self.publisher.publish_assistant(
                    org_id=outbox.org_id,
                    assistant_job_id=outbox.aggregate_id,
                    outbox_id=outbox.outbox_id,
                )
            else:
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
        claimed = self.repository.claim_job(
            org_id=org_id,
            job_id=job_id,
            lease_id=lease_id,
            lease_expires_at=utc_now() + timedelta(seconds=self.job_lease_seconds),
            event=StageEvent(
                event_id=_id("event"),
                job_id=job_id,
                sequence=sequence,
                stage="input_validation",
                state="started",
                message_code="review.input_validation_started",
                occurred_at=utc_now(),
            ),
            audit=self._audit(
                system,
                "review.started",
                case_id=job.case_id,
                detail={"job_id": job_id, "attempt": job.attempt_count + 1},
            ),
        )
        if claimed is None:
            current = self.repository.get_job(org_id, job_id)
            if current is not None and current.status == JobStatus.succeeded:
                completed = self.repository.get_run(org_id, current.review_run_id)
                if completed is not None:
                    return completed
            raise JobExecutionNotRunnableError("job is already claimed by another worker")
        running: JobRecord = claimed
        next_sequence = sequence + 1

        def emit_progress(
            stage: Literal[
                "input_validation",
                "agent_analysis",
                "result_validation",
                "persistence",
            ],
            message_code: str,
            *,
            state: Literal["started", "progress", "completed"] = "started",
        ) -> None:
            nonlocal running, next_sequence
            running = self.repository.update_job(
                running,
                StageEvent(
                    event_id=_id("event"),
                    job_id=job_id,
                    sequence=next_sequence,
                    stage=stage,
                    state=state,
                    message_code=message_code,
                    occurred_at=utc_now(),
                ),
                self._audit(
                    system,
                    "review.progress",
                    case_id=running.case_id,
                    review_run_id=running.review_run_id,
                    detail={"job_id": job_id, "stage": stage},
                ),
                expected_lease_id=lease_id,
            )
            next_sequence += 1

        call_started = False
        response_received = False
        response_validated = False
        experience_envelope: ReviewExperienceEnvelopeV1 | None = None
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
                emit_progress(
                    "input_validation",
                    "review.input_validation_completed",
                    state="completed",
                )
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
                emit_progress("agent_analysis", "agent.analysis_started")
                raw_revision_response = self.agent.revise(revision_request)
                response_received = True
                emit_progress(
                    "agent_analysis",
                    "agent.analysis_completed",
                    state="completed",
                )
                emit_progress("result_validation", "agent.response_validation_started")
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
                if running.request.get("experience_api_version") == "3.0":
                    if running.parent_run_id is None:
                        raise ConflictError("enhanced revision has no parent Run")
                    parent_presentation = self.repository.get_review_presentation(
                        org_id,
                        running.parent_run_id,
                    )
                    if parent_presentation is not None:
                        capability = parent_presentation.envelope.capability_attestation
                        try:
                            self._assert_uat_capability_is_current(capability)
                        except ValueError as exc:
                            raise ConflictError(
                                "parent UAT capability is not valid for the child revision"
                            ) from exc
                        experience_envelope = build_deterministic_review_experience(
                            result=AgentReviewResultV3.model_validate(result),
                            capability_attestation=capability,
                            reason_code="approved_child_revision_deterministic_fallback",
                        )
            else:
                emit_progress(
                    "input_validation",
                    "review.input_validation_completed",
                    state="completed",
                )
                running = self.repository.start_agent_call_once(
                    org_id=org_id,
                    job_id=job_id,
                    expected_lease_id=lease_id,
                    endpoint=(
                        "review_experience"
                        if running.request.get("experience_api_version") == "3.0"
                        else "review"
                    ),
                    request_sha256=agent_request_wire_sha256,
                    audit=self._audit(
                        system,
                        "agent_call.started",
                        case_id=job.case_id,
                        detail={
                            "job_id": job_id,
                            "endpoint": (
                                "review_experience"
                                if running.request.get("experience_api_version") == "3.0"
                                else "review"
                            ),
                        },
                    ),
                )
                call_started = True
                if running.request.get("experience_api_version") == "3.0":
                    self.require_enhanced_experience()
                    expected_agent_event_sequence = 0
                    review_stream = _OrderedAgentStageStream(
                        stages=(
                            "request_validation",
                            "document_analysis",
                            "normalization",
                            "document_pairing",
                            "cross_validation",
                            "adaptive_investigation",
                            "evidence_retrieval",
                            "compliance_review",
                            "report_composition",
                            "grounding_verification",
                            "result_validation",
                        ),
                        skippable_stages=frozenset({"adaptive_investigation"}),
                    )

                    def persist_agent_stage(raw_event: dict[str, Any]) -> None:
                        nonlocal running
                        nonlocal next_sequence
                        nonlocal expected_agent_event_sequence
                        try:
                            validate_agent_experience_payload(
                                "review-stage-event-v1",
                                raw_event,
                            )
                            agent_event = AgentReviewStageEventV1.model_validate(raw_event)
                        except ValidationError as exc:
                            raise ConflictError(
                                "Agent stage event violated the V3 streaming contract"
                            ) from exc
                        if agent_event.sequence != expected_agent_event_sequence:
                            raise ConflictError("Agent stage event sequence is discontinuous")
                        if agent_event.stage_code in {
                            "conversation_binding",
                            "answer_planning",
                            "answer_composition",
                        }:
                            raise ConflictError(
                                "Agent review stream contained an answer-only stage"
                            )
                        review_stream.observe(agent_event)
                        # The Agent terminal proves stream completeness, but the
                        # Backend publishes its own single terminal only after the
                        # verified result and presentation are atomically persisted.
                        if agent_event.stage_code not in {"completed", "failed"}:
                            running = self.repository.update_job(
                                running,
                                StageEvent(
                                    event_id=_id("event"),
                                    job_id=job_id,
                                    sequence=next_sequence,
                                    stage=agent_event.stage_code,
                                    state=agent_event.state,
                                    message_code=agent_event.message_key,
                                    occurred_at=agent_event.occurred_at,
                                    detail={
                                        "operation_id": agent_event.operation_id,
                                        "agent_sequence": agent_event.sequence,
                                        "duration_ms": agent_event.duration_ms,
                                        "safe_counts": agent_event.safe_counts,
                                        "agent_event_sha256": agent_event.event_sha256,
                                    },
                                ),
                                self._audit(
                                    system,
                                    "review.agent_stage",
                                    case_id=running.case_id,
                                    review_run_id=running.review_run_id,
                                    detail={
                                        "job_id": job_id,
                                        "stage": agent_event.stage_code,
                                        "state": agent_event.state,
                                    },
                                ),
                                expected_lease_id=lease_id,
                            )
                            next_sequence += 1
                        expected_agent_event_sequence += 1

                    raw_experience = self.agent.review_experience_stream(
                        agent_request,
                        on_event=persist_agent_stage,
                    )
                    response_received = True
                    review_stream.require_success()
                    try:
                        validate_agent_experience_payload(
                            "review-experience-envelope-v1",
                            raw_experience,
                        )
                        experience_envelope = ReviewExperienceEnvelopeV1.model_validate(
                            raw_experience
                        )
                        validate_presentation_against_result(experience_envelope)
                        self._assert_uat_capability_is_current(
                            experience_envelope.capability_attestation
                        )
                    except (ValidationError, ValueError) as exc:
                        raise ConflictError(
                            "Agent experience violated the trusted Backend boundary"
                        ) from exc
                    result = experience_envelope.canonical_result.model_dump(mode="json")
                else:
                    emit_progress("agent_analysis", "agent.analysis_started")
                    result = self.agent.review(agent_request)
                    response_received = True
                    emit_progress(
                        "agent_analysis",
                        "agent.analysis_completed",
                        state="completed",
                    )
                if running.request.get("experience_api_version") != "3.0":
                    emit_progress("result_validation", "agent.response_validation_started")
            run = self._run_from_agent(
                system,
                job=running,
                case=case,
                result=result,
                expected_agent_input_sha256=agent_input_manifest_sha256,
            )
            response_validated = True
            if running.request.get("experience_api_version") != "3.0":
                emit_progress(
                    "result_validation",
                    "agent.response_validation_completed",
                    state="completed",
                )
            emit_progress("persistence", "review.persistence_started")
            succeeded = running.model_copy(
                update={
                    "status": JobStatus.succeeded,
                    "agent_call_state": AgentCallState.completed,
                    "lease_id": None,
                    "lease_expires_at": None,
                    "updated_at": utc_now(),
                }
            )
            experience_matches_effective_run = (
                experience_envelope is not None
                and self._experience_matches_effective_run(experience_envelope, run)
            )
            presentation = (
                ReviewPresentationRecord(
                    presentation_id=(
                        "presentation_"
                        + canonical_digest({"envelope_sha256": experience_envelope.envelope_sha256})
                    ),
                    review_run_id=run.review_run_id,
                    case_id=run.case_id,
                    org_id=run.org_id,
                    bound_provider_result_sha256=run.provider_result_sha256,
                    bound_effective_result_digest=run.result_digest,
                    envelope=experience_envelope,
                    created_at=utc_now(),
                )
                if experience_matches_effective_run and experience_envelope is not None
                else None
            )
            return self.repository.complete_job(
                run=run,
                case_drift_run=self._case_drift_run(run),
                provider_result=result,
                job=succeeded,
                event=StageEvent(
                    event_id=_id("event"),
                    job_id=job_id,
                    sequence=next_sequence,
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
                presentation=presentation,
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
                    sequence=next_sequence,
                    stage="failed" if terminal_failure else "agent_analysis",
                    state="failed" if terminal_failure else "progress",
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
                                sequence=next_sequence,
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
                                sequence=next_sequence,
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
                        sequence=next_sequence,
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
                    sequence=next_sequence,
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
        enhanced_v3_result = job.request.get("experience_api_version") == "3.0"
        if enhanced_v3_result:
            validate_agent_experience_payload("review-result-v3", result)
        else:
            validate_agent_v3_payload("review_result", result)
        try:
            envelope = AgentReviewResultV3.model_validate(result)
        except ValidationError as exc:
            raise ConflictError(
                "Agent result violates the trusted Backend policy boundary"
            ) from exc
        if (
            self.expected_evidence_runtime_v3_sha256 is not None
            and envelope.runtime_release.evidence_runtime_v3_sha256
            != self.expected_evidence_runtime_v3_sha256
        ):
            raise ConflictError(
                "Agent result Evidence runtime identity differs from the Backend pin"
            )
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
        expected_result_schema_sha256 = (
            validate_agent_experience_contract_bundle().lock.provider_review_result_v3.file_sha256
            if enhanced_v3_result
            else load_agent_contract_lock().review_result_v3_sha256
        )
        if envelope.runtime_release.result_schema_sha256 != expected_result_schema_sha256:
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
        evidence_provenance = _safe_evidence_provenance(envelope)
        observed_retrieval_methods = sorted(
            {
                method
                for evidence in evidence_provenance["ranked_evidence"]
                for method in evidence["retrieval_methods"]
            },
            key={"exact_hs": 0, "dense": 1, "bm25": 2, "graph": 3}.__getitem__,
        )
        evidence_profile = str(job.request.get("evidence_profile", self.evidence_profile))
        evidence_request_id = evidence_provenance.get("request_id")
        attempted_retrieval_methods: list[Literal["exact_hs", "dense", "bm25", "graph"]] = []
        successful_retrieval_methods: list[Literal["exact_hs", "dense", "bm25", "graph"]] = []
        retrieval_execution_attested = False
        evidence_data_source: Literal[
            "UNATTESTED",
            "ACTUAL_POSTGRESQL_QDRANT_NEO4J_READ_ONLY",
        ] = "UNATTESTED"
        if self.uat_evidence_trace_reader is not None:
            if (
                evidence_profile != "preserved-3law-v3-scenario-eval"
                or not isinstance(evidence_request_id, str)
                or not isinstance(envelope.knowledge_snapshot_id, str)
            ):
                raise ConflictError("actual UAT Evidence execution binding is incomplete")
            try:
                trace = self.uat_evidence_trace_reader.projection(
                    request_id=evidence_request_id,
                    knowledge_snapshot_id=envelope.knowledge_snapshot_id,
                )
            except UatEvidenceTraceError as exc:
                raise ConflictError(
                    "actual UAT Evidence execution trace is unavailable or differs"
                ) from exc
            method_by_stage: dict[
                str,
                Literal["exact_hs", "dense", "bm25", "graph"],
            ] = {
                "structured": "exact_hs",
                "dense": "dense",
                "bm25": "bm25",
                "graph": "graph",
            }
            for stage_name, method in method_by_stage.items():
                stage = getattr(trace.retrieval, stage_name)
                if stage.attempted:
                    attempted_retrieval_methods.append(method)
                if stage.succeeded:
                    successful_retrieval_methods.append(method)
            retrieval_execution_attested = True
            evidence_data_source = "ACTUAL_POSTGRESQL_QDRANT_NEO4J_READ_ONLY"
        detail = {
            key: provider_public[key]
            for key in {
                "review_as_of",
                "trade_type",
                "jurisdiction",
                "retrieval_status",
                "retrieval_coverage",
                "knowledge_snapshot_id",
                "validation_issues",
                "regulatory_findings",
                "document_results",
                "fact_ledger",
                "review_gaps",
                "review_plan",
            }
        }
        detail["review_stage_trace"] = [item.model_dump(mode="json") for item in stage_trace]
        detail["model_execution_summary"] = model_execution_summary.model_dump(mode="json")
        detail["narrative_execution_summary"] = narrative_execution_summary.model_dump(mode="json")
        detail["evidence_provenance"] = evidence_provenance
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
            "evidence_profile": evidence_profile,
            "evidence_data_source": evidence_data_source,
            "evidence_request_id": evidence_request_id,
            "observed_retrieval_methods": observed_retrieval_methods,
            "attempted_retrieval_methods": attempted_retrieval_methods,
            "successful_retrieval_methods": successful_retrieval_methods,
            "retrieval_execution_attested": retrieval_execution_attested,
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
            evidence_profile=evidence_profile,
            evidence_data_source=evidence_data_source,
            evidence_request_id=evidence_request_id,
            observed_retrieval_methods=observed_retrieval_methods,
            attempted_retrieval_methods=attempted_retrieval_methods,
            successful_retrieval_methods=successful_retrieval_methods,
            retrieval_execution_attested=retrieval_execution_attested,
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
            "evidence_profile": run.evidence_profile,
            "evidence_data_source": run.evidence_data_source,
            "evidence_request_id": run.evidence_request_id,
            "observed_retrieval_methods": run.observed_retrieval_methods,
            "attempted_retrieval_methods": run.attempted_retrieval_methods,
            "successful_retrieval_methods": run.successful_retrieval_methods,
            "retrieval_execution_attested": run.retrieval_execution_attested,
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

    def get_job_experience(self, principal: Principal, job_id: str) -> JobExperienceView:
        self.require_enhanced_experience()
        job = self.get_job(principal, job_id)
        document_count = int(job.request.get("document_count", 0))
        evidence_profile = str(job.request.get("evidence_profile", self.evidence_profile))
        durations = sorted(
            self.repository.recent_review_durations_ms(
                org_id=principal.org_id,
                evidence_profile=evidence_profile,
                document_count=document_count,
                limit=30,
            )
        )
        if len(durations) >= 5:

            def percentile(fraction: float) -> int:
                index = min(len(durations) - 1, int((len(durations) - 1) * fraction + 0.5))
                return max(0, durations[index] // 1000)

            p50 = percentile(0.5)
            p90 = percentile(0.9)
            eta = ReviewEta(
                source="observed",
                sample_size=len(durations),
                p50_seconds=p50,
                p90_seconds=p90,
                range_min_seconds=p50,
                range_max_seconds=max(p50, p90),
            )
        else:
            eta = ReviewEta(
                source="validated_default",
                sample_size=len(durations),
                range_min_seconds=4 * 60,
                range_max_seconds=7 * 60,
            )
        stage_samples = self.repository.recent_review_stage_durations_ms(
            org_id=principal.org_id,
            evidence_profile=evidence_profile,
            document_count=document_count,
            limit=30,
        )
        stage_estimates: list[ReviewStageEta] = []
        for stage_code in REVIEW_TIMED_STAGE_CODES:
            stage_durations = sorted(
                sample[stage_code] for sample in stage_samples if stage_code in sample
            )
            if len(stage_durations) >= 5:

                def stage_percentile(fraction: float) -> int:
                    index = min(
                        len(stage_durations) - 1,
                        int((len(stage_durations) - 1) * fraction + 0.5),
                    )
                    return max(0, stage_durations[index] // 1000)

                stage_estimates.append(
                    ReviewStageEta(
                        stage_code=stage_code,
                        source="observed",
                        sample_count=len(stage_durations),
                        p50_seconds=stage_percentile(0.5),
                        p90_seconds=stage_percentile(0.9),
                    )
                )
            else:
                stage_estimates.append(
                    ReviewStageEta(
                        stage_code=stage_code,
                        source="insufficient_samples",
                        sample_count=len(stage_durations),
                    )
                )
        elapsed = max(0, int((utc_now() - job.created_at).total_seconds()))
        return JobExperienceView(
            job_id=job.job_id,
            case_id=job.case_id,
            kind=job.kind,
            parent_run_id=job.parent_run_id,
            status=job.status,
            review_run_id=job.review_run_id,
            error_code=job.error_code,
            attempt_count=job.attempt_count,
            agent_call_state=job.agent_call_state.value,
            created_by=job.created_by,
            created_at=job.created_at,
            updated_at=job.updated_at,
            elapsed_seconds=elapsed,
            eta=eta,
            stage_eta=ReviewStageEtaSet(stages=stage_estimates),
        )

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

    def get_review_experience(
        self, principal: Principal, review_run_id: str
    ) -> ReviewExperienceEnvelopeV1:
        self.require_enhanced_experience()
        run = self.get_run(principal, review_run_id)
        presentation = self.repository.get_review_presentation(principal.org_id, review_run_id)
        if presentation is None:
            raise NotFoundError("verified review presentation is unavailable")
        if (
            presentation.bound_provider_result_sha256 != run.provider_result_sha256
            or presentation.bound_effective_result_digest != run.result_digest
            or not self._experience_matches_effective_run(presentation.envelope, run)
        ):
            raise ConflictError("review presentation binding is stale")
        return presentation.envelope

    def get_public_review_experience(
        self, principal: Principal, review_run_id: str
    ) -> PublicReviewExperienceV1:
        envelope = self.get_review_experience(principal, review_run_id)
        run = self.get_run(principal, review_run_id)
        return public_review_experience(
            envelope,
            effective_result_digest=run.result_digest,
        )

    @staticmethod
    def _experience_matches_effective_run(
        envelope: ReviewExperienceEnvelopeV1,
        run: ReviewRunRecord,
    ) -> bool:
        canonical = envelope.canonical_result
        return (
            canonical.result_sha256 == run.provider_result_sha256
            and canonical.execution_status == run.execution_status
            and canonical.assessment == run.assessment
            and canonical.review_disposition == run.review_disposition
            and canonical.assurance_level == run.assurance_level
            and canonical.completion_eligibility == run.completion_eligibility
            and canonical.required_actions == run.required_actions
            and canonical.narrative_report == run.narrative_report
        )

    def create_assistant_conversation(
        self,
        principal: Principal,
        review_run_id: str,
        request: ConversationCreateV2,
    ) -> AssistantConversationRecord:
        self.require_enhanced_experience()
        run = self.get_run(principal, review_run_id)
        current_case = self.get_case(principal, run.case_id)
        if current_case.revision != run.case_revision:
            raise ConflictError(
                "cannot start an assistant conversation from a stale review revision"
            )
        if request.bound_result_sha256 != run.provider_result_sha256:
            raise ConflictError("conversation result binding is stale")
        presentation = self.repository.get_review_presentation(principal.org_id, review_run_id)
        if (
            presentation is None
            or presentation.bound_effective_result_digest != run.result_digest
            or not self._experience_matches_effective_run(presentation.envelope, run)
        ):
            raise ConflictError(
                "assistant is unavailable because no presentation matches the effective review"
            )
        now = utc_now()
        record = AssistantConversationRecord(
            conversation_id=(
                "conversation_"
                + canonical_digest(
                    {
                        "org_id": principal.org_id,
                        "review_run_id": review_run_id,
                        "created_by": principal.subject,
                        "nonce": uuid.uuid4().hex,
                    }
                )
            ),
            review_run_id=review_run_id,
            case_id=run.case_id,
            org_id=principal.org_id,
            bound_result_sha256=run.provider_result_sha256,
            bound_effective_result_digest=run.result_digest,
            created_by=principal.subject,
            created_at=now,
            updated_at=now,
        )
        return self.repository.create_assistant_conversation(
            record,
            self._audit(
                principal,
                "assistant_conversation.created",
                case_id=run.case_id,
                review_run_id=review_run_id,
                detail={"conversation_id": record.conversation_id},
            ),
        )

    def list_assistant_conversations(
        self, principal: Principal, review_run_id: str
    ) -> list[AssistantConversationRecord]:
        self.require_enhanced_experience()
        self.get_run(principal, review_run_id)
        return self.repository.list_assistant_conversations(principal.org_id, review_run_id)

    def get_assistant_conversation(
        self, principal: Principal, conversation_id: str
    ) -> AssistantConversationRecord:
        self.require_enhanced_experience()
        conversation = self.repository.get_assistant_conversation(principal.org_id, conversation_id)
        if conversation is None:
            raise NotFoundError("assistant conversation not found")
        run = self.get_run(principal, conversation.review_run_id)
        if (
            run.provider_result_sha256 != conversation.bound_result_sha256
            or run.result_digest != conversation.bound_effective_result_digest
        ):
            raise ConflictError("assistant conversation result binding is stale")
        return conversation

    @staticmethod
    def _conversation_turns(
        messages: list[AssistantMessageRecord],
    ) -> list[ConversationTurnV2]:
        return [
            ConversationTurnV2(
                message_id=item.message_id,
                parent_message_id=item.parent_message_id,
                role=item.role,
                content=item.content,
                created_at=item.created_at,
                answer_sha256=(
                    item.structured_answer.answer_sha256
                    if item.structured_answer is not None
                    else None
                ),
            )
            for item in messages[-8:]
        ]

    def enqueue_assistant_message(
        self,
        principal: Principal,
        conversation_id: str,
        request: AssistantMessageCreateV2,
        idempotency_key: str,
    ) -> tuple[AssistantJobRecord, AssistantMessageRecord, bool]:
        conversation = self.get_assistant_conversation(principal, conversation_id)
        run = self.get_run(principal, conversation.review_run_id)
        current_case = self.get_case(principal, run.case_id)
        if current_case.revision != run.case_revision:
            raise ConflictError("assistant conversation is stale because the Case changed")
        if request.bound_result_sha256 != conversation.bound_result_sha256:
            raise ConflictError("assistant message result binding is stale")
        scoped_idempotency_key = _scoped_idempotency_key(
            endpoint="case.answer/v2",
            principal=principal,
            case_id=conversation.case_id,
            supplied_key=idempotency_key,
        )
        existing_job = self.repository.get_assistant_job_by_idempotency_key(
            principal.org_id,
            scoped_idempotency_key,
        )
        if existing_job is not None:
            expected_digest = canonical_digest(
                {
                    "conversation_id": conversation_id,
                    "message": request.model_dump(mode="json"),
                    "conversation_context_sha256": existing_job.request.get(
                        "conversation_context_sha256"
                    ),
                }
            )
            if (
                existing_job.conversation_id != conversation_id
                or existing_job.created_by != principal.subject
                or existing_job.idempotency_request_digest != expected_digest
            ):
                raise ConflictError("assistant idempotency key was reused")
            stored_message = next(
                (
                    item
                    for item in self.repository.list_assistant_messages(
                        principal.org_id,
                        conversation_id,
                    )
                    if item.message_id == existing_job.user_message_id
                ),
                None,
            )
            if stored_message is None:
                raise ConflictError("assistant idempotency replay lost its user message")
            return existing_job, stored_message, False
        messages = self.repository.list_assistant_messages(principal.org_id, conversation_id)
        latest_message_id = messages[-1].message_id if messages else None
        if request.parent_message_id != latest_message_id:
            raise ConflictError("assistant message parent is stale")
        provider_result = self.repository.get_provider_result(principal.org_id, run.review_run_id)
        if provider_result is None:
            raise ConflictError("provider result is unavailable for grounded assistant")
        try:
            canonical_result = AgentReviewResultV3.model_validate(provider_result)
        except ValidationError as exc:
            raise ConflictError("stored provider result is no longer contract-valid") from exc
        recent_turns = self._conversation_turns(messages)
        context_sha256 = canonical_digest([item.model_dump(mode="json") for item in recent_turns])
        agent_request = CaseConversationRequestV2(
            conversation_id=conversation_id,
            parent_message_id=request.parent_message_id,
            question=request.question,
            bound_result_sha256=conversation.bound_result_sha256,
            conversation_context_sha256=context_sha256,
            recent_turns=recent_turns,
            review_result=canonical_result,
        )
        agent_request_wire = agent_request.model_dump(mode="json")
        if len(canonical_json_bytes(agent_request_wire)) > MAX_AGENT_REQUEST_BODY_BYTES:
            raise ConflictError("assistant request exceeds the 2 MiB transport limit")
        idempotency_request_digest = canonical_digest(
            {
                "conversation_id": conversation_id,
                "message": request.model_dump(mode="json"),
                "conversation_context_sha256": context_sha256,
            }
        )
        now = utc_now()
        assistant_job_id = "assistant_job_" + canonical_digest(
            {"idempotency_key": scoped_idempotency_key}
        )
        user_message = AssistantMessageRecord(
            message_id=(
                "assistant_message_"
                + canonical_digest(
                    {
                        "idempotency_key": scoped_idempotency_key,
                        "role": "user",
                    }
                )
            ),
            conversation_id=conversation_id,
            review_run_id=run.review_run_id,
            case_id=run.case_id,
            org_id=principal.org_id,
            parent_message_id=request.parent_message_id,
            role="user",
            content=request.question,
            bound_result_sha256=conversation.bound_result_sha256,
            created_by=principal.subject,
            created_at=now,
        )
        queued_event = _assistant_event(
            assistant_job_id=assistant_job_id,
            sequence=1,
            stage_code="queued",
            state="completed",
            message_key="assistant.queued",
        )
        job = AssistantJobRecord(
            assistant_job_id=assistant_job_id,
            conversation_id=conversation_id,
            review_run_id=run.review_run_id,
            case_id=run.case_id,
            org_id=principal.org_id,
            user_message_id=user_message.message_id,
            idempotency_key=scoped_idempotency_key,
            idempotency_request_digest=idempotency_request_digest,
            agent_request_sha256=canonical_digest(agent_request_wire),
            request=agent_request_wire,
            events=[queued_event],
            created_by=principal.subject,
            created_at=now,
            updated_at=now,
        )
        outbox = OutboxRecord(
            outbox_id=_id("outbox"),
            org_id=principal.org_id,
            topic="assistant.message.requested.v2",
            aggregate_id=assistant_job_id,
            payload={
                "assistant_job_id": assistant_job_id,
                "org_id": principal.org_id,
            },
            created_at=now,
        )
        return self.repository.enqueue_assistant_message(
            conversation=conversation,
            user_message=user_message,
            job=job,
            outbox=outbox,
            audit=self._audit(
                principal,
                "assistant_message.enqueued",
                case_id=run.case_id,
                review_run_id=run.review_run_id,
                detail={
                    "conversation_id": conversation_id,
                    "assistant_job_id": assistant_job_id,
                    "user_message_id": user_message.message_id,
                },
            ),
            expected_case_revision=run.case_revision,
            actor_window_started_at=now - timedelta(minutes=1),
            actor_window_limit=CHAT_INTERACTIONS_PER_ACTOR_PER_MINUTE,
            review_run_limit=CHAT_INTERACTIONS_PER_REVIEW_RUN,
        )

    def _reconcile_expired_assistant_job(
        self,
        job: AssistantJobRecord,
    ) -> AssistantJobRecord:
        if (
            job.state != AssistantJobState.running
            or job.lease_id is None
            or job.lease_expires_at is None
            or job.lease_expires_at > utc_now()
        ):
            return job
        system = Principal(
            subject="system:assistant-reconciler",
            org_id=job.org_id,
            roles=frozenset(),
        )
        try:
            return self.repository.mark_assistant_job_terminal(
                org_id=job.org_id,
                assistant_job_id=job.assistant_job_id,
                state=AssistantJobState.delivery_uncertain,
                error_code="delivery_uncertain:expired_call_once_lease",
                event=_assistant_event(
                    assistant_job_id=job.assistant_job_id,
                    sequence=len(job.events) + 1,
                    stage_code="failed",
                    state="failed",
                    message_key="assistant.delivery_uncertain",
                ),
                audit=self._audit(
                    system,
                    "assistant_message.delivery_uncertain",
                    case_id=job.case_id,
                    review_run_id=job.review_run_id,
                    detail={
                        "assistant_job_id": job.assistant_job_id,
                        "reason": "expired_call_once_lease",
                    },
                ),
                expected_lease_id=job.lease_id,
            )
        except ConflictError:
            current = self.repository.get_assistant_job(job.org_id, job.assistant_job_id)
            if current is None:
                raise NotFoundError("assistant job not found") from None
            return current

    def get_assistant_job(self, principal: Principal, assistant_job_id: str) -> AssistantJobRecord:
        self.require_enhanced_experience()
        job = self.repository.get_assistant_job(principal.org_id, assistant_job_id)
        if job is None:
            raise NotFoundError("assistant job not found")
        job = self._reconcile_expired_assistant_job(job)
        self.get_run(principal, job.review_run_id)
        return job

    def list_assistant_jobs(
        self, principal: Principal, conversation_id: str
    ) -> list[AssistantJobRecord]:
        self.require_enhanced_experience()
        conversation = self.get_assistant_conversation(principal, conversation_id)
        return [
            self._reconcile_expired_assistant_job(job)
            for job in self.repository.list_assistant_jobs(
                principal.org_id,
                conversation.conversation_id,
            )
        ]

    def list_assistant_messages(
        self, principal: Principal, conversation_id: str
    ) -> list[AssistantMessageRecord]:
        conversation = self.get_assistant_conversation(principal, conversation_id)
        return self.repository.list_assistant_messages(
            principal.org_id, conversation.conversation_id
        )

    def execute_assistant_job(self, *, org_id: str, assistant_job_id: str) -> AssistantJobRecord:
        self.require_enhanced_experience()
        job = self.repository.get_assistant_job(org_id, assistant_job_id)
        if job is None:
            raise NotFoundError("assistant job not found")
        if job.state == AssistantJobState.succeeded:
            return job
        job = self._reconcile_expired_assistant_job(job)
        if job.state != AssistantJobState.queued:
            raise JobExecutionNotRunnableError("assistant job is not runnable")
        system = Principal(
            subject="system:assistant-worker",
            org_id=org_id,
            roles=frozenset(),
        )
        lease_id = _id("assistantlease")
        started = self.repository.claim_assistant_job(
            org_id=org_id,
            assistant_job_id=assistant_job_id,
            lease_id=lease_id,
            lease_expires_at=utc_now() + timedelta(seconds=self.job_lease_seconds),
            event=_assistant_event(
                assistant_job_id=assistant_job_id,
                sequence=len(job.events) + 1,
                stage_code="request_validation",
                state="started",
                message_key="assistant.request_validation_started",
            ),
        )
        sequence = len(started.events) + 1
        external_call_started = False
        response_received = False
        response_validated = False
        try:
            request = CaseConversationRequestV2.model_validate(started.request)
            validate_agent_experience_payload(
                "case-conversation-request-v2",
                request.model_dump(mode="json"),
            )
            if canonical_digest(request.model_dump(mode="json")) != started.agent_request_sha256:
                raise ConflictError("assistant immutable request hash is inconsistent")
            conversation = self.repository.get_assistant_conversation(
                org_id, started.conversation_id
            )
            run = self.repository.get_run(org_id, started.review_run_id)
            case = self.repository.get_case(org_id, started.case_id)
            if conversation is None or run is None or case is None:
                raise ConflictError("assistant job lineage is unavailable")
            if (
                conversation.bound_result_sha256 != request.bound_result_sha256
                or run.provider_result_sha256 != request.bound_result_sha256
                or run.result_digest != conversation.bound_effective_result_digest
                or case.revision != run.case_revision
                or request.review_result.result_sha256 != request.bound_result_sha256
            ):
                raise ConflictError("assistant request result binding is stale")
            presentation = self.repository.get_review_presentation(
                org_id,
                started.review_run_id,
            )
            if (
                presentation is None
                or presentation.bound_provider_result_sha256 != request.bound_result_sha256
                or presentation.bound_effective_result_digest != run.result_digest
            ):
                raise ConflictError("assistant verified presentation binding is unavailable")
            self._assert_uat_capability_is_current(presentation.envelope.capability_attestation)
            expected_agent_sequence = 0
            answer_stream = _OrderedAgentStageStream(
                stages=(
                    "request_validation",
                    "conversation_binding",
                    "answer_planning",
                    "answer_composition",
                    "grounding_verification",
                    "result_validation",
                )
            )
            pending_result_validation_event: AgentAnswerStageEventV1 | None = None

            def persist_answer_stage(raw_event: dict[str, Any]) -> None:
                nonlocal sequence
                nonlocal expected_agent_sequence
                nonlocal pending_result_validation_event
                try:
                    validate_agent_experience_payload(
                        "review-stage-event-v1",
                        raw_event,
                    )
                    event = AgentAnswerStageEventV1.model_validate(raw_event)
                except ValidationError as exc:
                    raise ConflictError(
                        "Agent answer stage violated the streaming contract"
                    ) from exc
                if event.sequence != expected_agent_sequence:
                    raise ConflictError("Agent answer stage sequence is discontinuous")
                if event.stage_code not in {
                    "request_validation",
                    "conversation_binding",
                    "answer_planning",
                    "answer_composition",
                    "grounding_verification",
                    "result_validation",
                    "completed",
                    "failed",
                }:
                    raise ConflictError("Agent answer stream contained a review-only stage")
                answer_stream.observe(event)
                # Persist exactly one Browser-visible terminal after the verified
                # answer and its message are atomically committed.  The Agent's
                # result-validation event is held until the Backend independently
                # validates the structured answer below.
                if event.stage_code == "result_validation":
                    pending_result_validation_event = event
                elif event.stage_code not in {"completed", "failed"}:
                    assistant_stage_code = cast(AssistantStageCode, event.stage_code)
                    self.repository.append_assistant_event(
                        org_id=org_id,
                        assistant_job_id=assistant_job_id,
                        expected_lease_id=lease_id,
                        event=_assistant_event(
                            assistant_job_id=assistant_job_id,
                            sequence=sequence,
                            stage_code=assistant_stage_code,
                            state=event.state,
                            message_key=event.message_key,
                            duration_ms=event.duration_ms or 0,
                            safe_counts=event.safe_counts,
                        ),
                    )
                    sequence += 1
                expected_agent_sequence += 1

            external_call_started = True
            raw_answer = self.agent.answer_v2_stream(
                request.model_dump(mode="json"),
                on_event=persist_answer_stage,
            )
            response_received = True
            answer_stream.require_success()
            try:
                validate_agent_experience_payload(
                    "assistant-answer-v2",
                    raw_answer,
                )
                answer = AssistantAnswerV2.model_validate(raw_answer)
                validate_answer_against_context(
                    answer=answer,
                    request=request,
                )
            except (ValidationError, ValueError) as exc:
                raise ConflictError(
                    "Agent assistant answer violated the trusted Backend boundary"
                ) from exc
            if pending_result_validation_event is None:
                raise ConflictError("Agent assistant answer omitted result validation")
            response_validated = True
            self.repository.append_assistant_event(
                org_id=org_id,
                assistant_job_id=assistant_job_id,
                expected_lease_id=lease_id,
                event=_assistant_event(
                    assistant_job_id=assistant_job_id,
                    sequence=sequence,
                    stage_code="result_validation",
                    state=pending_result_validation_event.state,
                    message_key=pending_result_validation_event.message_key,
                    duration_ms=pending_result_validation_event.duration_ms or 0,
                    safe_counts=pending_result_validation_event.safe_counts,
                ),
            )
            sequence += 1
            now = utc_now()
            answer_message = AssistantMessageRecord(
                message_id=(
                    "assistant_message_"
                    + canonical_digest(
                        {
                            "assistant_job_id": assistant_job_id,
                            "answer_sha256": answer.answer_sha256,
                        }
                    )
                ),
                conversation_id=started.conversation_id,
                review_run_id=started.review_run_id,
                case_id=started.case_id,
                org_id=started.org_id,
                parent_message_id=started.user_message_id,
                role="assistant",
                content=answer.direct_answer,
                structured_answer=answer,
                bound_result_sha256=request.bound_result_sha256,
                created_by="system:assistant-worker",
                created_at=now,
            )
            completed = started.model_copy(
                update={
                    "state": AssistantJobState.succeeded,
                    "answer_message_id": answer_message.message_id,
                    "updated_at": now,
                }
            )
            return self.repository.complete_assistant_job(
                job=completed,
                answer_message=answer_message,
                event=_assistant_event(
                    assistant_job_id=assistant_job_id,
                    sequence=sequence,
                    stage_code="completed",
                    state="completed",
                    message_key="assistant.completed",
                ),
                audit=self._audit(
                    system,
                    "assistant_message.completed",
                    case_id=started.case_id,
                    review_run_id=started.review_run_id,
                    detail={
                        "assistant_job_id": assistant_job_id,
                        "answer_message_id": answer_message.message_id,
                    },
                ),
                expected_lease_id=lease_id,
                expected_case_revision=run.case_revision,
            )
        except Exception as exc:
            if response_validated:
                state = AssistantJobState.response_persistence_uncertain
            elif response_received:
                state = AssistantJobState.response_received_rejected
            elif isinstance(exc, AgentPreExecutionRetryableError):
                state = AssistantJobState.failed
            elif external_call_started:
                state = AssistantJobState.delivery_uncertain
            else:
                state = AssistantJobState.failed
            try:
                self.repository.mark_assistant_job_terminal(
                    org_id=org_id,
                    assistant_job_id=assistant_job_id,
                    state=state,
                    error_code=f"{state.value}:{type(exc).__name__}",
                    event=_assistant_event(
                        assistant_job_id=assistant_job_id,
                        sequence=sequence,
                        stage_code="failed",
                        state="failed",
                        message_key=f"assistant.{state.value}",
                    ),
                    audit=self._audit(
                        system,
                        f"assistant_message.{state.value}",
                        case_id=started.case_id,
                        review_run_id=started.review_run_id,
                        detail={"assistant_job_id": assistant_job_id},
                    ),
                    expected_lease_id=lease_id,
                )
            except Exception as state_error:
                _warn_terminal_state_not_persisted(
                    boundary="assistant_terminal",
                    error=state_error,
                )
            raise

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
