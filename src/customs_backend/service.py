"""Direct case-to-Agent review flow for the two local modes."""

from __future__ import annotations

import hashlib
import re
import threading
import uuid
from dataclasses import dataclass
from datetime import date, timedelta
from typing import Any, Literal, cast
from urllib.parse import urlsplit

from pydantic import ValidationError

from customs_backend.agent_contract import AgentContractPayloadError, validate_agent_v3_payload
from customs_backend.binary_assets import inspect_binary_asset
from customs_backend.case_input_boundary import (
    CASE_DOCUMENT_INPUT_RESPONSE,
    case_input_notice,
    safe_case_turn_content,
)
from customs_backend.guidance_registry import (
    GuidanceRegistryProjectionError,
    validate_development_fixture_citation,
    validate_guidance_citation,
    validate_guidance_resolution,
)
from customs_backend.knowledge_ingress import (
    canonical_knowledge_title,
    require_safe_knowledge_history,
    require_safe_knowledge_message,
)
from customs_backend.domain import (
    AnswerSection,
    AnswerSectionKind,
    AnswerScope,
    AgentKnowledgeAnswerRequest,
    AgentKnowledgeAnswerResponse,
    AgentDocumentExtractionRequest,
    AgentDocumentInput,
    AgentAssistantAnswerRequest,
    AgentAssistantAnswerResponse,
    AgentConversationTurn,
    AgentHumanCorrection,
    AgentReviewerHsSelection,
    AgentReviewRequest,
    Assessment,
    AssuranceLevel,
    CaseClosureRecord,
    CaseClosureRequest,
    CaseCreate,
    CaseRecord,
    CaseStatus,
    ConfirmedExtractedTextCreate,
    CitationKind,
    CompletionEligibility,
    ConversationCreate,
    ConversationMessageCreate,
    ConversationMessageExchange,
    ConversationMessageRecord,
    ConversationMessageRole,
    ConversationRecord,
    CorrectionProposalCreate,
    CorrectionProposalDecisionCreate,
    CorrectionProposalDecisionRecord,
    CorrectionProposalHistoryEntry,
    CorrectionProposalRecord,
    CorrectionProposalStatus,
    DocumentRecord,
    DocumentAssetRecord,
    DocumentExtractionResult,
    DocumentType,
    ExtractionJobRecord,
    ExecutionStatus,
    FindingDecisionRecord,
    FindingDecisionRequest,
    GuidanceResolution,
    GuidanceRegistryVersion,
    HsSelectionRecord,
    HsSelectionRequest,
    InputDocumentManifestEntry,
    KnowledgeConversationCreate,
    KnowledgeConversationRecord,
    JobRecord,
    JobStatus,
    MAX_TEXT_DOCUMENT_CHARACTERS,
    MAX_TEXT_DOCUMENT_UTF8_BYTES,
    MessageCitation,
    Principal,
    RequiredAction,
    RevisionCreate,
    ReviewDisposition,
    ReviewEnqueueRequest,
    ReviewRunRecord,
    Role,
    TemporalContext,
    TemporalCoverage,
    TextDocumentCreate,
    canonical_digest,
    canonical_json_bytes,
    utc_now,
)
from customs_backend.ports import (
    AgentClient,
    AgentResponseRejectedError,
    AuthorizationError,
    ConflictError,
    KnowledgeInputRejectedError,
    NotFoundError,
    ObjectStore,
    WorkbenchRepository,
)
from customs_backend.trade_features import (
    DutyAssessmentView,
    HsClassificationView,
    HsReferenceEntryView,
    HsReferenceSearchView,
    OriginVerificationView,
    build_duty_assessment,
    build_hs_classification,
    build_origin_verification,
    lookup_hs_reference,
    search_hs_reference,
    validate_hs_selection_code,
)


_DECLARED_TYPE_MAP: dict[
    str,
    Literal[
        "invoice",
        "packing_list",
        "bill_of_lading",
        "certificate_of_origin",
        "air_waybill",
        "export_declaration",
        "supporting_evidence",
        "unknown",
    ],
] = {
    "CI": "invoice",
    "PL": "packing_list",
    "BL": "bill_of_lading",
    "CO": "certificate_of_origin",
    "AWB": "air_waybill",
    "EXPORT_DECLARATION": "export_declaration",
    "SUPPORTING_EVIDENCE": "supporting_evidence",
    "UNKNOWN": "unknown",
}
_MAX_DOCUMENTS = 20
_MAX_AGENT_REQUEST_BYTES = 2 * 1024 * 1024
_V2_GUIDANCE_SNAPSHOT_ID = re.compile(r"^[a-z0-9][a-z0-9-]{2,80}$")
_OFFICIAL_GUIDANCE_HOSTS = frozenset(
    {
        "law.go.kr",
        "www.law.go.kr",
        "customs.go.kr",
        "www.customs.go.kr",
        "dream.kotra.or.kr",
    }
)


def _id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex}"


def _scoped_idempotency_key(principal: Principal, case_id: str, supplied_key: str) -> str:
    return "review_" + canonical_digest(
        {"subject": principal.subject, "case_id": case_id, "key": supplied_key}
    )


def _scoped_conversation_idempotency_key(
    principal: Principal, conversation_id: str, supplied_key: str
) -> str:
    return "chat_" + canonical_digest(
        {
            "subject": principal.subject,
            "conversation_id": conversation_id,
            "key": supplied_key,
        }
    )


def _scoped_knowledge_conversation_idempotency_key(
    principal: Principal, conversation_id: str, supplied_key: str
) -> str:
    """Keep standalone guidance retries separate from Case-assistant retries."""

    return "knowledge_chat_" + canonical_digest(
        {
            "subject": principal.subject,
            "conversation_id": conversation_id,
            "key": supplied_key,
        }
    )


def _citation_key(citation: MessageCitation) -> tuple[str, str, int | None]:
    """Use one stable identity for legacy Case and snapshot-source citations."""

    if citation.kind == CitationKind.guidance_evidence:
        if citation.source_id is None:  # guarded by the domain model
            raise ConflictError("guidance citation has no source identity")
        return citation.kind.value, citation.source_id, citation.resolution_index
    if citation.reference_id is None:  # guarded by the domain model
        raise ConflictError("case citation has no reference identity")
    return citation.kind.value, citation.reference_id, None


def _canonical_citation(citation: MessageCitation) -> bytes:
    """Return the complete stored citation representation for section binding."""

    # ``registry_source_id`` is intentionally excluded from public response
    # serialisation, but it is part of the internal Agent boundary.  Include
    # it here so a display section cannot omit or change provenance metadata.
    value = citation.model_dump(mode="json")
    value["registry_source_id"] = citation.registry_source_id
    return canonical_json_bytes(value)


def _validate_answer_sections(
    sections: list[AnswerSection], *, citations: list[MessageCitation], global_only: bool
) -> None:
    """Keep additive display sections tied to the canonical citation list.

    The legacy ``direct_answer`` remains the compatibility projection.  A
    section may repeat a top-level citation for display, but cannot smuggle a
    second unvalidated source into storage or the Browser.
    """

    if any(section.resolution_index is not None for section in sections):
        raise ConflictError("single-scope answer sections cannot carry a resolution index")
    if any(item.resolution_index is not None for item in citations):
        raise ConflictError("single-scope citations cannot carry a resolution index")
    if len({section.kind for section in sections}) != len(sections):
        raise ConflictError("assistant response repeats an answer section kind")
    expected_order = {
        AnswerSectionKind.case_analysis: 0,
        AnswerSectionKind.general_guidance: 1,
        AnswerSectionKind.case_application: 2,
    }
    if [expected_order[section.kind] for section in sections] != sorted(
        expected_order[section.kind] for section in sections
    ):
        raise ConflictError("assistant response answer sections are out of order")
    section_kinds = {section.kind for section in sections}
    if AnswerSectionKind.case_application in section_kinds and not {
        AnswerSectionKind.case_analysis,
        AnswerSectionKind.general_guidance,
    }.issubset(section_kinds):
        raise ConflictError("case application requires case and guidance sections")
    known = {_canonical_citation(item) for item in citations}
    for section in sections:
        if global_only and section.kind != AnswerSectionKind.general_guidance:
            raise ConflictError("knowledge answer cannot contain a case-specific section")
        if not section.citations:
            raise ConflictError("answer section requires at least one canonical citation")
        section_keys = [_citation_key(item) for item in section.citations]
        if len(section_keys) != len(set(section_keys)):
            raise ConflictError("answer section repeats a citation")
        if any(_canonical_citation(item) not in known for item in section.citations):
            raise ConflictError("answer section citation differs from its canonical citation")
        if section.kind == AnswerSectionKind.case_analysis and any(
            item.kind == CitationKind.guidance_evidence for item in section.citations
        ):
            raise ConflictError("case analysis section cannot use a guidance citation")
        if section.kind == AnswerSectionKind.general_guidance and any(
            item.kind != CitationKind.guidance_evidence for item in section.citations
        ):
            raise ConflictError("general guidance section requires official guidance citations")
        if section.kind == AnswerSectionKind.case_application:
            has_case = any(item.kind != CitationKind.guidance_evidence for item in section.citations)
            has_guidance = any(
                item.kind == CitationKind.guidance_evidence for item in section.citations
            )
            if not has_case or not has_guidance:
                raise ConflictError(
                    "case application section requires case and official guidance citations"
                )


def _validate_guidance_citations(
    citations: list[MessageCitation],
    *,
    expected_as_of: date,
    functional_uat: bool,
    guidance_resolution: GuidanceResolution | None = None,
    guidance_registry_sha256: str | None = None,
) -> None:
    """Apply the profile-specific boundary to every displayed guidance source."""

    guidance_citations = [
        citation for citation in citations if citation.kind == CitationKind.guidance_evidence
    ]
    registry = None
    if guidance_resolution is not None:
        resolution = guidance_resolution
        try:
            if resolution.as_of != expected_as_of:
                raise GuidanceRegistryProjectionError("guidance resolution date is invalid")
            registry = validate_guidance_resolution(
                registry_version=resolution.registry_version,
                topic=resolution.topic,
                scope=resolution.scope,
                requested_direction=resolution.requested_direction,
                applicable_directions=resolution.applicable_directions,
                coverage=resolution.coverage,
            )
            if guidance_registry_sha256 != registry.registry_sha256:
                raise GuidanceRegistryProjectionError("guidance registry digest is invalid")
            if any(
                citation.as_of != resolution.as_of
                or citation.snapshot_id != resolution.snapshot_id
                or citation.coverage != resolution.coverage
                for citation in guidance_citations
            ):
                raise GuidanceRegistryProjectionError("guidance citation snapshot is inconsistent")
            if registry.version == "2.0" and any(
                citation.registry_source_id is None for citation in guidance_citations
            ):
                raise GuidanceRegistryProjectionError("V2 guidance citation has no registry source")
            for citation in guidance_citations:
                if citation.registry_source_id is None:
                    # Legacy V1/r3 records did not expose a registry source
                    # identity.  They remain valid but cannot be upgraded or
                    # made to claim V2 provenance retroactively.
                    continue
                if not functional_uat and citation.source_kind == "development_fixture":
                    # The Agent's deterministic local fixture binds a real
                    # registry source but a clearly synthetic fixed URI, so
                    # the official title/URI equality below can never hold.
                    # Its registry binding is still validated, and the
                    # Functional-UAT check later rejects this kind outright.
                    validate_development_fixture_citation(
                        registry=registry,
                        registry_source_id=citation.registry_source_id,
                        source_uri=str(citation.source_uri),
                        topic=resolution.topic,
                        scope=resolution.scope,
                        requested_direction=resolution.requested_direction,
                        applicable_directions=resolution.applicable_directions,
                    )
                    continue
                validate_guidance_citation(
                    registry=registry,
                    registry_source_id=citation.registry_source_id,
                    source_kind=str(citation.source_kind),
                    source_title=str(citation.source_title),
                    source_uri=str(citation.source_uri),
                    topic=resolution.topic,
                    scope=resolution.scope,
                    requested_direction=resolution.requested_direction,
                    applicable_directions=resolution.applicable_directions,
                )
        except (AttributeError, GuidanceRegistryProjectionError) as exc:
            raise ConflictError("guidance provenance is outside the registry projection") from exc
    elif guidance_registry_sha256 is not None or any(
        citation.registry_source_id is not None for citation in guidance_citations
    ):
        raise ConflictError("guidance citation has registry provenance without a resolution")
    elif any(
        citation.source_kind != "development_fixture" for citation in guidance_citations
    ):
        # New Agent responses must bind every official citation to a concrete
        # V1 or V2 registry version and digest.  This does not reinterpret
        # historic persisted messages (which are read as-is), and it leaves
        # the explicitly local deterministic fixture usable without a real
        # Data snapshot.
        raise ConflictError("official guidance citation requires registry provenance")

    for citation in guidance_citations:
        # Only official snapshot citations can become Browser links.  Keep the
        # local deterministic fixture out of this policy: it is never allowed
        # in Functional-UAT and the Browser separately refuses its URL.
        # ``www2.customs.go.kr`` and any future V2-only hostname are accepted
        # solely after the exact source/page projection above succeeds.  The
        # generic legacy host boundary intentionally does not widen to them.
        v2_exact = registry is not None and registry.version == "2.0"
        if (
            citation.source_kind != "development_fixture"
            and not v2_exact
            and not _is_safe_official_guidance_uri(citation.source_uri)
        ):
            raise ConflictError("guidance citation has an unsafe official source URI")
        if citation.as_of != expected_as_of:
            raise ConflictError("Agent assistant guidance citation is not bound to the review date")
        # The versioned knowledge registry distinguishes an enacted law from
        # its official catalog, agency guidance, requirement reference, and
        # public guidance.  They are all legitimate *official* Functional-UAT
        # sources.  Only the deterministic fixture is local-development-only.
        if functional_uat and citation.source_kind == "development_fixture":
            raise ConflictError("functional UAT guidance requires an official snapshot source")
        if functional_uat and citation.coverage not in {"covered", "partial"}:
            raise ConflictError("functional UAT guidance requires covered official knowledge")
        if citation.coverage not in {"covered", "partial"}:
            raise ConflictError("guidance citation cannot claim unavailable coverage")


def _validate_multi_scope_metadata(
    *,
    guidance_resolution: GuidanceResolution | None,
    guidance_resolutions: list[GuidanceResolution] | None,
    answer_format: str | None,
    guidance_registry_sha256: str | None,
    expected_as_of: date,
) -> tuple[GuidanceResolution, GuidanceResolution] | None:
    """Validate the fixed two-V2-scope envelope before any citation is used.

    The singular field remains optional for legacy/no-coverage responses.  The
    plural form is deliberately all-or-nothing so a client cannot mistake a
    one-scope answer for a two-scope checklist.
    """

    if guidance_resolutions is None:
        if answer_format is not None:
            raise ConflictError("multi-scope format requires two guidance resolutions")
        return None
    if guidance_resolution is not None:
        raise ConflictError("guidance resolution forms are mutually exclusive")
    if answer_format != "multi_scope_checklist":
        raise ConflictError("two guidance resolutions require the multi-scope format")
    if len(guidance_resolutions) != 2:
        raise ConflictError("multi-scope answer requires exactly two guidance resolutions")
    first, second = guidance_resolutions
    if first.registry_version != "2.0" or second.registry_version != "2.0":
        raise ConflictError("multi-scope answer requires the V2 guidance registry")
    if (first.topic, first.scope) == (second.topic, second.scope):
        raise ConflictError("multi-scope answer requires independent guidance scopes")
    if first.as_of != expected_as_of or second.as_of != expected_as_of:
        raise ConflictError("multi-scope guidance resolution date is invalid")
    try:
        registries = [
            validate_guidance_resolution(
                registry_version=resolution.registry_version,
                topic=resolution.topic,
                scope=resolution.scope,
                requested_direction=resolution.requested_direction,
                applicable_directions=resolution.applicable_directions,
                coverage=resolution.coverage,
            )
            for resolution in (first, second)
        ]
    except (AttributeError, GuidanceRegistryProjectionError) as exc:
        raise ConflictError("multi-scope guidance provenance is outside the registry projection") from exc
    if any(guidance_registry_sha256 != registry.registry_sha256 for registry in registries):
        raise ConflictError("multi-scope guidance registry digest is invalid")
    return first, second


def _validate_multi_scope_guidance_citations(
    citations: list[MessageCitation],
    *,
    resolutions: tuple[GuidanceResolution, GuidanceResolution],
    expected_as_of: date,
    functional_uat: bool,
) -> None:
    """Validate every official citation against its own resolution index."""

    registries = [
        validate_guidance_resolution(
            registry_version=resolution.registry_version,
            topic=resolution.topic,
            scope=resolution.scope,
            requested_direction=resolution.requested_direction,
            applicable_directions=resolution.applicable_directions,
            coverage=resolution.coverage,
        )
        for resolution in resolutions
    ]
    for citation in citations:
        if citation.kind != CitationKind.guidance_evidence:
            if citation.resolution_index is not None:
                raise ConflictError("case citation cannot carry a resolution index")
            continue
        index = citation.resolution_index
        if index not in {0, 1}:
            raise ConflictError("multi-scope guidance citation has no resolution index")
        resolution = resolutions[index]
        registry = registries[index]
        if (
            citation.as_of != expected_as_of
            or citation.as_of != resolution.as_of
            or citation.snapshot_id != resolution.snapshot_id
            or citation.coverage != resolution.coverage
        ):
            raise ConflictError("multi-scope guidance citation snapshot is inconsistent")
        if citation.registry_source_id is None:
            raise ConflictError("multi-scope V2 guidance citation has no registry source")
        try:
            if not functional_uat and citation.source_kind == "development_fixture":
                # Same deterministic-fixture boundary as the single-scope
                # validation: keep the registry binding, replace the official
                # title/URI equality with the exact fixed fixture URI.
                validate_development_fixture_citation(
                    registry=registry,
                    registry_source_id=citation.registry_source_id,
                    source_uri=str(citation.source_uri),
                    topic=resolution.topic,
                    scope=resolution.scope,
                    requested_direction=resolution.requested_direction,
                    applicable_directions=resolution.applicable_directions,
                )
            else:
                validate_guidance_citation(
                    registry=registry,
                    registry_source_id=citation.registry_source_id,
                    source_kind=str(citation.source_kind),
                    source_title=str(citation.source_title),
                    source_uri=str(citation.source_uri),
                    topic=resolution.topic,
                    scope=resolution.scope,
                    requested_direction=resolution.requested_direction,
                    applicable_directions=resolution.applicable_directions,
                )
        except (AttributeError, GuidanceRegistryProjectionError) as exc:
            raise ConflictError("multi-scope guidance citation is outside the registry projection") from exc
        if functional_uat and citation.source_kind == "development_fixture":
            raise ConflictError("functional UAT guidance requires an official snapshot source")
        if citation.coverage != "covered":
            raise ConflictError("multi-scope checklist citation must be covered")


def _validate_multi_scope_section_citations(
    sections: list[AnswerSection], *, citations: list[MessageCitation]
) -> None:
    """Require every displayed section citation to be an exact top-level item."""

    known = {_canonical_citation(item) for item in citations}
    for section in sections:
        if not section.citations:
            raise ConflictError("answer section requires at least one canonical citation")
        section_keys = [_citation_key(item) for item in section.citations]
        if len(section_keys) != len(set(section_keys)):
            raise ConflictError("answer section repeats a citation")
        if any(_canonical_citation(item) not in known for item in section.citations):
            raise ConflictError("answer section citation differs from its canonical citation")


def _validate_multi_scope_case_sections(
    sections: list[AnswerSection], *, citations: list[MessageCitation]
) -> None:
    expected = (
        (AnswerSectionKind.case_analysis, None),
        (AnswerSectionKind.general_guidance, 0),
        (AnswerSectionKind.general_guidance, 1),
        (AnswerSectionKind.case_application, None),
    )
    actual = tuple((section.kind, section.resolution_index) for section in sections)
    if actual != expected:
        raise ConflictError("multi-scope checklist must use the fixed four-section order")
    _validate_multi_scope_section_citations(sections, citations=citations)
    case_section, first_guidance, second_guidance, application = sections
    if any(item.kind == CitationKind.guidance_evidence for item in case_section.citations):
        raise ConflictError("multi-scope case analysis requires only case citations")
    for index, section in enumerate((first_guidance, second_guidance)):
        if any(
            item.kind != CitationKind.guidance_evidence or item.resolution_index != index
            for item in section.citations
        ):
            raise ConflictError("multi-scope guidance section uses another resolution citation")
    application_indexes = {
        item.resolution_index
        for item in application.citations
        if item.kind == CitationKind.guidance_evidence
    }
    if (
        not any(item.kind != CitationKind.guidance_evidence for item in application.citations)
        or application_indexes != {0, 1}
    ):
        raise ConflictError("multi-scope application requires case and both guidance citations")


def _validate_multi_scope_knowledge_sections(
    sections: list[AnswerSection], *, citations: list[MessageCitation]
) -> None:
    expected = (
        (AnswerSectionKind.general_guidance, 0),
        (AnswerSectionKind.general_guidance, 1),
    )
    actual = tuple((section.kind, section.resolution_index) for section in sections)
    if actual != expected:
        raise ConflictError("multi-scope knowledge answer requires two indexed guidance sections")
    _validate_multi_scope_section_citations(sections, citations=citations)
    for index, section in enumerate(sections):
        if any(
            item.kind != CitationKind.guidance_evidence or item.resolution_index != index
            for item in section.citations
        ):
            raise ConflictError("multi-scope knowledge section uses another resolution citation")


def _multi_scope_is_complete(
    resolutions: tuple[GuidanceResolution, GuidanceResolution],
) -> tuple[bool, str | None]:
    snapshots = {item.snapshot_id for item in resolutions}
    if any(item.coverage != "covered" for item in resolutions):
        return False, None
    if len(snapshots) != 1:
        return False, None
    snapshot_id = next(iter(snapshots))
    return snapshot_id is not None, snapshot_id


def _guidance_snapshot_binding(
    *,
    guidance_resolution: GuidanceResolution | None,
    guidance_resolutions: list[GuidanceResolution] | None,
) -> tuple[str | None, GuidanceRegistryVersion | None]:
    """Return a durable snapshot binding only when an answer selects one.

    A partial two-scope result deliberately returns no binding: its visible
    response does not represent one combined snapshot, so persisting one peer
    would make later turns look more complete than the original answer.  V1
    bindings remain persisted for history, but only the separate stored-V2
    helper may send a pin back to Agent/Data.
    """

    if guidance_resolutions is not None:
        if len(guidance_resolutions) != 2:
            raise ConflictError("multi-scope answer requires exactly two guidance resolutions")
        if any(item.registry_version != "2.0" for item in guidance_resolutions):
            raise ConflictError("multi-scope answer requires the V2 guidance registry")
        complete, snapshot_id = _multi_scope_is_complete(
            (guidance_resolutions[0], guidance_resolutions[1])
        )
        if not complete:
            return None, None
        assert snapshot_id is not None
        if _V2_GUIDANCE_SNAPSHOT_ID.fullmatch(snapshot_id) is None:
            raise ConflictError("V2 guidance snapshot identifier is invalid")
        return snapshot_id, "2.0"
    if guidance_resolution is None or guidance_resolution.snapshot_id is None:
        return None, None
    if (
        guidance_resolution.registry_version == "2.0"
        and _V2_GUIDANCE_SNAPSHOT_ID.fullmatch(guidance_resolution.snapshot_id) is None
    ):
        raise ConflictError("V2 guidance snapshot identifier is invalid")
    return guidance_resolution.snapshot_id, guidance_resolution.registry_version


def _stored_v2_snapshot_pin(
    *, snapshot_id: str | None, registry_version: GuidanceRegistryVersion | None
) -> str | None:
    """Return only a verified V2 pin; retain legacy V1 state without sending it."""

    if registry_version is None:
        return None
    if registry_version != "2.0":
        return None
    if snapshot_id is None or _V2_GUIDANCE_SNAPSHOT_ID.fullmatch(snapshot_id) is None:
        raise ConflictError("stored V2 guidance snapshot identifier is invalid")
    return snapshot_id


def _is_safe_official_guidance_uri(value: str | None) -> bool:
    """Accept only the versioned registry's public HTTPS source hosts."""

    if not value:
        return False
    try:
        parsed = urlsplit(value)
        return (
            parsed.scheme == "https"
            and parsed.hostname in _OFFICIAL_GUIDANCE_HOSTS
            and parsed.username is None
            and parsed.password is None
            and parsed.port is None
            # ``urlsplit`` normalises an empty explicit port (``host:/``) to
            # ``None``.  Inspect the raw authority as well so every explicit
            # port spelling remains outside the immutable official-URI policy.
            and ":" not in parsed.netloc.rsplit("@", 1)[-1]
        )
    except ValueError:
        return False


def _review_documents(documents: list[DocumentRecord]) -> list[DocumentRecord]:
    if not documents:
        raise ConflictError("at least one text document is required")
    if len(documents) > _MAX_DOCUMENTS:
        raise ConflictError("a review accepts at most twenty text documents")
    if any(
        item.character_count > MAX_TEXT_DOCUMENT_CHARACTERS
        or item.byte_size > MAX_TEXT_DOCUMENT_UTF8_BYTES
        for item in documents
    ):
        raise ConflictError("case contains a document outside the text review boundary")
    return sorted(documents, key=lambda item: item.document_id)


def _active_documents(documents: list[DocumentRecord]) -> list[DocumentRecord]:
    """Return the current case input without mutating historic documents.

    Replacement is an append-only correction: a superseded document remains
    available to historic review snapshots and the document history, but is
    never sent to a newly created Agent run.
    """

    return [item for item in documents if item.superseded_by_document_id is None]


def _public_manifest(documents: list[DocumentRecord]) -> list[InputDocumentManifestEntry]:
    return [
        InputDocumentManifestEntry(
            document_id=item.document_id,
            document_type=cast(
                Literal[
                    "CI",
                    "PL",
                    "BL",
                    "CO",
                    "AWB",
                    "EXPORT_DECLARATION",
                    "SUPPORTING_EVIDENCE",
                    "UNKNOWN",
                ],
                item.document_type,
            ),
            content_sha256=item.content_sha256,
        )
        for item in _review_documents(documents)
    ]


def _manifest_digest(
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


def _result_digest(
    *,
    provider_result_sha256: str,
    input_manifest_digest: str,
    agent_result: dict[str, Any],
) -> str:
    return canonical_digest(
        {
            "provider_result_sha256": provider_result_sha256,
            "input_manifest_digest": input_manifest_digest,
            # The complete schema-validated Agent output covers V3.2 workflow,
            # document facts, comparisons, stable references, and actions.
            # Reviewer-owned decisions are deliberately outside this digest.
            "agent_result": agent_result,
            "human_review_required": True,
        }
    )


def _agent_result_hash_payload(result: dict[str, Any]) -> dict[str, Any]:
    """Return the Agent-defined canonical payload for ``result_sha256``.

    ``temporal_context`` and ``temporal_coverage`` are additive fields.  Their
    explicit ``null`` values mean that no temporal provenance was supplied, so
    the Agent deliberately hashes them exactly like a historic result where
    the fields were absent.  Populated values remain part of the provider hash.
    """

    payload = {key: value for key, value in result.items() if key != "result_sha256"}
    if payload.get("temporal_context") is None:
        payload.pop("temporal_context", None)
    if payload.get("temporal_coverage") is None:
        payload.pop("temporal_coverage", None)
    return payload


def _agent_result_snapshot(run: ReviewRunRecord) -> dict[str, Any]:
    """Rebuild the immutable schema-owned Agent result for a chat turn.

    This intentionally refuses a projection whose stored provider hash no
    longer covers the reconstructed result.  Chat must not silently turn a
    Backend-added value into an Agent-owned fact.
    """

    result: dict[str, Any] = {
        "schema_version": run.agent_schema_version,
        "case_id": run.case_id,
        "review_run_id": run.review_run_id,
        "review_as_of": run.review_as_of.isoformat(),
        "trade_type": run.trade_type,
        "jurisdiction": run.agent_jurisdiction,
        "status": run.agent_status,
        "requires_human_review": run.agent_requires_human_review,
        "execution_status": run.execution_status.value,
        "assessment": run.assessment.value,
        "input_manifest_sha256": run.agent_input_manifest_sha256,
        "report": run.report,
        "workflow": run.workflow,
        "document_analyses": run.document_analyses,
        "comparisons": run.comparisons,
        "evidence": run.evidence,
        "findings": run.findings,
        "required_actions": [item.model_dump(mode="json") for item in run.required_actions],
        "result_sha256": run.provider_result_sha256,
    }
    if run.agent_engine is not None:
        result["engine"] = run.agent_engine

    # The nullable field is exposed consistently to the current Agent.  Its
    # null value has the same canonical provider hash as the absent field in a
    # historic result, while a populated context is hash-protected.
    result["temporal_context"] = (
        run.temporal_context.model_dump(mode="json") if run.temporal_context is not None else None
    )
    result["temporal_coverage"] = (
        run.temporal_coverage.model_dump(mode="json")
        if run.temporal_coverage is not None
        else None
    )
    if canonical_digest(_agent_result_hash_payload(result)) != run.provider_result_sha256:
        raise ConflictError("persisted Agent result is not hash-consistent for assistant context")
    return result


def _source_reference_sets(run: ReviewRunRecord) -> dict[str, set[str]]:
    facts: set[str] = set()
    for analysis in run.document_analyses:
        if not isinstance(analysis, dict):
            continue
        for fact in analysis.get("facts", []):
            if isinstance(fact, dict) and isinstance(fact.get("fact_id"), str):
                facts.add(fact["fact_id"])
        for line_item in analysis.get("line_items", []):
            if not isinstance(line_item, dict):
                continue
            for fact in line_item.get("facts", []):
                if isinstance(fact, dict) and isinstance(fact.get("fact_id"), str):
                    facts.add(fact["fact_id"])
    return {
        "fact": facts,
        "comparison": {
            item["comparison_id"]
            for item in run.comparisons
            if isinstance(item, dict) and isinstance(item.get("comparison_id"), str)
        },
        "evidence": {
            item["evidence_id"]
            for item in run.evidence
            if isinstance(item, dict) and isinstance(item.get("evidence_id"), str)
        },
    }


def _known_correction_targets(
    run: ReviewRunRecord,
) -> tuple[set[str], dict[str, tuple[str, str | None, str]], set[tuple[str, str]], set[str]]:
    """Return only references actually produced by the immutable Agent result."""

    document_ids = {item.document_id for item in run.input_document_manifest}
    facts: dict[str, tuple[str, str | None, str]] = {}
    items: set[tuple[str, str]] = set()
    for analysis in run.document_analyses:
        if not isinstance(analysis, dict) or not isinstance(analysis.get("document_id"), str):
            continue
        document_id = analysis["document_id"]
        for fact in analysis.get("facts", []):
            if (
                isinstance(fact, dict)
                and isinstance(fact.get("fact_id"), str)
                and isinstance(fact.get("name"), str)
            ):
                facts[fact["fact_id"]] = (document_id, None, fact["name"])
        for item in analysis.get("line_items", []):
            if not isinstance(item, dict) or not isinstance(item.get("item_id"), str):
                continue
            item_id = item["item_id"]
            items.add((document_id, item_id))
            for fact in item.get("facts", []):
                if (
                    isinstance(fact, dict)
                    and isinstance(fact.get("fact_id"), str)
                    and isinstance(fact.get("name"), str)
                ):
                    facts[fact["fact_id"]] = (document_id, item_id, fact["name"])
    findings = {
        item["finding_id"]
        for item in run.findings
        if isinstance(item, dict) and isinstance(item.get("finding_id"), str)
    }
    return document_ids, facts, items, findings


def _agent_request_from_snapshot(
    *, object_store: ObjectStore, review_run_id: str, case_id: str, snapshot: dict[str, Any]
) -> AgentReviewRequest:
    try:
        review_as_of = date.fromisoformat(str(snapshot["review_as_of"]))
        trade_type = snapshot["trade_type"]
        if trade_type not in {"import", "export"}:
            raise ValueError("invalid trade type")
        documents = [DocumentRecord.model_validate(value) for value in snapshot["documents"]]
        parent_review_run_id = snapshot.get("parent_review_run_id")
        if parent_review_run_id is not None and not isinstance(parent_review_run_id, str):
            raise ValueError("invalid parent review run")
        applied_correction_proposal_ids = list(snapshot.get("applied_correction_proposal_ids", []))
        human_corrections = [
            AgentHumanCorrection.model_validate(value)
            for value in list(snapshot.get("human_corrections", []))
        ]
        reviewer_hs_selection = (
            AgentReviewerHsSelection.model_validate(snapshot["reviewer_hs_selection"])
            if snapshot.get("reviewer_hs_selection") is not None
            else None
        )
    except (KeyError, TypeError, ValueError, ValidationError) as exc:
        raise ConflictError("review job has an invalid immutable input snapshot") from exc
    return AgentReviewRequest(
        review_run_id=review_run_id,
        case_id=case_id,
        review_as_of=review_as_of,
        trade_type=trade_type,
        documents=[
            AgentDocumentInput(
                document_id=document.document_id,
                filename=document.filename,
                content_text=object_store.get_text(
                    key=document.object_key, expected_sha256=document.content_sha256
                ),
                declared_type=_DECLARED_TYPE_MAP[document.document_type],
            )
            for document in _review_documents(documents)
        ],
        parent_review_run_id=parent_review_run_id,
        applied_correction_proposal_ids=applied_correction_proposal_ids,
        human_corrections=human_corrections,
        reviewer_hs_selection=reviewer_hs_selection,
    )


@dataclass(frozen=True)
class DocumentView:
    record: DocumentRecord
    content_text: str


@dataclass(frozen=True)
class DocumentAssetExtractionView:
    """Operational preview state; it is deliberately outside review input."""

    asset: DocumentAssetRecord
    job: ExtractionJobRecord


class _AssistantTurnLock:
    """Reference-counted keyed lock for one synchronous assistant exchange."""

    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.waiters = 0


class WorkbenchService:
    """Case, text document, and one synchronous human-review workflow."""

    def __init__(
        self, *, repository: WorkbenchRepository, object_store: ObjectStore, agent: AgentClient
    ) -> None:
        self.repository = repository
        self.object_store = object_store
        self.agent = agent
        # The repository atomically stores a completed exchange, but the Agent
        # call itself happens before that write.  Keep one bounded in-process
        # reservation per idempotency key so two concurrent HTTP retries never
        # generate two Agent turns for the same user message.
        self._assistant_turn_locks: dict[tuple[str, str, str], _AssistantTurnLock] = {}
        self._assistant_turn_locks_guard = threading.Lock()

    def _acquire_assistant_turn_lock(
        self, *, org_id: str, conversation_id: str, idempotency_key: str
    ) -> tuple[tuple[str, str, str], "_AssistantTurnLock"]:
        key = (org_id, conversation_id, idempotency_key)
        with self._assistant_turn_locks_guard:
            entry = self._assistant_turn_locks.get(key)
            if entry is None:
                entry = _AssistantTurnLock()
                self._assistant_turn_locks[key] = entry
            entry.waiters += 1
        entry.lock.acquire()
        return key, entry

    def _release_assistant_turn_lock(
        self, key: tuple[str, str, str], entry: "_AssistantTurnLock"
    ) -> None:
        entry.lock.release()
        with self._assistant_turn_locks_guard:
            entry.waiters -= 1
            if entry.waiters == 0 and self._assistant_turn_locks.get(key) is entry:
                del self._assistant_turn_locks[key]

    @staticmethod
    def _require_case_access(principal: Principal, record: CaseRecord) -> None:
        if principal.roles.intersection({Role.reviewer, Role.approver}):
            return
        raise NotFoundError("case not found")

    @staticmethod
    def _require_role(principal: Principal, role: Role) -> None:
        if role not in principal.roles:
            raise AuthorizationError("the current role cannot perform this operation")

    def create_case(self, principal: Principal, request: CaseCreate) -> CaseRecord:
        self._require_role(principal, Role.reviewer)
        now = utc_now()
        return self.repository.create_case(
            CaseRecord(
                case_id=_id("case"),
                org_id=principal.org_id,
                title=request.title,
                external_reference=request.external_reference,
                revision=0,
                document_manifest_digest=canonical_digest([]),
                metadata=request.metadata,
                created_by=principal.subject,
                created_at=now,
                updated_at=now,
            )
        )

    def get_case(self, principal: Principal, case_id: str) -> CaseRecord:
        record = self.repository.get_case(principal.org_id, case_id)
        if record is None:
            raise NotFoundError("case not found")
        self._require_case_access(principal, record)
        return record

    def list_cases(self, principal: Principal) -> list[CaseRecord]:
        if not principal.roles.intersection({Role.reviewer, Role.approver}):
            raise NotFoundError("case not found")
        records = self.repository.list_cases(principal.org_id)
        return records

    def add_document(
        self,
        principal: Principal,
        case_id: str,
        request: TextDocumentCreate,
        *,
        source_asset_id: str | None = None,
    ) -> tuple[CaseRecord, DocumentRecord]:
        self._require_role(principal, Role.reviewer)
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
            source_asset_id=source_asset_id,
            replaces_document_id=request.replaces_document_id,
        )
        existing = self.repository.list_documents(principal.org_id, case_id)
        replaced: DocumentRecord | None = None
        if request.replaces_document_id is not None:
            replaced = next(
                (item for item in existing if item.document_id == request.replaces_document_id),
                None,
            )
            if replaced is None:
                raise NotFoundError("replacement document not found")
            if replaced.document_type != request.document_type:
                raise ConflictError("a replacement must use the same document type")
            if replaced.superseded_by_document_id is not None:
                raise ConflictError("replacement document is already superseded")
        new_manifest_digest = canonical_digest(
            sorted(
                [
                    {"document_id": item.document_id, "content_sha256": item.content_sha256}
                    for item in [
                        *(
                            item
                            for item in _active_documents(existing)
                            if item.document_id != request.replaces_document_id
                        ),
                        record,
                    ]
                ],
                key=lambda item: item["document_id"],
            )
        )
        self.object_store.put_text(
            key=object_key, content=request.content_text, content_sha256=digest
        )
        return (
            self.repository.add_document(
                record,
                expected_revision=case.revision,
                new_manifest_digest=new_manifest_digest,
            ),
            record,
        )

    def add_document_asset(
        self,
        principal: Principal,
        case_id: str,
        *,
        document_type: DocumentType,
        filename: str,
        declared_media_type: str,
        content: bytes,
        source_sha256: str | None = None,
    ) -> DocumentAssetRecord:
        """Store a verified binary source without adding it to text review."""

        self._require_role(principal, Role.reviewer)
        case = self.get_case(principal, case_id)
        if case.status == CaseStatus.closed:
            raise ConflictError("document assets cannot be added to a closed case")
        inspection = inspect_binary_asset(
            payload=content, declared_media_type=declared_media_type
        )
        asset_id = _id("asset")
        # Public uploads calculate this digest while consuming the raw stream.
        # Direct service callers retain the deterministic fallback.  The object
        # store independently verifies the final bytes against this digest.
        if source_sha256 is None:
            digest = hashlib.sha256(content).hexdigest()
        elif len(source_sha256) == 64 and all(
            character in "0123456789abcdef" for character in source_sha256
        ):
            digest = source_sha256
        else:
            raise ValueError("document asset source hash is invalid")
        record = DocumentAssetRecord(
            asset_id=asset_id,
            case_id=case_id,
            org_id=principal.org_id,
            document_type=document_type,
            filename=filename,
            media_type=inspection.media_type,
            source_sha256=digest,
            object_key=f"{principal.org_id}/{case_id}/assets/{asset_id}/{digest}",
            byte_size=inspection.byte_size,
            page_count=inspection.page_count,
            pixel_count=inspection.pixel_count,
            created_by=principal.subject,
            created_at=utc_now(),
        )
        self.object_store.put_bytes(
            key=record.object_key,
            content=content,
            content_sha256=record.source_sha256,
            media_type=record.media_type,
        )
        return self.repository.add_document_asset(record)

    def list_document_assets(
        self, principal: Principal, case_id: str
    ) -> list[DocumentAssetRecord]:
        self.get_case(principal, case_id)
        return self.repository.list_document_assets(principal.org_id, case_id)

    def get_document_asset(
        self, principal: Principal, case_id: str, asset_id: str
    ) -> DocumentAssetRecord:
        self.get_case(principal, case_id)
        record = self.repository.get_document_asset(principal.org_id, case_id, asset_id)
        if record is None:
            raise NotFoundError("document asset not found")
        return record

    @staticmethod
    def _validate_extraction_result(
        *, asset: DocumentAssetRecord, request: AgentDocumentExtractionRequest, raw: dict[str, Any]
    ) -> DocumentExtractionResult:
        try:
            validate_agent_v3_payload("document_extraction_result", raw)
            result = DocumentExtractionResult.model_validate(raw)
        except (AgentContractPayloadError, ValidationError) as exc:
            raise AgentResponseRejectedError("Agent document extraction contract is invalid") from exc
        if (
            result.document_asset_id != asset.asset_id
            or result.extraction_request_id != request.extraction_request_id
            or result.media_type != asset.media_type
            or result.byte_size != asset.byte_size
            or result.source_sha256 != asset.source_sha256
        ):
            raise AgentResponseRejectedError("Agent document extraction provenance does not match")
        page_numbers = [page.page_number for page in result.pages]
        if len(page_numbers) != len(set(page_numbers)) or any(
            number > asset.page_count for number in page_numbers
        ):
            raise AgentResponseRejectedError("Agent document extraction page provenance is invalid")
        return result

    def request_document_extraction(
        self, principal: Principal, case_id: str, asset_id: str
    ) -> DocumentAssetExtractionView:
        """Run one isolated preview request; its result cannot affect a review."""

        self._require_role(principal, Role.reviewer)
        case = self.get_case(principal, case_id)
        if case.status == CaseStatus.closed:
            raise ConflictError("document extraction cannot be started for a closed case")
        asset = self.get_document_asset(principal, case_id, asset_id)
        now = utc_now()
        job = ExtractionJobRecord(
            extraction_job_id=_id("extract"),
            asset_id=asset.asset_id,
            case_id=case_id,
            org_id=principal.org_id,
            status="queued",
            created_by=principal.subject,
            created_at=now,
            updated_at=now,
        )
        self.repository.create_extraction_job(job)
        request = AgentDocumentExtractionRequest(
            document_asset_id=asset.asset_id,
            extraction_request_id=job.extraction_job_id,
            media_type=asset.media_type,
            source_sha256=asset.source_sha256,
            byte_size=asset.byte_size,
        )
        try:
            validate_agent_v3_payload("document_extraction_request", request.to_agent_wire())
            content = self.object_store.get_bytes(
                key=asset.object_key,
                expected_sha256=asset.source_sha256,
                maximum_bytes=asset.byte_size,
            )
            result = self._validate_extraction_result(
                asset=asset,
                request=request,
                raw=self.agent.extract_document(request, content),
            )
            updated = job.model_copy(
                update={"status": result.status, "result": result, "updated_at": utc_now()}
            )
        except AgentResponseRejectedError:
            updated = job.model_copy(
                update={"status": "failed", "error_code": "document_extraction_unavailable", "updated_at": utc_now()}
            )
        except (KeyError, ValueError):
            updated = job.model_copy(
                update={"status": "failed", "error_code": "document_asset_unavailable", "updated_at": utc_now()}
            )
        persisted = self.repository.update_extraction_job(updated)
        return DocumentAssetExtractionView(asset=asset, job=persisted)

    def get_document_extraction(
        self, principal: Principal, case_id: str, asset_id: str, extraction_job_id: str
    ) -> DocumentAssetExtractionView:
        asset = self.get_document_asset(principal, case_id, asset_id)
        job = self.repository.get_extraction_job(principal.org_id, case_id, extraction_job_id)
        if job is None or job.asset_id != asset.asset_id:
            raise NotFoundError("document extraction job not found")
        return DocumentAssetExtractionView(asset=asset, job=job)

    def confirm_extracted_text(
        self,
        principal: Principal,
        case_id: str,
        asset_id: str,
        request: ConfirmedExtractedTextCreate,
    ) -> tuple[CaseRecord, DocumentRecord]:
        """Require a human supplied text value before it joins text-only review."""

        asset = self.get_document_asset(principal, case_id, asset_id)
        job = self.repository.get_extraction_job(
            principal.org_id, case_id, request.extraction_job_id
        )
        if (
            job is None
            or job.asset_id != asset.asset_id
            or job.status not in {"preview_ready", "needs_review"}
        ):
            raise ConflictError(
                "an available extraction preview is required before text confirmation"
            )
        return self.add_document(
            principal,
            case_id,
            TextDocumentCreate(
                document_type=asset.document_type,
                filename=request.filename,
                content_text=request.content_text,
                replaces_document_id=request.replaces_document_id,
            ),
            source_asset_id=asset.asset_id,
        )

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
                key=record.object_key, expected_sha256=record.content_sha256
            ),
        )

    def _latest_hs_selection_code(self, principal: Principal, case_id: str) -> str | None:
        """Return the case's most recent reviewer-confirmed HSK code, if any."""

        for run in self.repository.list_runs(principal.org_id, case_id):
            if run.hs_selection is not None:
                return run.hs_selection.code
        return None

    def review(
        self,
        principal: Principal,
        case_id: str,
        request: ReviewEnqueueRequest,
        idempotency_key: str,
    ) -> tuple[JobRecord, bool]:
        self._require_role(principal, Role.reviewer)
        case = self.get_case(principal, case_id)
        if case.status == CaseStatus.closed:
            raise ConflictError("reviews cannot be started for a closed case")
        documents = _active_documents(self.repository.list_documents(principal.org_id, case_id))
        manifest = _public_manifest(documents)
        now = utc_now()
        review_as_of = request.review_as_of or now.date()
        input_manifest_digest = _manifest_digest(
            case_id=case_id,
            case_revision=case.revision,
            review_as_of=review_as_of,
            trade_type=request.trade_type,
            documents=manifest,
        )
        snapshot: dict[str, Any] = {
            "review_as_of": review_as_of.isoformat(),
            "trade_type": request.trade_type,
            "documents": [document.model_dump(mode="json") for document in documents],
            "input_document_manifest": [item.model_dump(mode="json") for item in manifest],
            "input_manifest_digest": input_manifest_digest,
        }
        reviewer_hs_code = self._latest_hs_selection_code(principal, case_id)
        if reviewer_hs_code is not None:
            snapshot["reviewer_hs_selection"] = {
                "code": reviewer_hs_code,
                "source_kind": "human_provided",
            }
        supplied_digest = canonical_digest(
            {"case_id": case_id, "request": request.model_dump(mode="json")}
        )
        job = JobRecord(
            job_id=_id("job"),
            case_id=case_id,
            org_id=principal.org_id,
            idempotency_key=_scoped_idempotency_key(principal, case_id, idempotency_key),
            idempotency_request_digest=supplied_digest,
            request_digest=canonical_digest(snapshot),
            case_revision=case.revision,
            request=snapshot,
            status=JobStatus.queued,
            review_run_id=_id("run"),
            created_by=principal.subject,
            created_at=now,
            updated_at=now,
        )
        stored, created = self.repository.create_review_job(
            job, expected_case_revision=case.revision
        )
        if not created:
            return stored, False
        return self._run_review(principal, stored), True

    def _build_run(
        self,
        *,
        principal: Principal,
        job: JobRecord,
        case: CaseRecord,
        result: dict[str, Any],
        expected_input_sha256: str,
    ) -> ReviewRunRecord:
        try:
            validate_agent_v3_payload("review_result", result)
            review_as_of = date.fromisoformat(str(job.request["review_as_of"]))
            trade_type = job.request["trade_type"]
            if trade_type not in {"import", "export"}:
                raise ValueError("invalid trade type")
            manifest = [
                InputDocumentManifestEntry.model_validate(value)
                for value in job.request["input_document_manifest"]
            ]
            input_manifest_digest = str(job.request["input_manifest_digest"])
            execution_status = ExecutionStatus(str(result["execution_status"]))
            assessment = Assessment(str(result["assessment"]))
            provider_result_sha256 = str(result["result_sha256"])
            agent_schema_version = str(result["schema_version"])
            agent_status = str(result["status"])
            agent_engine = dict(result["engine"]) if result.get("engine") is not None else None
            agent_jurisdiction = result["jurisdiction"]
            report = dict(result["report"])
            evidence = list(result["evidence"])
            findings = list(result["findings"])
            actions = [RequiredAction.model_validate(value) for value in result["required_actions"]]
            workflow = dict(result["workflow"])
            document_analyses = list(result["document_analyses"])
            comparisons = list(result["comparisons"])
            temporal_context = (
                TemporalContext.model_validate(result["temporal_context"])
                if result.get("temporal_context") is not None
                else None
            )
            temporal_coverage = (
                TemporalCoverage.model_validate(result["temporal_coverage"])
                if result.get("temporal_coverage") is not None
                else None
            )
            if temporal_context is not None and temporal_context.law_as_of != review_as_of:
                raise ValueError("temporal law_as_of must match review_as_of")
            if temporal_coverage is not None and temporal_context is None:
                raise ValueError("temporal coverage requires temporal context")
        except (AgentContractPayloadError, KeyError, TypeError, ValueError, ValidationError) as exc:
            raise ConflictError("Agent result does not satisfy the V3 review contract") from exc
        expected_provider_result_sha256 = canonical_digest(_agent_result_hash_payload(result))
        if provider_result_sha256 != expected_provider_result_sha256:
            raise ConflictError("Agent result hash does not cover the canonical V3.2 result")
        comparison_ids = [
            str(item.get("comparison_id"))
            for item in comparisons
            if isinstance(item, dict) and isinstance(item.get("comparison_id"), str)
        ]
        finding_ids = [
            str(item.get("finding_id"))
            for item in findings
            if isinstance(item, dict) and isinstance(item.get("finding_id"), str)
        ]
        known_comparison_ids = set(comparison_ids)
        known_finding_ids = set(finding_ids)
        if len(comparison_ids) != len(comparisons) or len(comparison_ids) != len(
            set(comparison_ids)
        ):
            raise ConflictError("Agent result has invalid comparison identifiers")
        if len(finding_ids) != len(findings) or len(finding_ids) != len(set(finding_ids)):
            raise ConflictError("Agent result has invalid finding identifiers")
        if any(
            item.get("comparison_id") is not None
            and item.get("comparison_id") not in known_comparison_ids
            for item in findings
            if isinstance(item, dict)
        ):
            raise ConflictError("Agent finding refers to an unknown comparison")
        if any(
            finding_id not in known_finding_ids
            for action in actions
            for finding_id in action.finding_ids
        ):
            raise ConflictError("Agent action refers to an unknown finding")
        if (
            result.get("case_id") != case.case_id
            or result.get("review_run_id") != job.review_run_id
            or result.get("review_as_of") != review_as_of.isoformat()
            or result.get("trade_type") != trade_type
            or result.get("input_manifest_sha256") != expected_input_sha256
            or result.get("requires_human_review") is not True
        ):
            raise ConflictError("Agent result is not bound to the immutable review input")
        if not any(action.action_code == "human_review_required" for action in actions):
            # The Agent owns every field protected by ``result_sha256``.  Do
            # not append a Backend action after that hash is verified; reject
            # the malformed response instead so a future assistant context
            # cannot contain a hash-inconsistent synthetic result.
            raise ConflictError("Agent result is missing the required human-review action")
        eligibility = CompletionEligibility(
            eligible=False, unmet_reason_codes=["human_review_required"]
        )
        return ReviewRunRecord(
            review_run_id=job.review_run_id,
            case_id=case.case_id,
            org_id=job.org_id,
            job_id=job.job_id,
            parent_review_run_id=cast(str | None, job.request.get("parent_review_run_id")),
            applied_correction_proposal_ids=list(
                job.request.get("applied_correction_proposal_ids", [])
            ),
            case_revision=job.case_revision,
            review_as_of=review_as_of,
            trade_type=trade_type,
            input_document_manifest=manifest,
            input_manifest_digest=input_manifest_digest,
            agent_input_manifest_sha256=expected_input_sha256,
            provider_result_sha256=provider_result_sha256,
            result_digest=_result_digest(
                provider_result_sha256=provider_result_sha256,
                input_manifest_digest=input_manifest_digest,
                agent_result=result,
            ),
            execution_status=execution_status,
            assessment=assessment,
            agent_schema_version=agent_schema_version,
            agent_status=agent_status,
            agent_engine=agent_engine,
            agent_jurisdiction=agent_jurisdiction,
            agent_requires_human_review=True,
            temporal_context=temporal_context,
            temporal_coverage=temporal_coverage,
            review_disposition=ReviewDisposition.human_review_required,
            assurance_level=AssuranceLevel.diagnostic,
            completion_eligibility=eligibility,
            report=report,
            evidence=evidence,
            findings=findings,
            required_actions=actions,
            workflow=workflow,
            document_analyses=document_analyses,
            comparisons=comparisons,
            release_identity={"status": "non-release", "mode": "human-review-only"},
            created_by=principal.subject,
            created_at=utc_now(),
        )

    def _run_review(
        self,
        principal: Principal,
        job: JobRecord,
        *,
        applied_correction_proposal_ids: list[str] | None = None,
    ) -> JobRecord:
        running = self.repository.update_job(
            job.model_copy(update={"status": JobStatus.running, "updated_at": utc_now()})
        )
        try:
            case = self.repository.get_case(principal.org_id, running.case_id)
            if case is None:
                raise NotFoundError("case not found")
            agent_request = _agent_request_from_snapshot(
                object_store=self.object_store,
                review_run_id=running.review_run_id,
                case_id=running.case_id,
                snapshot=running.request,
            )
            wire = agent_request.to_agent_wire()
            validate_agent_v3_payload("review_request", wire)
            if len(canonical_json_bytes(wire)) > _MAX_AGENT_REQUEST_BYTES:
                raise ConflictError("Agent review request exceeds the 2 MiB transport limit")
            result = self.agent.review(agent_request)
            run = self._build_run(
                principal=principal,
                job=running,
                case=case,
                result=result,
                expected_input_sha256=agent_request.input_manifest_sha256,
            )
            completed = running.model_copy(
                update={"status": JobStatus.succeeded, "updated_at": utc_now(), "error_code": None}
            )
            if applied_correction_proposal_ids:
                self.repository.complete_review_and_apply_correction_proposals(
                    job=completed,
                    run=run,
                    correction_proposal_ids=applied_correction_proposal_ids,
                    applied_by=principal.subject,
                )
            else:
                self.repository.complete_review(completed, run)
            return completed
        except Exception as exc:
            return self.repository.update_job(
                running.model_copy(
                    update={
                        "status": JobStatus.failed,
                        "error_code": type(exc).__name__,
                        "updated_at": utc_now(),
                    }
                )
            )

    def get_job(self, principal: Principal, job_id: str) -> JobRecord:
        job = self.repository.get_job(principal.org_id, job_id)
        if job is None:
            raise NotFoundError("job not found")
        self.get_case(principal, job.case_id)
        return job

    def get_run(self, principal: Principal, review_run_id: str) -> ReviewRunRecord:
        run = self.repository.get_run(principal.org_id, review_run_id)
        if run is None:
            raise NotFoundError("review run not found")
        self.get_case(principal, run.case_id)
        return run

    def list_runs(self, principal: Principal, case_id: str) -> list[ReviewRunRecord]:
        self.get_case(principal, case_id)
        return self.repository.list_runs(principal.org_id, case_id)

    def _get_conversation(
        self, principal: Principal, conversation_id: str
    ) -> tuple[ConversationRecord, ReviewRunRecord]:
        conversation = self.repository.get_conversation(principal.org_id, conversation_id)
        if conversation is None:
            raise NotFoundError("assistant conversation not found")
        run = self.get_run(principal, conversation.review_run_id)
        return conversation, run

    def create_conversation(
        self,
        principal: Principal,
        review_run_id: str,
        request: ConversationCreate,
    ) -> ConversationRecord:
        self.get_run(principal, review_run_id)
        now = utc_now()
        return self.repository.create_conversation(
            ConversationRecord(
                conversation_id=_id("conv"),
                review_run_id=review_run_id,
                org_id=principal.org_id,
                title=request.title,
                created_by=principal.subject,
                created_at=now,
                updated_at=now,
            )
        )

    def list_conversations(
        self, principal: Principal, review_run_id: str
    ) -> list[ConversationRecord]:
        self.get_run(principal, review_run_id)
        return self.repository.list_conversations(principal.org_id, review_run_id)

    def list_conversation_messages(
        self, principal: Principal, conversation_id: str
    ) -> list[ConversationMessageRecord]:
        self._get_conversation(principal, conversation_id)
        return self.repository.list_messages(principal.org_id, conversation_id)

    def get_conversation_run(self, principal: Principal, conversation_id: str) -> ReviewRunRecord:
        """Resolve the immutable review that owns a conversation for API recording."""

        _conversation, run = self._get_conversation(principal, conversation_id)
        return run

    def send_conversation_message(
        self,
        principal: Principal,
        conversation_id: str,
        request: ConversationMessageCreate,
        idempotency_key: str,
        *,
        functional_uat: bool = False,
    ) -> ConversationMessageExchange:
        conversation, run = self._get_conversation(principal, conversation_id)
        request_digest = canonical_digest(
            {"conversation_id": conversation_id, "content": request.content}
        )
        scoped_key = _scoped_conversation_idempotency_key(
            principal, conversation_id, idempotency_key
        )
        existing = self.repository.get_message_exchange(
            org_id=principal.org_id,
            conversation_id=conversation_id,
            idempotency_key=scoped_key,
            idempotency_request_digest=request_digest,
        )
        if existing is not None:
            return existing

        lock_key, lock_entry = self._acquire_assistant_turn_lock(
            org_id=principal.org_id,
            conversation_id=conversation_id,
            idempotency_key=scoped_key,
        )
        try:
            # A concurrent first request may have completed while this request
            # waited.  Recheck before constructing context or contacting Agent.
            existing = self.repository.get_message_exchange(
                org_id=principal.org_id,
                conversation_id=conversation_id,
                idempotency_key=scoped_key,
                idempotency_request_digest=request_digest,
            )
            if existing is not None:
                return existing

            # Another turn may have completed while this idempotency key waited.
            # Reload the immutable V2 pin before the Agent request so a later
            # lookup cannot silently select the active snapshot again.
            conversation, run = self._get_conversation(principal, conversation_id)

            input_notice = case_input_notice(request.content)
            if input_notice is not None:
                # Preserve the authorised Case conversation record, but do
                # not create a new fact, finding, correction, Agent request,
                # or Functional-UAT transcript containing the pasted source
                # text.  A document can influence review only after the
                # normal document registration/review workflow.
                now = utc_now()
                exchange, _created = self.repository.append_message_exchange(
                    user_message=ConversationMessageRecord(
                        message_id=_id("msg"),
                        conversation_id=conversation_id,
                        role=ConversationMessageRole.user,
                        content=request.content,
                        created_by=principal.subject,
                        created_at=now,
                    ),
                    assistant_message=ConversationMessageRecord(
                        message_id=_id("msg"),
                        conversation_id=conversation_id,
                        role=ConversationMessageRole.assistant,
                        content=CASE_DOCUMENT_INPUT_RESPONSE,
                        answer_scope=AnswerScope.insufficient_evidence,
                        suggested_questions=[
                            "문서를 등록 또는 교체한 뒤 새 심사를 시작하려면 어떻게 하나요?"
                        ],
                        case_input_notice=input_notice,
                        created_by="customs-backend",
                        created_at=now + timedelta(microseconds=1),
                    ),
                    idempotency_key=scoped_key,
                    idempotency_request_digest=request_digest,
                )
                return exchange

            history = self.repository.list_messages(principal.org_id, conversation_id)[-8:]
            answer_request = AgentAssistantAnswerRequest(
                case_id=run.case_id,
                review_run_id=run.review_run_id,
                conversation_id=conversation_id,
                review_result=_agent_result_snapshot(run),
                question=request.content,
                recent_turns=[
                    AgentConversationTurn(
                        role=item.role,
                        content=safe_case_turn_content(role=item.role.value, content=item.content),
                    )
                    for item in history
                ],
                expected_snapshot_id=_stored_v2_snapshot_pin(
                    snapshot_id=conversation.snapshot_id,
                    registry_version=conversation.guidance_registry_version,
                ),
            )
            answer_wire = answer_request.to_agent_wire()
            try:
                validate_agent_v3_payload("assistant_answer_request", answer_wire)
                raw_answer = self.agent.answer(answer_request)
                validate_agent_v3_payload("assistant_answer_response", raw_answer)
                answer = AgentAssistantAnswerResponse.model_validate(raw_answer)
            except (
                AgentContractPayloadError,
                AgentResponseRejectedError,
                ValidationError,
                ValueError,
            ) as exc:
                raise ConflictError(
                    "Agent assistant response does not satisfy the bounded contract"
                ) from exc
            if (
                answer.case_id != run.case_id
                or answer.review_run_id != run.review_run_id
                or answer.conversation_id != conversation_id
            ):
                raise ConflictError("Agent assistant response is not bound to the conversation")
            citation_keys = [_citation_key(item) for item in answer.citations]
            if len(citation_keys) != len(set(citation_keys)):
                raise ConflictError("Agent assistant response repeats a citation")
            if answer.answer_scope.value in {"case_grounded", "evidence_grounded"} and not any(
                item.kind != CitationKind.guidance_evidence for item in answer.citations
            ):
                raise ConflictError("grounded Agent assistant response requires a citation")
            if answer.answer_scope == AnswerScope.general_customs_guidance and not any(
                item.kind == CitationKind.guidance_evidence for item in answer.citations
            ):
                raise ConflictError(
                    "general Agent assistant response requires an official citation"
                )
            references = _source_reference_sets(run)
            if any(
                citation.kind != CitationKind.guidance_evidence
                and citation.reference_id not in references[citation.kind.value]
                for citation in answer.citations
            ):
                raise ConflictError("Agent assistant response contains an unknown citation")
            multi_resolutions = _validate_multi_scope_metadata(
                guidance_resolution=answer.guidance_resolution,
                guidance_resolutions=answer.guidance_resolutions,
                answer_format=answer.answer_format,
                guidance_registry_sha256=answer.guidance_registry_sha256,
                expected_as_of=run.review_as_of,
            )
            if multi_resolutions is None:
                _validate_guidance_citations(
                    answer.citations,
                    expected_as_of=run.review_as_of,
                    functional_uat=functional_uat,
                    guidance_resolution=answer.guidance_resolution,
                    guidance_registry_sha256=answer.guidance_registry_sha256,
                )
                _validate_answer_sections(
                    answer.answer_sections, citations=answer.citations, global_only=False
                )
            else:
                if answer.correction_draft is not None:
                    raise ConflictError("multi-scope checklist cannot contain a correction draft")
                complete, _snapshot_id = _multi_scope_is_complete(multi_resolutions)
                if answer.answer_scope == AnswerScope.insufficient_evidence:
                    if answer.citations or answer.answer_sections:
                        raise ConflictError(
                            "incomplete multi-scope answer cannot expose citations or sections"
                        )
                else:
                    if not complete:
                        raise ConflictError("multi-scope checklist requires both scopes covered")
                    if answer.answer_scope not in {
                        AnswerScope.case_grounded,
                        AnswerScope.evidence_grounded,
                    }:
                        raise ConflictError("multi-scope checklist requires a case-grounded answer")
                    guidance_citations = [
                        item
                        for item in answer.citations
                        if item.kind == CitationKind.guidance_evidence
                    ]
                    if {item.resolution_index for item in guidance_citations} != {0, 1}:
                        raise ConflictError(
                            "multi-scope guidance citations require both resolution indexes"
                        )
                    _validate_multi_scope_guidance_citations(
                        answer.citations,
                        resolutions=multi_resolutions,
                        expected_as_of=run.review_as_of,
                        functional_uat=functional_uat,
                    )
                    _validate_multi_scope_case_sections(
                        answer.answer_sections, citations=answer.citations
                    )
            if answer.case_input_notice is not None and (
                answer.citations
                or answer.answer_sections
                or answer.correction_draft is not None
                or answer.guidance_resolution is not None
                or answer.guidance_resolutions is not None
                or answer.answer_format is not None
                or answer.guidance_registry_sha256 is not None
            ):
                raise ConflictError("case input notice response cannot create review evidence")
            if any(not item.strip() for item in answer.suggested_questions):
                raise ConflictError("Agent assistant response contains an empty suggested question")
            if answer.correction_draft is not None:
                self._validate_correction_target(
                    run,
                    document_id=answer.correction_draft.document_id,
                    field_name=answer.correction_draft.field_name,
                    fact_id=answer.correction_draft.fact_id,
                    item_id=answer.correction_draft.item_id,
                    finding_id=None,
                )
            conversation_snapshot_id, guidance_registry_version = _guidance_snapshot_binding(
                guidance_resolution=answer.guidance_resolution,
                guidance_resolutions=answer.guidance_resolutions,
            )
            now = utc_now()
            exchange, _created = self.repository.append_message_exchange(
                user_message=ConversationMessageRecord(
                    message_id=_id("msg"),
                    conversation_id=conversation_id,
                    role=ConversationMessageRole.user,
                    content=request.content,
                    created_by=principal.subject,
                    created_at=now,
                ),
                assistant_message=ConversationMessageRecord(
                    message_id=_id("msg"),
                    conversation_id=conversation_id,
                    role=ConversationMessageRole.assistant,
                    content=answer.direct_answer,
                    answer_scope=answer.answer_scope,
                    citations=answer.citations,
                    answer_sections=answer.answer_sections,
                    suggested_questions=answer.suggested_questions,
                    correction_draft=answer.correction_draft,
                    guidance_resolution=answer.guidance_resolution,
                    guidance_resolutions=answer.guidance_resolutions,
                    answer_format=answer.answer_format,
                    case_input_notice=answer.case_input_notice,
                    created_by="customs-ai-agent",
                    created_at=now + timedelta(microseconds=1),
                ),
                idempotency_key=scoped_key,
                idempotency_request_digest=request_digest,
                conversation_snapshot_id=conversation_snapshot_id,
                guidance_registry_version=guidance_registry_version,
            )
            return exchange
        finally:
            self._release_assistant_turn_lock(lock_key, lock_entry)

    @staticmethod
    def _require_knowledge_access(principal: Principal) -> None:
        if not principal.roles.intersection({Role.reviewer, Role.approver}):
            raise NotFoundError("knowledge conversation not found")

    def _get_knowledge_conversation(
        self, principal: Principal, conversation_id: str
    ) -> KnowledgeConversationRecord:
        self._require_knowledge_access(principal)
        record = self.repository.get_knowledge_conversation(
            principal.org_id, principal.subject, conversation_id
        )
        if record is None:
            raise NotFoundError("knowledge conversation not found")
        return record

    def create_knowledge_conversation(
        self,
        principal: Principal,
        request: KnowledgeConversationCreate,
    ) -> KnowledgeConversationRecord:
        self._require_knowledge_access(principal)
        # A standalone knowledge thread must never become a second
        # persistence route for case material.  Its durable title is code
        # owned rather than caller prose, so a first pasted question cannot
        # survive in conversation-list text through this field.
        title = canonical_knowledge_title(request.title, request.trade_type)
        if request.as_of is None:
            # A caller outside the public API must not bypass its fixed-date
            # resolution and issue an ambiguous Agent/Data request.
            raise ConflictError("knowledge conversation requires a resolved as_of date")
        now = utc_now()
        return self.repository.create_knowledge_conversation(
            KnowledgeConversationRecord(
                conversation_id=_id("knowledge_conv"),
                org_id=principal.org_id,
                title=title,
                trade_type=request.trade_type,
                as_of=request.as_of,
                created_by=principal.subject,
                created_at=now,
                updated_at=now,
            )
        )

    def list_knowledge_conversations(
        self, principal: Principal
    ) -> list[KnowledgeConversationRecord]:
        self._require_knowledge_access(principal)
        return self.repository.list_knowledge_conversations(principal.org_id, principal.subject)

    def list_knowledge_conversation_messages(
        self, principal: Principal, conversation_id: str
    ) -> list[ConversationMessageRecord]:
        self._get_knowledge_conversation(principal, conversation_id)
        return self.repository.list_knowledge_messages(
            principal.org_id, principal.subject, conversation_id
        )

    def send_knowledge_conversation_message(
        self,
        principal: Principal,
        conversation_id: str,
        request: ConversationMessageCreate,
        idempotency_key: str,
        *,
        functional_uat: bool = False,
    ) -> ConversationMessageExchange:
        conversation = self._get_knowledge_conversation(principal, conversation_id)
        # Validate before digesting, replaying, persisting, or dispatching.
        # In particular, a rejected first turn cannot create a user/assistant
        # exchange; a Browser-created generic title is the only permitted
        # persisted shell for that failed attempt.
        require_safe_knowledge_message(request.content)
        existing_history = self.repository.list_knowledge_messages(
            principal.org_id, principal.subject, conversation_id
        )
        require_safe_knowledge_history(
            title=conversation.title,
            turns=((item.role.value, item.content) for item in existing_history),
        )
        request_digest = canonical_digest(
            {
                "conversation_id": conversation_id,
                "content": request.content,
                "trade_type": conversation.trade_type,
                "as_of": conversation.as_of.isoformat(),
            }
        )
        scoped_key = _scoped_knowledge_conversation_idempotency_key(
            principal, conversation_id, idempotency_key
        )
        existing = self.repository.get_knowledge_message_exchange(
            org_id=principal.org_id,
            owner_subject=principal.subject,
            conversation_id=conversation_id,
            idempotency_key=scoped_key,
            idempotency_request_digest=request_digest,
        )
        if existing is not None:
            return existing

        lock_key, lock_entry = self._acquire_assistant_turn_lock(
            org_id=principal.org_id,
            conversation_id=conversation_id,
            idempotency_key=scoped_key,
        )
        try:
            existing = self.repository.get_knowledge_message_exchange(
                org_id=principal.org_id,
                owner_subject=principal.subject,
                conversation_id=conversation_id,
                idempotency_key=scoped_key,
                idempotency_request_digest=request_digest,
            )
            if existing is not None:
                return existing
            # Re-read under the per-turn lock.  Historical user turns remain
            # fail-closed, and standalone knowledge never replays historical
            # prose to Agent, Data, Claude, or a transcript.
            conversation = self._get_knowledge_conversation(principal, conversation_id)
            history = self.repository.list_knowledge_messages(
                principal.org_id, principal.subject, conversation_id
            )
            require_safe_knowledge_history(
                title=conversation.title,
                turns=((item.role.value, item.content) for item in history),
            )
            answer_request = AgentKnowledgeAnswerRequest(
                conversation_id=conversation_id,
                question=request.content,
                trade_type=conversation.trade_type,
                as_of=conversation.as_of,
                recent_turns=[],
                expected_snapshot_id=_stored_v2_snapshot_pin(
                    snapshot_id=conversation.snapshot_id,
                    registry_version=conversation.guidance_registry_version,
                ),
            )
            answer_wire = answer_request.to_agent_wire()
            try:
                validate_agent_v3_payload("knowledge_answer_request", answer_wire)
                raw_answer = self.agent.answer_knowledge(answer_request)
                validate_agent_v3_payload("knowledge_answer_response", raw_answer)
                answer = AgentKnowledgeAnswerResponse.model_validate(raw_answer)
            except KnowledgeInputRejectedError:
                # The Agent maintains the same independent boundary.  Its
                # fixed 422 signal is safe to expose through the public API,
                # while any provider text remains suppressed.
                raise
            except (
                AgentContractPayloadError,
                AgentResponseRejectedError,
                ValidationError,
                ValueError,
            ) as exc:
                raise ConflictError(
                    "Agent knowledge answer does not satisfy the bounded contract"
                ) from exc
            if answer.conversation_id != conversation_id:
                raise ConflictError("Agent knowledge answer is not bound to the conversation")
            if answer.as_of != conversation.as_of:
                raise ConflictError("Agent knowledge answer is not bound to the requested date")
            if answer.answer_scope.value not in {
                "general_customs_guidance",
                "clarification_needed",
                "insufficient_evidence",
                "out_of_scope",
            }:
                raise ConflictError("knowledge answer has an invalid scope")
            multi_resolutions = _validate_multi_scope_metadata(
                guidance_resolution=answer.guidance_resolution,
                guidance_resolutions=answer.guidance_resolutions,
                answer_format=answer.answer_format,
                guidance_registry_sha256=answer.guidance_registry_sha256,
                expected_as_of=conversation.as_of,
            )
            citation_keys = [_citation_key(item) for item in answer.citations]
            if len(citation_keys) != len(set(citation_keys)):
                raise ConflictError("Agent knowledge answer repeats a citation")
            if any(item.kind != CitationKind.guidance_evidence for item in answer.citations):
                raise ConflictError("knowledge answer contains a non-guidance citation")
            if multi_resolutions is None:
                if any(
                    item.as_of != answer.as_of
                    or item.snapshot_id != answer.snapshot_id
                    or item.coverage != answer.coverage
                    for item in answer.citations
                ):
                    raise ConflictError(
                        "knowledge answer citation does not match its resolved snapshot"
                    )
                if answer.guidance_resolution is not None and (
                    answer.guidance_resolution.as_of != answer.as_of
                    or answer.guidance_resolution.snapshot_id != answer.snapshot_id
                    or answer.guidance_resolution.coverage != answer.coverage
                ):
                    raise ConflictError("knowledge answer resolution does not match its snapshot")
                _validate_guidance_citations(
                    answer.citations,
                    expected_as_of=conversation.as_of,
                    functional_uat=functional_uat,
                    guidance_resolution=answer.guidance_resolution,
                    guidance_registry_sha256=answer.guidance_registry_sha256,
                )
                if answer.answer_scope.value == "general_customs_guidance" and not citation_keys:
                    raise ConflictError("grounded knowledge answer requires an official citation")
                if answer.answer_scope.value == "general_customs_guidance" and answer.coverage not in {
                    "covered",
                    "partial",
                }:
                    raise ConflictError("grounded knowledge answer has no covered snapshot")
                # A V2 no-coverage answer can be deliberately resolved before
                # Data is called (for example, export-side customs value).  Its
                # ``snapshot_id`` is therefore ``null``.  It cannot replace the
                # immutable snapshot already pinned by an earlier covered answer,
                # but it is not a snapshot switch either.  A *non-null* later
                # snapshot still has to match exactly.
                if (
                    conversation.snapshot_id is not None
                    and answer.snapshot_id is not None
                    and answer.snapshot_id != conversation.snapshot_id
                ):
                    raise ConflictError("knowledge conversation cannot switch its resolved snapshot")
                _validate_answer_sections(
                    answer.answer_sections, citations=answer.citations, global_only=True
                )
                if answer.answer_scope.value != "general_customs_guidance" and answer.answer_sections:
                    raise ConflictError(
                        "non-guidance knowledge answer cannot contain a guidance section"
                    )
                sections = answer.answer_sections
                if answer.answer_scope.value == "general_customs_guidance" and not sections:
                    sections = [
                        AnswerSection(
                            kind=AnswerSectionKind.general_guidance,
                            title="공식 통관 안내",
                            content=answer.direct_answer,
                            citations=answer.citations,
                        )
                    ]
                knowledge_snapshot_id = answer.snapshot_id
            else:
                complete, common_snapshot_id = _multi_scope_is_complete(multi_resolutions)
                if answer.answer_scope == AnswerScope.insufficient_evidence:
                    if answer.citations or answer.answer_sections:
                        raise ConflictError(
                            "incomplete multi-scope answer cannot expose citations or sections"
                        )
                else:
                    if not complete or common_snapshot_id is None:
                        raise ConflictError("multi-scope knowledge answer requires both scopes covered")
                    if answer.answer_scope != AnswerScope.general_customs_guidance:
                        raise ConflictError(
                            "multi-scope knowledge answer requires official guidance scope"
                        )
                    if answer.coverage != "covered" or answer.snapshot_id != common_snapshot_id:
                        raise ConflictError(
                            "multi-scope knowledge answer does not match its common snapshot"
                        )
                    if {item.resolution_index for item in answer.citations} != {0, 1}:
                        raise ConflictError(
                            "multi-scope guidance citations require both resolution indexes"
                        )
                    _validate_multi_scope_guidance_citations(
                        answer.citations,
                        resolutions=multi_resolutions,
                        expected_as_of=conversation.as_of,
                        functional_uat=functional_uat,
                    )
                    _validate_multi_scope_knowledge_sections(
                        answer.answer_sections, citations=answer.citations
                    )
                sections = answer.answer_sections
            if any(not item.strip() for item in answer.suggested_questions):
                raise ConflictError("Agent knowledge answer contains an empty suggested question")
            _binding_snapshot_id, guidance_registry_version = _guidance_snapshot_binding(
                guidance_resolution=answer.guidance_resolution,
                guidance_resolutions=answer.guidance_resolutions,
            )
            if multi_resolutions is None:
                # Preserve pre-V2 snapshot persistence for legacy responses
                # that predate an explicit registry resolution, while only a
                # verified V2 registry version is ever sent back as a selector.
                knowledge_snapshot_id = answer.snapshot_id
            else:
                knowledge_snapshot_id = _binding_snapshot_id
            now = utc_now()
            exchange, _created = self.repository.append_knowledge_message_exchange(
                user_message=ConversationMessageRecord(
                    message_id=_id("knowledge_msg"),
                    conversation_id=conversation_id,
                    role=ConversationMessageRole.user,
                    content=request.content,
                    created_by=principal.subject,
                    created_at=now,
                ),
                assistant_message=ConversationMessageRecord(
                    message_id=_id("knowledge_msg"),
                    conversation_id=conversation_id,
                    role=ConversationMessageRole.assistant,
                    content=answer.direct_answer,
                    answer_scope=answer.answer_scope,
                    citations=answer.citations,
                    answer_sections=sections,
                    suggested_questions=answer.suggested_questions,
                    knowledge_coverage=answer.coverage,
                    knowledge_snapshot_id=answer.snapshot_id,
                    knowledge_as_of=answer.as_of,
                    guidance_resolution=answer.guidance_resolution,
                    guidance_resolutions=answer.guidance_resolutions,
                    answer_format=answer.answer_format,
                    created_by="customs-ai-agent",
                    created_at=now + timedelta(microseconds=1),
                ),
                knowledge_snapshot_id=knowledge_snapshot_id,
                idempotency_key=scoped_key,
                idempotency_request_digest=request_digest,
                guidance_registry_version=guidance_registry_version,
            )
            return exchange
        finally:
            self._release_assistant_turn_lock(lock_key, lock_entry)

    @staticmethod
    def _validate_correction_target(
        run: ReviewRunRecord,
        *,
        document_id: str,
        field_name: str,
        fact_id: str | None,
        item_id: str | None,
        finding_id: str | None,
    ) -> None:
        document_ids, facts, items, findings = _known_correction_targets(run)
        if document_id not in document_ids:
            raise NotFoundError("correction document is not part of this review input")
        if item_id is not None and (document_id, item_id) not in items:
            raise NotFoundError("correction item is not part of this review input")
        if fact_id is not None:
            target = facts.get(fact_id)
            if target is None or target[0] != document_id:
                raise NotFoundError("correction fact is not part of this review input")
            if item_id is not None and target[1] != item_id:
                raise ConflictError("correction fact and item do not match")
            if target[2] != field_name:
                raise ConflictError("correction field does not match the selected fact")
        if finding_id is not None and finding_id not in findings:
            raise NotFoundError("correction finding is not part of this review")

    def create_correction_proposal(
        self,
        principal: Principal,
        review_run_id: str,
        request: CorrectionProposalCreate,
    ) -> CorrectionProposalRecord:
        self._require_role(principal, Role.reviewer)
        run = self.get_run(principal, review_run_id)
        self._validate_correction_target(
            run,
            document_id=request.document_id,
            field_name=request.field_name,
            fact_id=request.fact_id,
            item_id=request.item_id,
            finding_id=request.finding_id,
        )
        now = utc_now()
        return self.repository.create_correction_proposal(
            CorrectionProposalRecord(
                correction_proposal_id=_id("correction"),
                review_run_id=review_run_id,
                case_id=run.case_id,
                org_id=principal.org_id,
                parent_result_digest=run.result_digest,
                parent_input_manifest_sha256=run.agent_input_manifest_sha256,
                document_id=request.document_id,
                field_name=request.field_name,
                new_value=request.new_value,
                unit=request.unit,
                fact_id=request.fact_id,
                item_id=request.item_id,
                finding_id=request.finding_id,
                reason=request.reason,
                created_by=principal.subject,
                created_at=now,
                history=[
                    CorrectionProposalHistoryEntry(
                        event="proposed",
                        reason=request.reason,
                        actor=principal.subject,
                        occurred_at=now,
                    )
                ],
            )
        )

    def list_correction_proposals(
        self, principal: Principal, review_run_id: str
    ) -> list[CorrectionProposalRecord]:
        self.get_run(principal, review_run_id)
        return self.repository.list_correction_proposals(principal.org_id, review_run_id)

    def decide_correction_proposal(
        self,
        principal: Principal,
        correction_proposal_id: str,
        request: CorrectionProposalDecisionCreate,
    ) -> CorrectionProposalRecord:
        self._require_role(principal, Role.approver)
        proposal = self.repository.get_correction_proposal(principal.org_id, correction_proposal_id)
        if proposal is None:
            raise NotFoundError("correction proposal not found")
        self.get_run(principal, proposal.review_run_id)
        if proposal.created_by == principal.subject:
            raise ConflictError("a correction proposal cannot be approved by its creator")
        now = utc_now()
        return self.repository.decide_correction_proposal(
            org_id=principal.org_id,
            correction_proposal_id=correction_proposal_id,
            decision=CorrectionProposalDecisionRecord(
                status=request.status,
                reason=request.reason,
                decided_by=principal.subject,
                decided_at=now,
            ),
        )

    def create_revision(
        self,
        principal: Principal,
        review_run_id: str,
        request: RevisionCreate,
        idempotency_key: str,
    ) -> tuple[JobRecord, bool]:
        self._require_role(principal, Role.approver)
        parent = self.get_run(principal, review_run_id)
        case = self.get_case(principal, parent.case_id)
        supplied_digest = canonical_digest(
            {"review_run_id": review_run_id, "proposal_ids": request.proposal_ids}
        )
        scoped_idempotency_key = _scoped_idempotency_key(principal, case.case_id, idempotency_key)
        # Idempotency is deliberately checked before proposal state.  Once a
        # child succeeds, its approved proposals are marked applied; a network
        # retry with the original key must replay its JobResponse rather than
        # being rejected as a second attempt.
        existing = self.repository.get_review_job_by_idempotency(
            principal.org_id, scoped_idempotency_key
        )
        if existing is not None:
            if existing.idempotency_request_digest != supplied_digest:
                raise ConflictError("idempotency key was reused with another request")
            return existing, False
        if case.status == CaseStatus.closed:
            raise ConflictError("revisions cannot be started for a closed case")
        proposals: list[CorrectionProposalRecord] = []
        target_keys: set[tuple[str, str | None, str | None, str]] = set()
        for proposal_id in request.proposal_ids:
            proposal = self.repository.get_correction_proposal(principal.org_id, proposal_id)
            if proposal is None:
                raise NotFoundError("correction proposal not found")
            if proposal.review_run_id != parent.review_run_id:
                raise ConflictError("correction proposal belongs to another review run")
            if proposal.status != CorrectionProposalStatus.approved:
                raise ConflictError("only approved correction proposals can be applied")
            if proposal.applied_review_run_id is not None:
                raise ConflictError("approved correction proposal was already applied")
            if (
                proposal.parent_result_digest != parent.result_digest
                or proposal.parent_input_manifest_sha256 != parent.agent_input_manifest_sha256
            ):
                raise ConflictError("correction proposal is no longer bound to its parent result")
            self._validate_correction_target(
                parent,
                document_id=proposal.document_id,
                field_name=proposal.field_name,
                fact_id=proposal.fact_id,
                item_id=proposal.item_id,
                finding_id=proposal.finding_id,
            )
            target_key = (
                proposal.document_id,
                proposal.item_id,
                proposal.fact_id,
                proposal.field_name,
            )
            if target_key in target_keys:
                raise ConflictError("a revision cannot apply two corrections to the same target")
            target_keys.add(target_key)
            proposals.append(proposal)
        parent_job = self.repository.get_job(principal.org_id, parent.job_id)
        if parent_job is None:
            raise ConflictError("parent review input snapshot is unavailable")
        correction_context = [
            AgentHumanCorrection(
                correction_proposal_id=proposal.correction_proposal_id,
                document_id=proposal.document_id,
                item_id=proposal.item_id,
                fact_id=proposal.fact_id,
                field_name=proposal.field_name,
                new_value=proposal.new_value,
                unit=proposal.unit,
                reason=proposal.reason,
            ).model_dump(mode="json")
            for proposal in proposals
        ]
        snapshot = {
            **parent_job.request,
            "parent_review_run_id": parent.review_run_id,
            "applied_correction_proposal_ids": list(request.proposal_ids),
            "human_corrections": correction_context,
        }
        revision_hs_code = self._latest_hs_selection_code(principal, case.case_id)
        if revision_hs_code is not None:
            snapshot["reviewer_hs_selection"] = {
                "code": revision_hs_code,
                "source_kind": "human_provided",
            }
        now = utc_now()
        job = JobRecord(
            job_id=_id("job"),
            case_id=case.case_id,
            org_id=principal.org_id,
            idempotency_key=scoped_idempotency_key,
            idempotency_request_digest=supplied_digest,
            request_digest=canonical_digest(snapshot),
            case_revision=case.revision,
            request=snapshot,
            status=JobStatus.queued,
            review_run_id=_id("run"),
            created_by=principal.subject,
            created_at=now,
            updated_at=now,
        )
        stored, created = self.repository.create_review_job(
            job, expected_case_revision=case.revision
        )
        if not created:
            return stored, False
        return (
            self._run_review(
                principal,
                stored,
                applied_correction_proposal_ids=list(request.proposal_ids),
            ),
            True,
        )

    def record_finding_decision(
        self,
        principal: Principal,
        review_run_id: str,
        finding_id: str,
        request: FindingDecisionRequest,
    ) -> FindingDecisionRecord:
        self._require_role(principal, Role.reviewer)
        run = self.get_run(principal, review_run_id)
        known_finding_ids = {
            str(item.get("finding_id"))
            for item in run.findings
            if isinstance(item, dict) and isinstance(item.get("finding_id"), str)
        }
        if finding_id not in known_finding_ids:
            raise NotFoundError("finding not found")
        return self.repository.record_finding_decision(
            org_id=principal.org_id,
            review_run_id=review_run_id,
            finding_id=finding_id,
            status=request.status,
            reason=request.reason.strip(),
            decided_by=principal.subject,
        )

    def delete_case(self, principal: Principal, case_id: str) -> None:
        """Local admin removal of a whole case; reviewer-only, irreversible."""

        self._require_role(principal, Role.reviewer)
        self.get_case(principal, case_id)
        self.repository.delete_case(org_id=principal.org_id, case_id=case_id)

    def close_case(
        self, principal: Principal, case_id: str, request: CaseClosureRequest
    ) -> CaseRecord:
        """Record the reviewer's final internal disposition and close the case.

        Closing requires a completed review run whose findings all carry a
        reviewer decision - a case is never closed over undecided findings.
        Nothing is transmitted anywhere; this is an internal state change.
        """

        self._require_role(principal, Role.reviewer)
        case = self.get_case(principal, case_id)
        if case.status == CaseStatus.closed:
            raise ConflictError("the case is already closed")
        runs = self.repository.list_runs(principal.org_id, case_id)
        latest = max(runs, key=lambda run: run.created_at, default=None)
        if latest is None:
            raise ConflictError("a case needs a completed review before it can be closed")
        decided = {decision.finding_id for decision in latest.reviewer_decisions}
        undecided = [
            str(item.get("finding_id"))
            for item in latest.findings
            if isinstance(item, dict)
            and isinstance(item.get("finding_id"), str)
            and item["finding_id"] not in decided
        ]
        if undecided:
            raise ConflictError(
                "every finding needs a reviewer decision before the case can be closed"
            )
        closure = CaseClosureRecord(
            disposition=request.disposition,
            reason=request.reason.strip(),
            review_run_id=latest.review_run_id,
            closed_by=principal.subject,
            closed_at=utc_now(),
        )
        return self.repository.close_case(
            org_id=principal.org_id, case_id=case_id, closure=closure
        )

    def review_progress(self, principal: Principal, case_id: str) -> dict[str, Any]:
        """Display-only live progress of the newest in-flight review, if any.

        The Agent keeps this in memory during the synchronous review call; a
        missing or stale snapshot degrades to "running with no stages" or
        "idle" rather than an error.
        """

        self.get_case(principal, case_id)
        job = self.repository.latest_review_job(principal.org_id, case_id)
        if job is None or job.status not in {JobStatus.queued, JobStatus.running}:
            return {"status": "idle", "stages": []}
        snapshot = self.agent.get_review_progress(job.review_run_id)
        if not isinstance(snapshot, dict):
            return {
                "status": "running",
                "review_run_id": job.review_run_id,
                "stages": [],
            }
        stages = [
            {"stage": str(item.get("stage", "")), "status": str(item.get("status", ""))}
            for item in snapshot.get("stages", [])
            if isinstance(item, dict)
        ]
        return {
            "status": "running",
            "review_run_id": job.review_run_id,
            "stages": stages,
        }

    def hs_classification(
        self, principal: Principal, review_run_id: str
    ) -> HsClassificationView:
        run = self.get_run(principal, review_run_id)
        return build_hs_classification(run)

    def hs_reference_entry(self, principal: Principal, code: str) -> HsReferenceEntryView:
        del principal  # the packaged reference table has no org scope
        if re.fullmatch(r"[0-9]{10}", code) is None:
            raise NotFoundError("an HSK reference lookup takes a 10-digit code")
        return lookup_hs_reference(code)

    def hs_reference_search(self, principal: Principal, query: str) -> HsReferenceSearchView:
        del principal  # the packaged reference table has no org scope
        return search_hs_reference(query)

    def record_hs_selection(
        self,
        principal: Principal,
        review_run_id: str,
        request: HsSelectionRequest,
    ) -> HsSelectionRecord:
        self._require_role(principal, Role.reviewer)
        run = self.get_run(principal, review_run_id)
        try:
            validate_hs_selection_code(request.code)
        except ValueError as exc:
            raise ConflictError(str(exc)) from exc
        return self.repository.record_hs_selection(
            org_id=principal.org_id,
            review_run_id=run.review_run_id,
            code=request.code,
            reason=request.reason.strip(),
            selected_by=principal.subject,
        )

    def origin_verification(
        self, principal: Principal, review_run_id: str
    ) -> OriginVerificationView:
        run = self.get_run(principal, review_run_id)
        return build_origin_verification(run)

    def duty_assessment(self, principal: Principal, review_run_id: str) -> DutyAssessmentView:
        run = self.get_run(principal, review_run_id)
        return build_duty_assessment(run)
