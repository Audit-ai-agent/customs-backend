from __future__ import annotations

import json
import time
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from typing import Annotated, Any, Literal, Never

from fastapi import Depends, FastAPI, Header, HTTPException, Request, status
from fastapi.exceptions import RequestValidationError
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field
from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from customs_backend.adapters import (
    DramatiqJobPublisher,
    HttpAgentClient,
    InMemoryJobPublisher,
    InMemoryObjectStore,
    MinioObjectStore,
)
from customs_backend.assertion_signing import (
    LocalUatRequirementPolicyIdentityResolver,
    RequirementPolicyIdentityResolver,
    SupportingAssertionSigner,
    load_assertion_signer,
    load_local_uat_assertion_signer,
    load_local_uat_requirement_policy_resolver,
    load_requirement_policy_resolver,
)
from customs_backend.auth import OidcJwtVerifier, principal_dependency, require_roles
from customs_backend.domain import (
    AgentCallState,
    AuditRecord,
    CaseCreate,
    CaseRecord,
    ChatRecord,
    CorrectionProposalCreate,
    CorrectionProposalDecisionCreate,
    CorrectionProposalRecord,
    DeliveryReconciliationCreate,
    GroundedQuestion,
    HumanDecisionRecord,
    JobStatus,
    MAX_SUPPORTING_DOCUMENTS_PER_CASE,
    Principal,
    ReleaseIdentityV3,
    ReviewEnqueueRequest,
    RevisionProposalCreate,
    ReviewRunRecord,
    Role,
    StageEvent,
    SupportingDocumentCreate,
    SupportingDocumentRecord,
    TextDocumentCreate,
    VerificationAssertionCreate,
)
from customs_backend.ports import (
    AgentClient,
    ConflictError,
    JobPublisher,
    NotFoundError,
    ObjectStore,
    QuotaExceededError,
    TokenVerifier,
    WorkbenchRepository,
)
from customs_backend.experience import (
    AssistantConversationRecord,
    AgentUatCapabilityAttestationV1,
    AssistantJobEnqueueResponse,
    AssistantJobRecord,
    AssistantJobView,
    AssistantMessageCreateV2,
    AssistantMessageRecord,
    ConversationCreateV2,
    JobExperienceView,
    PublicReviewExperienceV1,
)
from customs_backend.experience_contract import (
    expected_meaning_catalog_sha256,
    validate_agent_experience_contract_bundle,
    validate_agent_experience_payload,
)
from customs_backend.release import (
    AgentReleasePin,
    AgentServicePin,
    EvidenceRuntimeReleaseIdentityV3,
    LocalUatRuntimeBindingV1,
    ReleaseConfigurationError,
    load_auto_completion_approval,
    load_local_uat_runtime_binding,
    validate_agent_contract_bundle,
)
from customs_backend.repository import InMemoryWorkbenchRepository, PostgresWorkbenchRepository
from customs_backend.service import WorkbenchService
from customs_backend.settings import Settings, get_settings
from customs_backend.uat_evidence_trace import (
    UatEvidenceTraceError,
    UatEvidenceTraceReader,
    UatEvidenceTraceResponse,
)

IdempotencyKey = Annotated[
    str,
    Header(alias="Idempotency-Key", min_length=8, max_length=128, pattern=r"^[A-Za-z0-9._:-]+$"),
]

MAX_PUBLIC_REQUEST_BODY_BYTES = 4 * 1024 * 1024


class ProblemDetailsV1(BaseModel):
    model_config = {"extra": "forbid"}

    type: Literal["about:blank"] = "about:blank"
    title: str
    status: int
    code: str
    detail: str
    instance: str


class _RequestBodyTooLarge(Exception):
    pass


class RequestBodyLimitMiddleware:
    """Enforce the same 4 MiB public body cap declared by the reverse proxy."""

    def __init__(self, app: ASGIApp, *, max_bytes: int) -> None:
        self.app = app
        self.max_bytes = max_bytes

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http" or not str(scope.get("path", "")).startswith("/api/"):
            await self.app(scope, receive, send)
            return
        headers = {key.lower(): value for key, value in scope.get("headers", [])}
        content_length = headers.get(b"content-length")
        if content_length is not None:
            try:
                if int(content_length) > self.max_bytes:
                    await self._reject(scope, receive, send)
                    return
            except ValueError:
                await self._reject(scope, receive, send)
                return
        received = 0

        async def limited_receive() -> Message:
            nonlocal received
            message = await receive()
            if message["type"] == "http.request":
                received += len(message.get("body", b""))
                if received > self.max_bytes:
                    raise _RequestBodyTooLarge
            return message

        try:
            await self.app(scope, limited_receive, send)
        except _RequestBodyTooLarge:
            await self._reject(scope, receive, send)

    @staticmethod
    async def _reject(scope: Scope, receive: Receive, send: Send) -> None:
        response = _error_response(
            413,
            "request_body_too_large",
            instance=str(scope.get("path", "/")),
        )
        await response(scope, receive, send)


class DocumentViewResponse(BaseModel):
    class PublicDocumentRecord(BaseModel):
        document_id: str
        case_id: str
        document_type: str
        filename: str
        media_type: str
        content_sha256: str
        byte_size: int
        character_count: int
        created_by: str
        created_at: datetime

    record: PublicDocumentRecord
    content_text: str | None = None


class CaseListResponse(BaseModel):
    items: list[CaseRecord]


class DocumentListResponse(BaseModel):
    items: list[DocumentViewResponse]


class JobView(BaseModel):
    job_id: str
    case_id: str
    kind: Literal["review", "revision"]
    parent_run_id: str | None = None
    status: JobStatus
    review_run_id: str
    error_code: str | None = None
    attempt_count: int
    agent_call_state: AgentCallState
    created_by: str
    created_at: datetime
    updated_at: datetime


class JobResponse(BaseModel):
    job: JobView
    idempotency_reused: bool


class JobExperienceResponse(BaseModel):
    model_config = {"extra": "forbid"}

    job: JobExperienceView
    idempotency_reused: bool


class AssistantConversationListResponse(BaseModel):
    model_config = {"extra": "forbid"}

    items: list[AssistantConversationRecord]


class AssistantMessageListResponse(BaseModel):
    model_config = {"extra": "forbid"}

    items: list[AssistantMessageRecord]


class AssistantJobListResponse(BaseModel):
    model_config = {"extra": "forbid"}

    items: list[AssistantJobView]


def _assistant_job_view(job: AssistantJobRecord) -> AssistantJobView:
    return AssistantJobView(
        assistant_job_id=job.assistant_job_id,
        request_binding_sha256=job.agent_request_sha256,
        conversation_id=job.conversation_id,
        review_run_id=job.review_run_id,
        case_id=job.case_id,
        state=job.state,
        events=job.events,
        answer_message_id=job.answer_message_id,
        error_code=job.error_code,
        created_at=job.created_at,
        updated_at=job.updated_at,
    )


class JobEventsResponse(BaseModel):
    items: list[StageEvent]


class ReviewRunListResponse(BaseModel):
    items: list[ReviewRunRecord]


class DecisionListResponse(BaseModel):
    items: list[HumanDecisionRecord]


class SupportingDocumentListResponse(BaseModel):
    items: list[SupportingDocumentRecord] = Field(max_length=MAX_SUPPORTING_DOCUMENTS_PER_CASE)


class CorrectionProposalListResponse(BaseModel):
    items: list[CorrectionProposalRecord]


class CorrectionProposalDecisionResponse(BaseModel):
    proposal: CorrectionProposalRecord
    decision: HumanDecisionRecord
    job: JobView | None = None


class ChatListResponse(BaseModel):
    items: list[ChatRecord]


class AuditListResponse(BaseModel):
    items: list[AuditRecord]


class SessionResponse(BaseModel):
    subject: str
    org_id: str
    roles: list[Role]


class AuthConfigResponse(BaseModel):
    issuer: str
    audience: str
    client_id: str
    authorization_endpoint: str
    token_endpoint: str
    end_session_endpoint: str | None = None
    flow: str = "authorization_code_pkce_s256"
    token_storage: str = "memory_only"


class UatEvidenceStoreCountsResponse(BaseModel):
    model_config = {"extra": "forbid"}

    postgresql_article_count: int = Field(ge=0, le=10_000_000)
    qdrant_point_count: int = Field(ge=0, le=10_000_000)
    neo4j_article_count: int = Field(ge=0, le=10_000_000)


class UatEvidenceDatasetResponse(BaseModel):
    model_config = {"extra": "forbid"}

    data_source: Literal[
        "SYNTHETIC_V3_NO_RAG_QUALITY",
        "ACTUAL_POSTGRESQL_QDRANT_NEO4J_READ_ONLY",
        "UNKNOWN",
    ]
    expected_production_law_count: int = Field(ge=1, le=10_000)
    observed_law_count: int = Field(ge=0, le=10_000)
    observed_article_count: int = Field(ge=0, le=10_000_000)
    store_counts: UatEvidenceStoreCountsResponse
    rag_quality_evaluated: Literal[False] = False


class UatMetadataResponse(BaseModel):
    model_config = {"extra": "forbid"}

    mode: Literal["local_uat"] = "local_uat"
    assurance_ceiling: Literal["diagnostic"] = "diagnostic"
    text_only: Literal[True] = True
    auto_completion_enabled: Literal[False] = False
    claude_mode: Literal["actual"] = "actual"
    evidence_profile: Literal[
        "synthetic-v3",
        "preserved-3law-v3-diagnostic",
        "preserved-3law-v3-scenario-eval",
        "unknown",
    ]
    evidence_scope: Literal[
        "TEST_ONLY_V3",
        "ACTUAL_3_LAW_RAG_3_OF_58",
        "ACTUAL_3_LAW_RAG_SCENARIO_EVAL_3_OF_58",
        "UNKNOWN",
    ]
    evidence_scope_label: Literal[
        "TEST_ONLY_V3",
        "ACTUAL_3_LAW_RAG_3_OF_58",
        "ACTUAL_3_LAW_RAG_SCENARIO_EVAL_3_OF_58",
        "UNKNOWN",
    ]
    evidence_data_source: Literal[
        "SYNTHETIC_V3_NO_RAG_QUALITY",
        "ACTUAL_POSTGRESQL_QDRANT_NEO4J_READ_ONLY",
        "UNKNOWN",
    ]
    evidence_policy_mode: Literal[
        "SYNTHETIC_FIXTURE",
        "ACTUAL_CODES_HUMAN_ONLY",
        "SEALED_TEST_ONLY_ACTUAL_CCCT_HUMAN_ONLY",
        "UNKNOWN",
    ]
    evidence_dataset: UatEvidenceDatasetResponse
    supported_review_as_of: date
    planner_mode: Literal["shadow", "diagnostic_active"]
    production_release: Literal[False] = False


class UatMetadataV2Response(UatMetadataResponse):
    model_config = {"extra": "forbid"}

    enhanced_agent_experience: bool = False
    uat_adaptive_active: bool = False
    agent_experience_api_version: Literal["disabled", "3.0"] = "disabled"
    assistant_api_version: Literal["disabled", "2.0"] = "disabled"
    actual_stage_streaming: bool = False


@dataclass(frozen=True, slots=True)
class _UatEvidencePolicyIdentityPin:
    requirement_policy_release: str
    requirement_policy_manifest_sha256: str
    coverage_matrix_sha256: str
    reviewer_trust_store_sha256: str | None = None


_UAT_EVIDENCE_POLICY_IDENTITY_PINS: Mapping[str, _UatEvidencePolicyIdentityPin] = {
    "synthetic-v3": _UatEvidencePolicyIdentityPin(
        requirement_policy_release="test-only-conformance.no-production-policy.v1",
        requirement_policy_manifest_sha256=(
            "882d18df864cf19149589705c27e9d94b8441e80c8d6bef397a7507cf67a9c90"
        ),
        coverage_matrix_sha256=("a47fd7e683b7e0a270c5d4b76365034b2f24a0b4f2a52ba1c69421480c1f5263"),
    ),
    "preserved-3law-v3-diagnostic": _UatEvidencePolicyIdentityPin(
        requirement_policy_release="test-only-conformance.no-production-policy.v1",
        requirement_policy_manifest_sha256=(
            "54aa31a0baed9a8eeeea57f9c436a78b8d687e244538491a46e34af46238cf7a"
        ),
        coverage_matrix_sha256=("a47fd7e683b7e0a270c5d4b76365034b2f24a0b4f2a52ba1c69421480c1f5263"),
    ),
    "preserved-3law-v3-scenario-eval": _UatEvidencePolicyIdentityPin(
        requirement_policy_release="test-only-three-law-scenario-eval-human-only.v1",
        requirement_policy_manifest_sha256=(
            "2467226fe64c84f8fdc0b41a8ab44ebaba04e6ba7280cd3e4bd56bd2e9c38c95"
        ),
        coverage_matrix_sha256=("21012116b4fddf0ae0e0bb12343558aca472373480d32d535e6b59875800c444"),
        reviewer_trust_store_sha256=(
            "7864482157b701b44c457bd35c7721b31ffc31564ea43d83948aa27e08233637"
        ),
    ),
}


def _validate_uat_evidence_policy_identity(
    profile: str,
    identity: EvidenceRuntimeReleaseIdentityV3,
) -> None:
    """Independently bind Agent-reported V3 policy bytes to the Backend profile."""

    pin = _UAT_EVIDENCE_POLICY_IDENTITY_PINS.get(profile)
    if pin is None or (
        identity.requirement_policy_release != pin.requirement_policy_release
        or identity.requirement_policy_manifest_sha256 != pin.requirement_policy_manifest_sha256
        or identity.coverage_matrix_sha256 != pin.coverage_matrix_sha256
        or (
            pin.reviewer_trust_store_sha256 is not None
            and identity.reviewer_trust_store_sha256 != pin.reviewer_trust_store_sha256
        )
    ):
        raise ReleaseConfigurationError(
            "Agent UAT Evidence policy identity differs from the selected profile"
        )


@dataclass
class Container:
    settings: Settings
    repository: WorkbenchRepository
    object_store: ObjectStore
    publisher: JobPublisher
    agent: AgentClient
    token_verifier: TokenVerifier
    service: WorkbenchService
    local_uat_runtime_binding: LocalUatRuntimeBindingV1 | None = None
    uat_evidence_trace_reader: UatEvidenceTraceReader | None = None


def _binding_requires_static_service_pin(
    binding: LocalUatRuntimeBindingV1 | None,
) -> bool:
    """Keep the static release pin at the candidate boundary only.

    A short-lived development binding already pins the live wheel, source tree,
    model, contracts, capability and Evidence runtime.  Requiring the packaged
    release pin as well would force every local edit through the release-pin
    update chain that this binding is specifically intended to avoid.
    """

    return binding is not None and binding.mode != "development"


def build_container(settings: Settings) -> Container:
    if settings.process_role != "runtime":
        raise ReleaseConfigurationError("the HTTP/worker container requires the runtime role")
    validate_agent_contract_bundle()
    local_uat_runtime_binding = (
        load_local_uat_runtime_binding(
            settings.uat_runtime_binding_path,
            expected_file_sha256=settings.uat_runtime_binding_sha256,
        )
        if settings.uat_runtime_binding_path is not None
        and settings.uat_runtime_binding_sha256 is not None
        else None
    )
    if (
        settings.environment == "local_uat"
        and settings.enhanced_agent_experience
        and local_uat_runtime_binding is None
    ):
        raise ReleaseConfigurationError(
            "enhanced local UAT runtime requires one exact runtime binding file"
        )
    if (
        local_uat_runtime_binding is not None
        and local_uat_runtime_binding.loopback_origin.rstrip("/") != settings.public_base_url
    ):
        raise ReleaseConfigurationError(
            "local UAT runtime binding origin differs from the Backend public origin"
        )
    verifier = OidcJwtVerifier(
        issuer=settings.oidc_issuer,
        audience=settings.oidc_audience,
        jwks_url=settings.oidc_jwks_url,
        org_claim=settings.oidc_org_claim,
        roles_claim=settings.oidc_roles_claim,
    )
    service_pin_values = {
        "build_sha": settings.agent_expected_build_sha,
        "wheel_sha256": settings.agent_expected_wheel_sha256,
        "source_snapshot_sha256": settings.agent_expected_source_snapshot_sha256,
        "package_version": settings.agent_expected_package_version,
        "agent_code_snapshot_sha256": settings.agent_expected_code_snapshot_sha256,
        "model_id": settings.agent_expected_model_id,
        "extraction_prompt_sha256": settings.agent_expected_extraction_prompt_sha256,
        "extraction_schema_sha256": settings.agent_expected_extraction_schema_sha256,
        "coordinator_prompt_sha256": settings.agent_expected_coordinator_prompt_sha256,
        "narrative_prompt_sha256": settings.agent_expected_narrative_prompt_sha256,
        "chat_prompt_sha256": settings.agent_expected_chat_prompt_sha256,
        "coordinator_schema_sha256": settings.agent_expected_coordinator_schema_sha256,
        "narrative_schema_sha256": settings.agent_expected_narrative_schema_sha256,
        "chat_schema_sha256": settings.agent_expected_chat_schema_sha256,
        "policy_id": settings.agent_expected_policy_id,
        "tool_registry_id": settings.agent_expected_tool_registry_id,
        "assertion_trust_store_release_state": (
            settings.agent_expected_assertion_trust_store_release_state
        ),
        "assertion_trust_store_sha256": settings.agent_expected_assertion_trust_store_sha256,
        "evidence_provider_release": settings.agent_expected_evidence_provider_release,
        "evidence_provider_revision": settings.agent_expected_evidence_provider_revision,
        "evidence_provider_build_sha256": (settings.agent_expected_evidence_provider_build_sha256),
        "evidence_provider_source_manifest_sha256": (
            settings.agent_expected_evidence_provider_source_manifest_sha256
        ),
        "evidence_provider_retrieval_schema_sha256": (
            settings.agent_expected_evidence_provider_retrieval_schema_sha256
        ),
        "evidence_provider_readiness_schema_sha256": (
            settings.agent_expected_evidence_provider_readiness_schema_sha256
        ),
    }
    if any(value is not None for value in service_pin_values.values()) and not all(
        value is not None for value in service_pin_values.values()
    ):
        raise ReleaseConfigurationError("the Agent service pin must be complete or absent")
    service_pin = (
        AgentServicePin.model_validate(
            {
                **{
                    key: value
                    for key, value in service_pin_values.items()
                    if not key.startswith("evidence_provider_")
                },
                "evidence_provider_contract": {
                    "contract": "evidence-runtime-release-provider-lock",
                    "schema_version": "1.0",
                    "provider_release": service_pin_values["evidence_provider_release"],
                    "provider_revision": service_pin_values["evidence_provider_revision"],
                    "provider_build_sha256": service_pin_values["evidence_provider_build_sha256"],
                    "provider_source_manifest_sha256": service_pin_values[
                        "evidence_provider_source_manifest_sha256"
                    ],
                    "retrieval_schema_sha256": service_pin_values[
                        "evidence_provider_retrieval_schema_sha256"
                    ],
                    "readiness_schema_sha256": service_pin_values[
                        "evidence_provider_readiness_schema_sha256"
                    ],
                },
            }
        )
        if all(value is not None for value in service_pin_values.values())
        else None
    )
    if _binding_requires_static_service_pin(local_uat_runtime_binding) and service_pin is None:
        raise ReleaseConfigurationError(
            "candidate UAT runtime binding requires the packaged Agent service contract pin"
        )
    release_values = {
        "model_revision": settings.agent_expected_model_revision,
        "evidence_snapshot_id": settings.agent_expected_evidence_snapshot_id,
        "evidence_runtime_release_sha256": (
            settings.agent_expected_evidence_runtime_release_sha256
        ),
        "evidence_runtime_v3_sha256": settings.agent_expected_evidence_runtime_v3_sha256,
        "release_profile_id": settings.agent_expected_release_profile_id,
        "release_profile_sha256": settings.agent_expected_release_profile_sha256,
        "release_profile_subject_sha256": settings.agent_expected_release_profile_subject_sha256,
        "release_profile_evaluation_report_sha256": (
            settings.agent_expected_release_profile_evaluation_report_sha256
        ),
        "reviewer_trust_store_sha256": settings.agent_expected_reviewer_trust_store_sha256,
    }
    if any(value is not None for value in release_values.values()) and not all(
        value is not None for value in release_values.values()
    ):
        raise ReleaseConfigurationError(
            "the automatic-completion Agent release pin must be complete or absent"
        )
    release_pin = (
        AgentReleasePin.model_validate(
            {
                **service_pin.model_dump(mode="json"),
                **release_values,
                "release_profile_state": "approved",
                "kill_switch_active": False,
            }
        )
        if service_pin is not None and all(value is not None for value in release_values.values())
        else None
    )
    approval = load_auto_completion_approval(
        settings.auto_completion_approval_manifest_path,
        expected_digest=settings.auto_completion_approval_digest,
        expected_profile_id=settings.auto_completion_profile_id,
        expected_release_pin=release_pin,
        expected_backend_decision_policy_release=settings.human_decision_policy_release,
    )
    if settings.environment in {"local_uat", "production"}:
        if (
            settings.database_url is None
            or settings.agent_service_token is None
            or settings.minio_endpoint is None
            or settings.minio_access_key is None
            or settings.minio_secret_key is None
            or settings.redis_url is None
        ):
            raise ReleaseConfigurationError(
                f"{settings.environment} runtime dependencies are incomplete"
            )
        repository: WorkbenchRepository = PostgresWorkbenchRepository(
            settings.database_url.get_secret_value()
        )
        object_store: ObjectStore = MinioObjectStore(
            endpoint=settings.minio_endpoint,
            access_key=settings.minio_access_key.get_secret_value(),
            secret_key=settings.minio_secret_key.get_secret_value(),
            bucket=settings.minio_bucket,
            secure=settings.minio_secure,
        )
        publisher: JobPublisher = DramatiqJobPublisher(settings.redis_url.get_secret_value())
        agent: AgentClient = HttpAgentClient(
            base_url=settings.agent_base_url,
            service_token=settings.agent_service_token.get_secret_value(),
            timeout_seconds=settings.agent_timeout_seconds,
        )
    else:
        repository = InMemoryWorkbenchRepository()
        object_store = InMemoryObjectStore()
        publisher = InMemoryJobPublisher()
        # Development still uses the real private HTTP boundary when configured.
        # An absent token makes construction fail closed instead of selecting a Mock Agent.
        if settings.agent_service_token is None:
            agent = _UnavailableAgentClient()
        else:
            agent = HttpAgentClient(
                base_url=settings.agent_base_url,
                service_token=settings.agent_service_token.get_secret_value(),
                timeout_seconds=settings.agent_timeout_seconds,
            )
    assertion_signer: SupportingAssertionSigner
    requirement_policy_resolver: RequirementPolicyIdentityResolver
    uat_evidence_trace_reader: UatEvidenceTraceReader | None = None
    if (
        settings.environment == "local_uat"
        and settings.uat_evidence_profile == "preserved-3law-v3-scenario-eval"
    ):
        if (
            settings.uat_assertion_signing_key_registry_path is None
            or settings.uat_assertion_trust_store_sha256 is None
            or settings.uat_requirement_policy_fixture_path is None
            or settings.uat_requirement_policy_fixture_sha256 is None
            or settings.uat_evidence_runtime_v3_sha256 is None
        ):
            raise ReleaseConfigurationError("local UAT scenario assertion fixture is incomplete")
        assertion_signer = load_local_uat_assertion_signer(
            settings.uat_assertion_signing_key_registry_path,
            expected_trust_store_sha256=settings.uat_assertion_trust_store_sha256,
        )
        requirement_policy_resolver = load_local_uat_requirement_policy_resolver(
            settings.uat_requirement_policy_fixture_path,
            expected_fixture_sha256=settings.uat_requirement_policy_fixture_sha256,
            expected_runtime_v3_sha256=settings.uat_evidence_runtime_v3_sha256,
        )
        if settings.uat_evidence_trace_path is None:
            raise ReleaseConfigurationError("local UAT scenario Evidence trace is incomplete")
        uat_evidence_trace_reader = UatEvidenceTraceReader(
            settings.uat_evidence_trace_path,
            expected_runtime_v3_sha256=settings.uat_evidence_runtime_v3_sha256,
        )
    else:
        assertion_signer = load_assertion_signer(
            settings.assertion_signing_key_registry_path,
            expected_trust_store_sha256=(
                settings.agent_expected_assertion_trust_store_sha256
                or "ccad11713d4c18b4f19246f32fc68cbd4faba2d66889f9c037845fdf66db3b13"
            ),
        )
        requirement_policy_resolver = load_requirement_policy_resolver(
            policy_path=settings.requirement_policy_release_path,
            trust_store_path=settings.requirement_policy_trust_store_path,
            runtime_v3_path=settings.evidence_runtime_v3_release_path,
            expected_runtime_v3_sha256=settings.agent_expected_evidence_runtime_v3_sha256,
        )
    service = WorkbenchService(
        repository=repository,
        object_store=object_store,
        publisher=publisher,
        agent=agent,
        auto_completion_enabled=settings.auto_completion_enabled,
        approved_profile_id=settings.auto_completion_profile_id,
        service_pin=service_pin,
        release_pin=release_pin,
        auto_completion_approval=approval,
        human_decision_policy_release=settings.human_decision_policy_release,
        assertion_signer=assertion_signer,
        requirement_policy_resolver=requirement_policy_resolver,
        expected_evidence_runtime_v3_sha256=(
            settings.agent_expected_evidence_runtime_v3_sha256
            or settings.uat_evidence_runtime_v3_sha256
        ),
        job_lease_seconds=settings.job_lease_seconds,
        job_max_attempts=settings.job_max_attempts,
        outbox_lease_seconds=settings.outbox_lease_seconds,
        enhanced_agent_experience=settings.enhanced_agent_experience,
        uat_adaptive_active=settings.uat_adaptive_active,
        evidence_profile=settings.uat_evidence_profile,
        expected_uat_capability_sha256=(settings.uat_expected_capability_sha256),
        local_uat_runtime_binding=local_uat_runtime_binding,
        uat_evidence_trace_reader=uat_evidence_trace_reader,
    )
    return Container(
        settings,
        repository,
        object_store,
        publisher,
        agent,
        verifier,
        service,
        local_uat_runtime_binding,
        uat_evidence_trace_reader,
    )


class _UnavailableAgentClient:
    def _fail(self) -> Never:
        raise RuntimeError("Agent service token is not configured")

    def review(self, request: Any) -> dict[str, Any]:
        self._fail()

    def revise(self, request: Any) -> dict[str, Any]:
        self._fail()

    def revise_experience(self, request: Any) -> dict[str, Any]:
        self._fail()

    def review_experience(self, request: Any) -> dict[str, Any]:
        self._fail()

    def review_experience_stream(self, request: Any, *, on_event: Any) -> dict[str, Any]:
        self._fail()

    def answer(
        self,
        *,
        provider_result: dict[str, Any],
        question: str,
        question_id: str,
    ) -> Any:
        self._fail()

    def answer_v2(self, payload: dict[str, Any]) -> dict[str, Any]:
        self._fail()

    def answer_v2_stream(self, payload: dict[str, Any], *, on_event: Any) -> dict[str, Any]:
        self._fail()

    def metadata(self) -> dict[str, Any]:
        self._fail()

    def experience_metadata(self) -> dict[str, Any]:
        self._fail()

    def uat_readiness(self) -> dict[str, Any]:
        self._fail()


def _service(request: Request) -> WorkbenchService:
    return request.app.state.container.service


def create_app(*, container: Container | None = None, settings: Settings | None = None) -> FastAPI:
    actual_settings = settings or get_settings()
    actual_container = container or build_container(actual_settings)
    app = FastAPI(
        title="Customs Review Workbench API",
        version="2.0.0",
        description=(
            "Internal, text-only workbench. Browser clients call this API only; "
            "Agent and Evidence credentials are never exposed."
        ),
        responses={
            code: {
                "model": ProblemDetailsV1,
                "description": "Problem Details V1 fail-closed response.",
            }
            for code in (400, 401, 403, 404, 409, 413, 422, 429, 503)
        },
    )
    app.add_middleware(
        RequestBodyLimitMiddleware,
        max_bytes=MAX_PUBLIC_REQUEST_BODY_BYTES,
    )
    app.state.container = actual_container

    @app.middleware("http")
    async def no_store_authenticated_api(request: Request, call_next):
        response = await call_next(request)
        if request.url.path.startswith(("/api/v1/", "/api/v2/")):
            response.headers["Cache-Control"] = "no-store"
            response.headers["Pragma"] = "no-cache"
        return response

    @app.exception_handler(NotFoundError)
    async def not_found_handler(request: Request, _exc: NotFoundError):
        return _error_response(
            status.HTTP_404_NOT_FOUND,
            "not_found",
            instance=request.url.path,
        )

    @app.exception_handler(ConflictError)
    async def conflict_handler(request: Request, _exc: ConflictError):
        return _error_response(
            status.HTTP_409_CONFLICT,
            "stale_or_conflicting_state",
            instance=request.url.path,
        )

    @app.exception_handler(ValueError)
    async def value_handler(request: Request, _exc: ValueError):
        return _error_response(
            status.HTTP_422_UNPROCESSABLE_ENTITY,
            "invalid_operation",
            instance=request.url.path,
        )

    @app.exception_handler(QuotaExceededError)
    async def quota_handler(request: Request, _exc: QuotaExceededError):
        return _error_response(
            status.HTTP_429_TOO_MANY_REQUESTS,
            "interaction_quota_exceeded",
            instance=request.url.path,
            headers={"Retry-After": "60"},
        )

    @app.exception_handler(RequestValidationError)
    async def request_validation_handler(request: Request, _exc: RequestValidationError):
        return _error_response(
            status.HTTP_422_UNPROCESSABLE_ENTITY,
            "request_validation_failed",
            instance=request.url.path,
        )

    @app.exception_handler(HTTPException)
    async def http_exception_handler(request: Request, exc: HTTPException):
        codes = {
            400: "bad_request",
            401: "authentication_required",
            403: "permission_denied",
            404: "not_found",
            405: "method_not_allowed",
            409: "stale_or_conflicting_state",
            413: "request_body_too_large",
            422: "request_validation_failed",
            429: "interaction_quota_exceeded",
            503: "dependency_not_ready",
        }
        return _error_response(
            exc.status_code,
            codes.get(exc.status_code, "http_error"),
            instance=request.url.path,
            headers=exc.headers,
        )

    @app.get("/health/live", include_in_schema=False)
    def health_live() -> dict[str, str]:
        return {"status": "alive"}

    @app.get("/health/ready", include_in_schema=False)
    def health_ready() -> dict[str, Any]:
        if actual_container.settings.environment == "local_uat":
            raise HTTPException(
                status_code=503,
                detail="local UAT is never Production release-ready",
            )
        verifier = actual_container.token_verifier
        auth_ready = not isinstance(verifier, OidcJwtVerifier) or verifier.health()
        dependency_status: dict[str, bool] = {}
        for name, check in [
            ("repository", actual_container.repository.health),
            ("object_store", actual_container.object_store.health),
            ("job_broker", actual_container.publisher.health),
        ]:
            try:
                dependency_status[name] = check()
            except Exception:
                dependency_status[name] = False
        candidate_valid = actual_container.service.service_pin is None
        release_ready = False
        try:
            agent_metadata = actual_container.agent.metadata()
            if actual_container.service.service_pin is not None:
                if actual_container.service.release_pin is None:
                    actual_container.service.service_pin.validate_unreleased_metadata(
                        agent_metadata
                    )
                    candidate_valid = True
                else:
                    if (
                        actual_container.service.release_pin.service_pin()
                        != actual_container.service.service_pin
                    ):
                        raise ReleaseConfigurationError(
                            "Agent service and release pins do not match"
                        )
                    actual_container.service.release_pin.validate_metadata(agent_metadata)
                    candidate_valid = True
                    release_ready = True
            else:
                release_ready = True
            actual_container.service.assert_auto_completion_ready()
            agent_ready = candidate_valid and release_ready
        except Exception:
            agent_metadata = {"status": "unavailable"}
            agent_ready = False
        if not auth_ready or not agent_ready or not all(dependency_status.values()):
            raise HTTPException(status_code=503, detail="required dependency is not ready")
        return {
            "status": "ready",
            "auth": "oidc",
            "text_only": True,
            "auto_completion_enabled": actual_container.service.auto_completion_enabled,
            "agent": agent_metadata,
            "dependencies": dependency_status,
        }

    def validate_uat_capability_attestation(
        payload: dict[str, Any],
    ) -> AgentUatCapabilityAttestationV1:
        if not actual_container.settings.enhanced_agent_experience:
            raise ReleaseConfigurationError("enhanced Agent experience is disabled")
        try:
            validate_agent_experience_payload(
                "agent-uat-capability-v1",
                payload["uat_capability_attestation"],
            )
            capability = AgentUatCapabilityAttestationV1.model_validate(
                payload["uat_capability_attestation"]
            )
        except (KeyError, TypeError, ValueError):
            raise ReleaseConfigurationError("Agent UAT capability is invalid") from None
        contract_lock = validate_agent_experience_contract_bundle().lock
        runtime_binding = actual_container.local_uat_runtime_binding
        if runtime_binding is not None:
            runtime_binding.require_active()
        capability_now = datetime.now(UTC)
        common_identity_differs = (
            payload.get("uat_capability_sha256") != capability.capability_sha256
            or payload.get("uat_capability_sha256")
            != actual_container.settings.uat_expected_capability_sha256
            or (
                runtime_binding is not None
                and payload.get("uat_capability_sha256") != runtime_binding.capability_sha256
            )
            or not isinstance(payload.get("uat_capability_file_sha256"), str)
            or any(
                character not in "0123456789abcdef"
                for character in payload["uat_capability_file_sha256"]
            )
            or len(payload["uat_capability_file_sha256"]) != 64
            or payload.get("uat_capability_file_sha256")
            != actual_container.settings.uat_expected_capability_file_sha256
            or (
                runtime_binding is not None
                and payload.get("uat_capability_file_sha256")
                != runtime_binding.capability_file_sha256
            )
            or payload.get("uat_capability_expires_at") != capability.expires_at.isoformat()
            or payload.get("agent_commit") != capability.agent_commit
            or capability.expires_at <= capability_now
            or capability.issued_at > capability_now + timedelta(minutes=5)
            or capability.expires_at > capability_now + timedelta(hours=24, minutes=5)
            or capability.adaptive_active != actual_container.settings.uat_adaptive_active
            or capability.evidence_profile != actual_container.settings.uat_evidence_profile
            or capability.evidence_runtime_v3_sha256
            != actual_container.settings.uat_evidence_runtime_v3_sha256
            or (
                runtime_binding is not None
                and capability.evidence_runtime_v3_sha256
                != runtime_binding.evidence_runtime_v3_sha256
            )
        )
        binding_identity_differs = runtime_binding is not None and (
            capability.agent_commit != runtime_binding.agent.commit
            or capability.prompt_bundle_sha256 != runtime_binding.agent.prompt_bundle_sha256
            or capability.schema_bundle_sha256 != runtime_binding.agent.schema_bundle_sha256
            or capability.tool_registry_id != runtime_binding.agent.tool_registry_id
            or capability.tool_registry_sha256 != runtime_binding.agent.tool_registry_sha256
        )
        candidate_lock_differs = (
            runtime_binding is None or runtime_binding.mode == "candidate"
        ) and (
            capability.prompt_bundle_sha256 != contract_lock.experience_prompt_bundle_sha256
            or capability.schema_bundle_sha256 != contract_lock.experience_schema_bundle_sha256
            or capability.tool_registry_id != contract_lock.uat_tool_registry_id
            or capability.tool_registry_sha256 != contract_lock.uat_tool_registry_sha256
            or (
                runtime_binding is not None
                and runtime_binding.agent.meaning_catalog_sha256
                != expected_meaning_catalog_sha256()
            )
        )
        if common_identity_differs or binding_identity_differs or candidate_lock_differs:
            raise ReleaseConfigurationError("Agent UAT capability identity differs")
        return capability

    def validate_enhanced_capability(
        payload: dict[str, Any],
    ) -> AgentUatCapabilityAttestationV1:
        expected_keys = {
            "service_api_version": "internal-agent-service/v3",
            "enhanced_experience_enabled": True,
            "uat_adaptive_active": actual_container.settings.uat_adaptive_active,
            "auto_completion_enabled": False,
            "uat_capability_status": "valid",
        }
        schemas = payload.get("schemas")
        contract_lock = validate_agent_experience_contract_bundle().lock
        if (
            any(payload.get(key) != value for key, value in expected_keys.items())
            or payload.get("meaning_catalog_sha256") != expected_meaning_catalog_sha256()
            or (
                actual_container.local_uat_runtime_binding is not None
                and payload.get("meaning_catalog_sha256")
                != actual_container.local_uat_runtime_binding.agent.meaning_catalog_sha256
            )
            or not isinstance(schemas, dict)
            or schemas.get("service_openapi_sha256") != contract_lock.provider_openapi_sha256
        ):
            raise ReleaseConfigurationError("Agent enhanced-experience mode differs")
        return validate_uat_capability_attestation(payload)

    def validated_local_uat_agent_metadata() -> tuple[
        dict[str, Any], EvidenceRuntimeReleaseIdentityV3
    ]:
        agent_metadata = actual_container.agent.metadata()
        uat_identity = agent_metadata.get("uat")
        transport_limits = agent_metadata.get("transport_limits")
        agentic_limits = agent_metadata.get("agentic_limits")
        model_identity = agent_metadata.get("model")
        evidence_release = agent_metadata.get("evidence_runtime_release_v3")
        evidence_profile_metadata = (
            uat_identity.get("evidence_profile") if isinstance(uat_identity, dict) else None
        )
        expected_planner_mode = {
            "shadow": "shadow",
            "diagnostic_active": "active",
        }[actual_container.settings.uat_planner_mode]
        expected_execution_mode = {
            "shadow": "shadow",
            "diagnostic_active": "uat_active",
        }[actual_container.settings.uat_planner_mode]
        if (
            agent_metadata.get("status") != "ready"
            or agent_metadata.get("service_api_version") != "internal-agent-service/v2"
            or agent_metadata.get("service_mode") != "local_user_uat_diagnostic"
            or agent_metadata.get("assurance_level") != "diagnostic"
            or not isinstance(transport_limits, dict)
            or transport_limits.get("text_only") is not True
            or not isinstance(agentic_limits, dict)
            or agentic_limits.get("planner_mode") != expected_planner_mode
            or agentic_limits.get("effective_execution_mode") != expected_execution_mode
            or not isinstance(model_identity, dict)
            or not isinstance(model_identity.get("id"), str)
            or not model_identity["id"]
            or not isinstance(uat_identity, dict)
            or uat_identity.get("diagnostic_only") is not True
            or uat_identity.get("mock_fallback_allowed") is not False
            or uat_identity.get("auto_completion_allowed") is not False
            or not isinstance(evidence_profile_metadata, dict)
            or evidence_profile_metadata.get("direct_database_access") is not False
            or evidence_profile_metadata.get("mock_fallback_allowed") is not False
            or evidence_profile_metadata.get("release_eligible") is not False
            or not isinstance(evidence_release, dict)
            or set(evidence_release) != {"release_state", "identity"}
            or evidence_release.get("release_state") != "diagnostic"
        ):
            raise ReleaseConfigurationError("Agent is not local UAT diagnostic-ready")
        expected_evidence_mode = actual_container.settings.uat_evidence_profile
        expected_policy_mode = actual_container.settings.uat_evidence_policy_mode
        if (
            uat_identity.get("evidence_mode") != expected_evidence_mode
            or evidence_profile_metadata.get("profile_id") != expected_evidence_mode
        ):
            raise ReleaseConfigurationError("Agent UAT Evidence profile differs")
        if uat_identity.get("evidence_policy_mode") != expected_policy_mode:
            raise ReleaseConfigurationError("Agent UAT Evidence policy mode differs")
        try:
            evidence_identity = EvidenceRuntimeReleaseIdentityV3.model_validate(
                evidence_release["identity"]
            )
        except (KeyError, TypeError, ValueError):
            raise ReleaseConfigurationError("Agent UAT Evidence identity is invalid") from None
        if uat_identity.get("evidence_runtime_identity") != evidence_identity.model_dump(
            mode="json"
        ):
            raise ReleaseConfigurationError("Agent UAT Evidence identity is not consistently bound")
        _validate_uat_evidence_policy_identity(expected_evidence_mode, evidence_identity)
        scenario_eval = expected_evidence_mode == "preserved-3law-v3-scenario-eval"
        resolver = actual_container.service.requirement_policy_resolver
        if scenario_eval:
            if (
                actual_container.settings.uat_assertion_trust_store_sha256 is None
                or actual_container.settings.uat_evidence_runtime_v3_sha256 is None
                or uat_identity.get("assertion_fixture_mode") != "LOOPBACK_TEST_ONLY_HUMAN_APPROVER"
                or uat_identity.get("trusted_assertion_verifier_configured") is not True
                or uat_identity.get("assertion_trust_store_sha256")
                != actual_container.settings.uat_assertion_trust_store_sha256
                or evidence_identity.runtime_release_v3_sha256
                != actual_container.settings.uat_evidence_runtime_v3_sha256
                or not actual_container.service.assertion_signer.ready
                or actual_container.service.assertion_signer.trust_store_sha256
                != actual_container.settings.uat_assertion_trust_store_sha256
                or not isinstance(resolver, LocalUatRequirementPolicyIdentityResolver)
                or resolver.evidence_runtime_v3_sha256
                != evidence_identity.runtime_release_v3_sha256
                or resolver.policy_release != evidence_identity.requirement_policy_release
            ):
                raise ReleaseConfigurationError("Agent UAT assertion fixture identity differs")
        elif (
            uat_identity.get("assertion_fixture_mode") != "DISABLED"
            or uat_identity.get("trusted_assertion_verifier_configured") is not False
            or uat_identity.get("assertion_trust_store_sha256") is not None
            or actual_container.service.assertion_signer.ready
            or actual_container.service.requirement_policy_resolver.ready
        ):
            raise ReleaseConfigurationError(
                "Agent UAT assertion fixture is forbidden for this profile"
            )
        runtime_binding = actual_container.local_uat_runtime_binding
        if runtime_binding is not None:
            runtime_binding.require_active()
            bound_agent_metadata = agent_metadata.get("agent")
            bound_runtime_release = agent_metadata.get("runtime_release")
            if not isinstance(bound_agent_metadata, dict):
                raise ReleaseConfigurationError("Agent UAT build metadata is invalid")
            try:
                bound_runtime_identity = ReleaseIdentityV3.model_validate(bound_runtime_release)
            except (TypeError, ValueError):
                raise ReleaseConfigurationError("Agent UAT runtime metadata is invalid") from None
            if (
                bound_agent_metadata.get("build_sha") != runtime_binding.agent.commit
                or bound_agent_metadata.get("source_snapshot_sha256")
                != runtime_binding.agent.working_tree_sha256
                or bound_agent_metadata.get("wheel_sha256") != runtime_binding.agent.wheel_sha256
                or not runtime_binding.matches_service_metadata_runtime(bound_runtime_identity)
            ):
                raise ReleaseConfigurationError("Agent UAT metadata differs from runtime binding")
        if actual_container.service.service_pin is not None:
            if runtime_binding is not None and runtime_binding.mode == "development":
                actual_container.service.service_pin.validate_local_uat_development_metadata(
                    agent_metadata,
                    binding=runtime_binding,
                )
            else:
                actual_container.service.service_pin.validate_local_uat_metadata(agent_metadata)
        if actual_container.settings.enhanced_agent_experience:
            experience_metadata = actual_container.agent.experience_metadata()
            capability = validate_enhanced_capability(experience_metadata)
            if (
                not isinstance(experience_metadata.get("agentic_limits"), dict)
                or experience_metadata["agentic_limits"].get("effective_execution_mode")
                != "uat_active"
                or experience_metadata["agentic_limits"].get("planner_mode") != "active"
                or experience_metadata["agentic_limits"].get(
                    "active_execution_authorized_by_release"
                )
                is not False
                or capability.auto_completion_enabled is not False
            ):
                raise ReleaseConfigurationError("Agent enhanced-experience limits are invalid")
        return agent_metadata, evidence_identity

    def local_uat_evidence_scope() -> Literal[
        "TEST_ONLY_V3",
        "ACTUAL_3_LAW_RAG_3_OF_58",
        "ACTUAL_3_LAW_RAG_SCENARIO_EVAL_3_OF_58",
        "UNKNOWN",
    ]:
        profile = actual_container.settings.uat_evidence_profile
        if profile == "synthetic-v3":
            return "TEST_ONLY_V3"
        if profile == "preserved-3law-v3-diagnostic":
            return "ACTUAL_3_LAW_RAG_3_OF_58"
        if profile == "preserved-3law-v3-scenario-eval":
            return "ACTUAL_3_LAW_RAG_SCENARIO_EVAL_3_OF_58"
        return "UNKNOWN"

    def local_uat_evidence_dataset() -> UatEvidenceDatasetResponse:
        return UatEvidenceDatasetResponse(
            data_source=actual_container.settings.uat_evidence_data_source,
            expected_production_law_count=(
                actual_container.settings.uat_evidence_expected_production_law_count
            ),
            observed_law_count=actual_container.settings.uat_evidence_observed_law_count,
            observed_article_count=(actual_container.settings.uat_evidence_observed_article_count),
            store_counts=UatEvidenceStoreCountsResponse(
                postgresql_article_count=(
                    actual_container.settings.uat_evidence_observed_article_count
                ),
                qdrant_point_count=actual_container.settings.uat_evidence_qdrant_point_count,
                neo4j_article_count=(actual_container.settings.uat_evidence_neo4j_article_count),
            ),
            rag_quality_evaluated=(actual_container.settings.uat_evidence_rag_quality_evaluated),
        )

    @app.get("/health/uat-ready", include_in_schema=False)
    def health_uat_ready() -> dict[str, Any]:
        if actual_container.settings.environment != "local_uat":
            raise HTTPException(status_code=404, detail="local UAT endpoint is unavailable")
        verifier = actual_container.token_verifier
        auth_ready = isinstance(verifier, OidcJwtVerifier) and verifier.health()
        dependency_status: dict[str, bool] = {}
        for name, check in [
            ("repository", actual_container.repository.health),
            ("object_store", actual_container.object_store.health),
            ("job_broker", actual_container.publisher.health),
        ]:
            try:
                dependency_status[name] = check()
            except Exception:
                dependency_status[name] = False
        try:
            agent_metadata, evidence_identity = validated_local_uat_agent_metadata()
            agent_uat_readiness = actual_container.agent.uat_readiness()
            if actual_container.settings.enhanced_agent_experience:
                readiness_capability = validate_uat_capability_attestation(agent_uat_readiness)
                metadata_capability = validate_enhanced_capability(
                    actual_container.agent.experience_metadata()
                )
                if readiness_capability != metadata_capability:
                    raise ReleaseConfigurationError(
                        "Agent metadata/readiness capability identity drifted"
                    )
            if (
                agent_uat_readiness.get("status") != "uat-ready"
                or agent_uat_readiness.get("diagnostic_only") is not True
                or agent_uat_readiness.get("text_only") is not True
                or agent_uat_readiness.get("mock_fallback_allowed") is not False
                or agent_uat_readiness.get("auto_completion_allowed") is not False
            ):
                raise ReleaseConfigurationError("Agent is not local UAT diagnostic-ready")
            expected_evidence_mode = actual_container.settings.uat_evidence_profile
            expected_policy_mode = actual_container.settings.uat_evidence_policy_mode
            if agent_uat_readiness.get("evidence_mode") != expected_evidence_mode:
                raise ReleaseConfigurationError("Agent UAT Evidence profile differs")
            if agent_uat_readiness.get("evidence_policy_mode") != expected_policy_mode:
                raise ReleaseConfigurationError("Agent UAT Evidence policy mode differs")
            scenario_eval = expected_evidence_mode == "preserved-3law-v3-scenario-eval"
            if scenario_eval:
                if (
                    agent_uat_readiness.get("assertion_fixture_mode")
                    != "LOOPBACK_TEST_ONLY_HUMAN_APPROVER"
                    or agent_uat_readiness.get("trusted_assertion_verifier_configured") is not True
                    or agent_uat_readiness.get("assertion_trust_store_sha256")
                    != actual_container.settings.uat_assertion_trust_store_sha256
                ):
                    raise ReleaseConfigurationError(
                        "Agent UAT assertion readiness identity differs"
                    )
            elif (
                agent_uat_readiness.get("assertion_fixture_mode") != "DISABLED"
                or agent_uat_readiness.get("trusted_assertion_verifier_configured") is not False
                or agent_uat_readiness.get("assertion_trust_store_sha256") is not None
            ):
                raise ReleaseConfigurationError("Agent UAT assertion readiness is invalid")
            agent_ready = True
        except Exception:
            agent_metadata = {"status": "unavailable"}
            agent_uat_readiness = {"status": "unavailable"}
            agent_ready = False
        if (
            not auth_ready
            or not agent_ready
            or actual_container.service.auto_completion_enabled
            or not all(dependency_status.values())
        ):
            raise HTTPException(status_code=503, detail="local UAT dependency is not ready")
        return {
            "status": "ready",
            "mode": "local_uat",
            "assurance_ceiling": "diagnostic",
            "text_only": True,
            "auto_completion_enabled": False,
            "production_release": False,
            "evidence_profile": actual_container.settings.uat_evidence_profile,
            "evidence_scope": local_uat_evidence_scope(),
            "evidence_scope_label": local_uat_evidence_scope(),
            "evidence_data_source": actual_container.settings.uat_evidence_data_source,
            "evidence_policy_mode": actual_container.settings.uat_evidence_policy_mode,
            "evidence_dataset": local_uat_evidence_dataset().model_dump(mode="json"),
            "evidence_runtime_v3_sha256": evidence_identity.runtime_release_v3_sha256,
            "agent_status": agent_metadata["status"],
            "agent_uat_status": agent_uat_readiness["status"],
            "enhanced_agent_experience": (actual_container.settings.enhanced_agent_experience),
            "uat_adaptive_active": actual_container.settings.uat_adaptive_active,
            "dependencies": dependency_status,
        }

    @app.get("/api/v1/uat/metadata", response_model=UatMetadataResponse)
    def uat_metadata() -> UatMetadataResponse:
        if actual_container.settings.environment != "local_uat":
            raise HTTPException(status_code=404, detail="local UAT metadata is unavailable")
        try:
            _agent_metadata, evidence_identity = validated_local_uat_agent_metadata()
        except Exception:
            raise HTTPException(
                status_code=503,
                detail="local UAT Agent metadata is invalid or unavailable",
            ) from None
        return UatMetadataResponse(
            evidence_profile=actual_container.settings.uat_evidence_profile,
            evidence_scope=local_uat_evidence_scope(),
            evidence_scope_label=local_uat_evidence_scope(),
            evidence_data_source=actual_container.settings.uat_evidence_data_source,
            evidence_policy_mode=actual_container.settings.uat_evidence_policy_mode,
            evidence_dataset=local_uat_evidence_dataset(),
            supported_review_as_of=evidence_identity.base_runtime_release.supported_review_as_of,
            planner_mode=actual_container.settings.uat_planner_mode,
        )

    @app.get("/api/v2/uat/metadata", response_model=UatMetadataV2Response)
    def uat_metadata_v2() -> UatMetadataV2Response:
        if actual_container.settings.environment != "local_uat":
            raise HTTPException(status_code=404, detail="local UAT metadata is unavailable")
        try:
            _agent_metadata, evidence_identity = validated_local_uat_agent_metadata()
        except Exception:
            raise HTTPException(
                status_code=503,
                detail="local UAT Agent metadata is invalid or unavailable",
            ) from None
        return UatMetadataV2Response(
            evidence_profile=actual_container.settings.uat_evidence_profile,
            evidence_scope=local_uat_evidence_scope(),
            evidence_scope_label=local_uat_evidence_scope(),
            evidence_data_source=actual_container.settings.uat_evidence_data_source,
            evidence_policy_mode=actual_container.settings.uat_evidence_policy_mode,
            evidence_dataset=local_uat_evidence_dataset(),
            supported_review_as_of=evidence_identity.base_runtime_release.supported_review_as_of,
            planner_mode=actual_container.settings.uat_planner_mode,
            enhanced_agent_experience=(actual_container.settings.enhanced_agent_experience),
            uat_adaptive_active=actual_container.settings.uat_adaptive_active,
            agent_experience_api_version=(
                "3.0" if actual_container.settings.enhanced_agent_experience else "disabled"
            ),
            assistant_api_version=(
                "2.0" if actual_container.settings.enhanced_agent_experience else "disabled"
            ),
            actual_stage_streaming=(actual_container.settings.enhanced_agent_experience),
        )

    @app.get("/api/v1/auth/config", response_model=AuthConfigResponse)
    def auth_config() -> AuthConfigResponse:
        settings_value = actual_container.settings
        required = {
            "issuer": settings_value.oidc_issuer,
            "audience": settings_value.oidc_audience,
            "client_id": settings_value.oidc_client_id,
            "authorization_endpoint": settings_value.oidc_authorization_endpoint,
            "token_endpoint": settings_value.oidc_token_endpoint,
        }
        if any(value is None for value in required.values()):
            raise HTTPException(status_code=503, detail="OIDC client configuration unavailable")
        return AuthConfigResponse(
            **required,  # type: ignore[arg-type]
            end_session_endpoint=settings_value.oidc_end_session_endpoint,
        )

    @app.get("/api/v1/session", response_model=SessionResponse)
    def session(principal: Principal = Depends(principal_dependency)) -> SessionResponse:
        return SessionResponse(
            subject=principal.subject,
            org_id=principal.org_id,
            roles=sorted(principal.roles, key=lambda role: role.value),
        )

    writers = require_roles(Role.reviewer, Role.approver, Role.admin)
    readers = require_roles(Role.reviewer, Role.approver, Role.auditor, Role.admin)
    approvers = require_roles(Role.approver, Role.admin)
    chat_users = require_roles(Role.reviewer, Role.approver, Role.admin)

    @app.post("/api/v1/cases", response_model=CaseRecord, status_code=201)
    def create_case_endpoint(
        body: CaseCreate,
        principal: Principal = Depends(writers),
        service: WorkbenchService = Depends(_service),
    ) -> CaseRecord:
        return service.create_case(principal, body)

    @app.get("/api/v1/cases", response_model=CaseListResponse)
    def list_cases_endpoint(
        principal: Principal = Depends(readers),
        service: WorkbenchService = Depends(_service),
    ) -> CaseListResponse:
        return CaseListResponse(items=service.list_cases(principal))

    @app.get("/api/v1/cases/{case_id}", response_model=CaseRecord)
    def get_case_endpoint(
        case_id: str,
        principal: Principal = Depends(readers),
        service: WorkbenchService = Depends(_service),
    ) -> CaseRecord:
        return service.get_case(principal, case_id)

    @app.post(
        "/api/v1/cases/{case_id}/documents", response_model=DocumentViewResponse, status_code=201
    )
    def add_document_endpoint(
        case_id: str,
        body: TextDocumentCreate,
        principal: Principal = Depends(writers),
        service: WorkbenchService = Depends(_service),
    ) -> DocumentViewResponse:
        _case, record = service.add_document(principal, case_id, body)
        return DocumentViewResponse(
            record=DocumentViewResponse.PublicDocumentRecord.model_validate(
                record.model_dump(mode="json")
            ),
            content_text=body.content_text,
        )

    @app.get("/api/v1/cases/{case_id}/documents", response_model=DocumentListResponse)
    def list_documents_endpoint(
        case_id: str,
        principal: Principal = Depends(readers),
        service: WorkbenchService = Depends(_service),
    ) -> DocumentListResponse:
        return DocumentListResponse(
            items=[
                DocumentViewResponse(
                    record=DocumentViewResponse.PublicDocumentRecord.model_validate(
                        item.model_dump(mode="json")
                    ),
                    content_text=None,
                )
                for item in service.list_document_records(principal, case_id)
            ]
        )

    @app.get(
        "/api/v1/cases/{case_id}/documents/{document_id}/content",
        response_model=DocumentViewResponse,
    )
    def get_document_content_endpoint(
        case_id: str,
        document_id: str,
        principal: Principal = Depends(readers),
        service: WorkbenchService = Depends(_service),
    ) -> DocumentViewResponse:
        view = service.get_document_view(principal, case_id, document_id)
        return DocumentViewResponse(
            record=DocumentViewResponse.PublicDocumentRecord.model_validate(
                view.record.model_dump(mode="json")
            ),
            content_text=view.content_text,
        )

    @app.post(
        "/api/v1/cases/{case_id}/supporting-documents",
        response_model=SupportingDocumentRecord,
        status_code=201,
    )
    def add_supporting_document_endpoint(
        case_id: str,
        body: SupportingDocumentCreate,
        principal: Principal = Depends(writers),
        service: WorkbenchService = Depends(_service),
    ) -> SupportingDocumentRecord:
        return service.add_supporting_document(principal, case_id, body)

    @app.get(
        "/api/v1/cases/{case_id}/supporting-documents",
        response_model=SupportingDocumentListResponse,
    )
    def list_supporting_documents_endpoint(
        case_id: str,
        principal: Principal = Depends(readers),
        service: WorkbenchService = Depends(_service),
    ) -> SupportingDocumentListResponse:
        return SupportingDocumentListResponse(
            items=service.list_supporting_documents(principal, case_id)
        )

    @app.post(
        "/api/v1/cases/{case_id}/supporting-documents/{document_id}/assertions",
        response_model=SupportingDocumentRecord,
        status_code=201,
    )
    def assert_supporting_document_endpoint(
        case_id: str,
        document_id: str,
        body: VerificationAssertionCreate,
        principal: Principal = Depends(approvers),
        service: WorkbenchService = Depends(_service),
    ) -> SupportingDocumentRecord:
        return service.assert_supporting_document(principal, case_id, document_id, body)

    @app.post("/api/v1/cases/{case_id}/reviews", response_model=JobResponse, status_code=202)
    def enqueue_review_endpoint(
        case_id: str,
        body: ReviewEnqueueRequest,
        idempotency_key: IdempotencyKey,
        principal: Principal = Depends(writers),
        service: WorkbenchService = Depends(_service),
    ) -> JobResponse:
        job, created = service.enqueue_review(principal, case_id, body, idempotency_key)
        return JobResponse(
            job=JobView.model_validate(job.model_dump(mode="json")),
            idempotency_reused=not created,
        )

    @app.get("/api/v1/jobs/{job_id}", response_model=JobView)
    def get_job_endpoint(
        job_id: str,
        principal: Principal = Depends(readers),
        service: WorkbenchService = Depends(_service),
    ) -> JobView:
        job = service.get_job(principal, job_id)
        return JobView.model_validate(job.model_dump(mode="json"))

    @app.post("/api/v1/jobs/{job_id}/reconciliation", response_model=JobView)
    def reconcile_delivery_uncertain_endpoint(
        job_id: str,
        body: DeliveryReconciliationCreate,
        principal: Principal = Depends(approvers),
        service: WorkbenchService = Depends(_service),
    ) -> JobView:
        job = service.reconcile_delivery_uncertain(principal, job_id, body)
        return JobView.model_validate(job.model_dump(mode="json"))

    @app.get("/api/v1/jobs/{job_id}/events", response_model=JobEventsResponse)
    def get_job_events_endpoint(
        job_id: str,
        principal: Principal = Depends(readers),
        service: WorkbenchService = Depends(_service),
    ) -> JobEventsResponse:
        service.get_job(principal, job_id)
        return JobEventsResponse(
            items=service.repository.list_stage_events(principal.org_id, job_id)
        )

    @app.get("/api/v1/jobs/{job_id}/events/stream")
    def stream_job_events_endpoint(
        job_id: str,
        last_event_id: Annotated[str | None, Header(alias="Last-Event-ID")] = None,
        principal: Principal = Depends(readers),
        service: WorkbenchService = Depends(_service),
    ) -> StreamingResponse:
        service.get_job(principal, job_id)

        def generate():
            try:
                next_sequence = max(int(last_event_id or "0") + 1, 1)
            except ValueError:
                next_sequence = 1
            deadline = time.monotonic() + 300
            heartbeat_at = time.monotonic() + 15
            while time.monotonic() < deadline:
                events = service.repository.list_stage_events(principal.org_id, job_id)
                for event in events:
                    if event.sequence < next_sequence:
                        continue
                    yield (
                        f"id: {event.sequence}\n"
                        "event: review-stage\n"
                        f"data: {json.dumps(event.model_dump(mode='json'), separators=(',', ':'))}\n\n"
                    )
                    next_sequence = event.sequence + 1
                job = service.repository.get_job(principal.org_id, job_id)
                if job is None or (
                    job.status.value
                    in {
                        "succeeded",
                        "failed",
                        "delivery_uncertain",
                        "response_received_rejected",
                        "response_persistence_uncertain",
                    }
                    and all(event.sequence < next_sequence for event in events)
                ):
                    return
                if time.monotonic() >= heartbeat_at:
                    yield ": keep-alive\n\n"
                    heartbeat_at = time.monotonic() + 15
                time.sleep(0.5)

        return StreamingResponse(
            generate(),
            media_type="text/event-stream",
            headers={
                "Cache-Control": "no-cache, no-store",
                "X-Accel-Buffering": "no",
            },
        )

    @app.get("/api/v1/cases/{case_id}/review-runs", response_model=ReviewRunListResponse)
    def list_runs_endpoint(
        case_id: str,
        principal: Principal = Depends(readers),
        service: WorkbenchService = Depends(_service),
    ) -> ReviewRunListResponse:
        service.get_case(principal, case_id)
        return ReviewRunListResponse(items=service.repository.list_runs(principal.org_id, case_id))

    @app.get("/api/v1/review-runs/{review_run_id}", response_model=ReviewRunRecord)
    def get_run_endpoint(
        review_run_id: str,
        principal: Principal = Depends(readers),
        service: WorkbenchService = Depends(_service),
    ) -> ReviewRunRecord:
        return service.get_run(principal, review_run_id)

    @app.get(
        "/api/v1/uat/review-runs/{review_run_id}/evidence-trace",
        response_model=UatEvidenceTraceResponse,
    )
    def get_uat_evidence_trace_endpoint(
        review_run_id: str,
        principal: Principal = Depends(readers),
        service: WorkbenchService = Depends(_service),
    ) -> UatEvidenceTraceResponse:
        reader = actual_container.uat_evidence_trace_reader
        if (
            actual_container.settings.environment != "local_uat"
            or actual_container.settings.uat_evidence_profile != "preserved-3law-v3-scenario-eval"
            or reader is None
        ):
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND)
        run = service.get_run(principal, review_run_id)
        provenance = run.result_detail.get("evidence_provenance")
        request_id = provenance.get("request_id") if isinstance(provenance, dict) else None
        knowledge_snapshot_id = run.result_detail.get("knowledge_snapshot_id")
        if (
            not isinstance(request_id, str)
            or not isinstance(knowledge_snapshot_id, str)
            or run.release_identity.get("evidence_snapshot_id") != knowledge_snapshot_id
        ):
            raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE)
        try:
            return reader.projection(
                request_id=request_id,
                knowledge_snapshot_id=knowledge_snapshot_id,
            )
        except UatEvidenceTraceError:
            raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE) from None

    @app.post(
        "/api/v1/review-runs/{review_run_id}/correction-proposals",
        response_model=CorrectionProposalRecord,
        status_code=201,
    )
    def create_correction_proposal_endpoint(
        review_run_id: str,
        body: CorrectionProposalCreate,
        idempotency_key: IdempotencyKey,
        principal: Principal = Depends(writers),
        service: WorkbenchService = Depends(_service),
    ) -> CorrectionProposalRecord:
        return service.create_correction_proposal(principal, review_run_id, body, idempotency_key)

    @app.get(
        "/api/v1/review-runs/{review_run_id}/correction-proposals",
        response_model=CorrectionProposalListResponse,
    )
    def list_correction_proposals_endpoint(
        review_run_id: str,
        principal: Principal = Depends(readers),
        service: WorkbenchService = Depends(_service),
    ) -> CorrectionProposalListResponse:
        return CorrectionProposalListResponse(
            items=service.list_correction_proposals(principal, review_run_id)
        )

    @app.post(
        "/api/v1/review-runs/{review_run_id}/revision-proposals",
        response_model=CorrectionProposalRecord,
        status_code=201,
    )
    def create_revision_proposal_endpoint(
        review_run_id: str,
        body: RevisionProposalCreate,
        idempotency_key: IdempotencyKey,
        principal: Principal = Depends(writers),
        service: WorkbenchService = Depends(_service),
    ) -> CorrectionProposalRecord:
        return service.create_revision_proposal(principal, review_run_id, body, idempotency_key)

    @app.get(
        "/api/v1/review-runs/{review_run_id}/revision-proposals",
        response_model=CorrectionProposalListResponse,
    )
    def list_revision_proposals_endpoint(
        review_run_id: str,
        principal: Principal = Depends(readers),
        service: WorkbenchService = Depends(_service),
    ) -> CorrectionProposalListResponse:
        return CorrectionProposalListResponse(
            items=service.list_correction_proposals(principal, review_run_id)
        )

    @app.post(
        "/api/v1/correction-proposals/{proposal_id}/decisions",
        response_model=CorrectionProposalDecisionResponse,
        status_code=201,
    )
    def decide_correction_proposal_endpoint(
        proposal_id: str,
        body: CorrectionProposalDecisionCreate,
        idempotency_key: IdempotencyKey,
        principal: Principal = Depends(approvers),
        service: WorkbenchService = Depends(_service),
    ) -> CorrectionProposalDecisionResponse:
        proposal, decision, job = service.decide_correction_proposal(
            principal, proposal_id, body, idempotency_key
        )
        return CorrectionProposalDecisionResponse(
            proposal=proposal,
            decision=decision,
            job=(JobView.model_validate(job.model_dump(mode="json")) if job else None),
        )

    @app.post(
        "/api/v1/revision-proposals/{proposal_id}/decisions",
        response_model=CorrectionProposalDecisionResponse,
        status_code=201,
    )
    def decide_revision_proposal_endpoint(
        proposal_id: str,
        body: CorrectionProposalDecisionCreate,
        idempotency_key: IdempotencyKey,
        principal: Principal = Depends(approvers),
        service: WorkbenchService = Depends(_service),
    ) -> CorrectionProposalDecisionResponse:
        proposal, decision, job = service.decide_correction_proposal(
            principal, proposal_id, body, idempotency_key
        )
        return CorrectionProposalDecisionResponse(
            proposal=proposal,
            decision=decision,
            job=(JobView.model_validate(job.model_dump(mode="json")) if job else None),
        )

    @app.get("/api/v1/review-runs/{review_run_id}/decisions", response_model=DecisionListResponse)
    def list_decisions_endpoint(
        review_run_id: str,
        principal: Principal = Depends(readers),
        service: WorkbenchService = Depends(_service),
    ) -> DecisionListResponse:
        service.get_run(principal, review_run_id)
        return DecisionListResponse(
            items=service.repository.list_decisions(principal.org_id, review_run_id)
        )

    @app.post(
        "/api/v1/review-runs/{review_run_id}/answers", response_model=ChatRecord, status_code=201
    )
    def answer_endpoint(
        review_run_id: str,
        body: GroundedQuestion,
        idempotency_key: IdempotencyKey,
        principal: Principal = Depends(chat_users),
        service: WorkbenchService = Depends(_service),
    ) -> ChatRecord:
        return service.answer(principal, review_run_id, body, idempotency_key)

    @app.get("/api/v1/review-runs/{review_run_id}/messages", response_model=ChatListResponse)
    def list_chat_endpoint(
        review_run_id: str,
        principal: Principal = Depends(readers),
        service: WorkbenchService = Depends(_service),
    ) -> ChatListResponse:
        service.get_run(principal, review_run_id)
        return ChatListResponse(items=service.repository.list_chat(principal.org_id, review_run_id))

    @app.post(
        "/api/v2/cases/{case_id}/reviews",
        response_model=JobExperienceResponse,
        status_code=202,
    )
    def enqueue_experience_review_endpoint(
        case_id: str,
        body: ReviewEnqueueRequest,
        idempotency_key: IdempotencyKey,
        principal: Principal = Depends(writers),
        service: WorkbenchService = Depends(_service),
    ) -> JobExperienceResponse:
        service.require_enhanced_experience()
        job, created = service.enqueue_review(
            principal,
            case_id,
            body,
            idempotency_key,
            experience_api_version="3.0",
            evidence_profile=service.evidence_profile,
        )
        return JobExperienceResponse(
            job=service.get_job_experience(principal, job.job_id),
            idempotency_reused=not created,
        )

    @app.get("/api/v2/jobs/{job_id}", response_model=JobExperienceView)
    def get_experience_job_endpoint(
        job_id: str,
        principal: Principal = Depends(readers),
        service: WorkbenchService = Depends(_service),
    ) -> JobExperienceView:
        return service.get_job_experience(principal, job_id)

    @app.get("/api/v2/jobs/{job_id}/events/stream")
    def stream_experience_job_events_endpoint(
        job_id: str,
        last_event_id: Annotated[str | None, Header(alias="Last-Event-ID")] = None,
        principal: Principal = Depends(readers),
        service: WorkbenchService = Depends(_service),
    ) -> StreamingResponse:
        service.get_job_experience(principal, job_id)

        def generate():
            try:
                next_sequence = max(int(last_event_id or "0") + 1, 1)
            except ValueError:
                next_sequence = 1
            deadline = time.monotonic() + 600
            heartbeat_at = time.monotonic() + 15
            while time.monotonic() < deadline:
                events = service.repository.list_stage_events(principal.org_id, job_id)
                for event in events:
                    if event.sequence < next_sequence:
                        continue
                    yield (
                        f"id: {event.sequence}\n"
                        "event: review-stage\n"
                        f"data: {json.dumps(event.model_dump(mode='json'), separators=(',', ':'))}\n\n"
                    )
                    next_sequence = event.sequence + 1
                job = service.repository.get_job(principal.org_id, job_id)
                if job is None or (
                    job.status
                    in {
                        JobStatus.succeeded,
                        JobStatus.failed,
                        JobStatus.delivery_uncertain,
                        JobStatus.response_received_rejected,
                        JobStatus.response_persistence_uncertain,
                    }
                    and all(event.sequence < next_sequence for event in events)
                ):
                    return
                if time.monotonic() >= heartbeat_at:
                    yield ": keep-alive\n\n"
                    heartbeat_at = time.monotonic() + 15
                time.sleep(0.5)

        return StreamingResponse(
            generate(),
            media_type="text/event-stream",
            headers={
                "Cache-Control": "no-cache, no-store",
                "X-Accel-Buffering": "no",
            },
        )

    @app.get(
        "/api/v2/review-runs/{review_run_id}/experience",
        response_model=PublicReviewExperienceV1,
    )
    def get_review_experience_endpoint(
        review_run_id: str,
        principal: Principal = Depends(readers),
        service: WorkbenchService = Depends(_service),
    ) -> PublicReviewExperienceV1:
        return service.get_public_review_experience(principal, review_run_id)

    @app.post(
        "/api/v2/review-runs/{review_run_id}/assistant/conversations",
        response_model=AssistantConversationRecord,
        status_code=201,
    )
    def create_assistant_conversation_endpoint(
        review_run_id: str,
        body: ConversationCreateV2,
        principal: Principal = Depends(chat_users),
        service: WorkbenchService = Depends(_service),
    ) -> AssistantConversationRecord:
        return service.create_assistant_conversation(principal, review_run_id, body)

    @app.get(
        "/api/v2/review-runs/{review_run_id}/assistant/conversations",
        response_model=AssistantConversationListResponse,
    )
    def list_assistant_conversations_endpoint(
        review_run_id: str,
        principal: Principal = Depends(readers),
        service: WorkbenchService = Depends(_service),
    ) -> AssistantConversationListResponse:
        return AssistantConversationListResponse(
            items=service.list_assistant_conversations(principal, review_run_id)
        )

    @app.post(
        "/api/v2/assistant/conversations/{conversation_id}/messages",
        response_model=AssistantJobEnqueueResponse,
        status_code=202,
    )
    def enqueue_assistant_message_endpoint(
        conversation_id: str,
        body: AssistantMessageCreateV2,
        idempotency_key: IdempotencyKey,
        principal: Principal = Depends(chat_users),
        service: WorkbenchService = Depends(_service),
    ) -> AssistantJobEnqueueResponse:
        job, user_message, created = service.enqueue_assistant_message(
            principal,
            conversation_id,
            body,
            idempotency_key,
        )
        return AssistantJobEnqueueResponse(
            job=_assistant_job_view(job),
            user_message=user_message,
            idempotency_reused=not created,
        )

    @app.get(
        "/api/v2/assistant/conversations/{conversation_id}/messages",
        response_model=AssistantMessageListResponse,
    )
    def list_assistant_messages_endpoint(
        conversation_id: str,
        principal: Principal = Depends(readers),
        service: WorkbenchService = Depends(_service),
    ) -> AssistantMessageListResponse:
        return AssistantMessageListResponse(
            items=service.list_assistant_messages(principal, conversation_id)
        )

    @app.get("/api/v2/assistant/jobs/{assistant_job_id}", response_model=AssistantJobView)
    def get_assistant_job_endpoint(
        assistant_job_id: str,
        principal: Principal = Depends(readers),
        service: WorkbenchService = Depends(_service),
    ) -> AssistantJobView:
        return _assistant_job_view(service.get_assistant_job(principal, assistant_job_id))

    @app.get(
        "/api/v2/assistant/conversations/{conversation_id}/jobs",
        response_model=AssistantJobListResponse,
    )
    def list_assistant_jobs_endpoint(
        conversation_id: str,
        principal: Principal = Depends(readers),
        service: WorkbenchService = Depends(_service),
    ) -> AssistantJobListResponse:
        return AssistantJobListResponse(
            items=[
                _assistant_job_view(job)
                for job in service.list_assistant_jobs(principal, conversation_id)
            ]
        )

    @app.get("/api/v2/assistant/jobs/{assistant_job_id}/events/stream")
    def stream_assistant_job_events_endpoint(
        assistant_job_id: str,
        last_event_id: Annotated[str | None, Header(alias="Last-Event-ID")] = None,
        principal: Principal = Depends(readers),
        service: WorkbenchService = Depends(_service),
    ) -> StreamingResponse:
        service.get_assistant_job(principal, assistant_job_id)

        def generate():
            try:
                next_sequence = max(int(last_event_id or "0") + 1, 1)
            except ValueError:
                next_sequence = 1
            deadline = time.monotonic() + 300
            heartbeat_at = time.monotonic() + 15
            terminal = {
                "succeeded",
                "failed",
                "delivery_uncertain",
                "response_received_rejected",
                "response_persistence_uncertain",
            }
            while time.monotonic() < deadline:
                try:
                    job = service.get_assistant_job(principal, assistant_job_id)
                except NotFoundError:
                    return
                for event in job.events:
                    if event.sequence < next_sequence:
                        continue
                    yield (
                        f"id: {event.sequence}\n"
                        "event: assistant-stage\n"
                        f"data: {json.dumps(event.model_dump(mode='json'), separators=(',', ':'))}\n\n"
                    )
                    next_sequence = event.sequence + 1
                if job.state.value in terminal and all(
                    event.sequence < next_sequence for event in job.events
                ):
                    return
                if time.monotonic() >= heartbeat_at:
                    yield ": keep-alive\n\n"
                    heartbeat_at = time.monotonic() + 15
                time.sleep(0.5)

        return StreamingResponse(
            generate(),
            media_type="text/event-stream",
            headers={
                "Cache-Control": "no-cache, no-store",
                "X-Accel-Buffering": "no",
            },
        )

    @app.get("/api/v1/cases/{case_id}/audit", response_model=AuditListResponse)
    def list_audit_endpoint(
        case_id: str,
        principal: Principal = Depends(readers),
        service: WorkbenchService = Depends(_service),
    ) -> AuditListResponse:
        service.get_case(principal, case_id)
        return AuditListResponse(items=service.repository.list_audit(principal.org_id, case_id))

    return app


def _error_response(
    status_code: int,
    code: str,
    *,
    instance: str,
    headers: Mapping[str, str] | None = None,
) -> JSONResponse:
    titles = {
        400: "Bad Request",
        401: "Unauthorized",
        403: "Forbidden",
        404: "Not Found",
        405: "Method Not Allowed",
        409: "Conflict",
        413: "Content Too Large",
        422: "Unprocessable Content",
        429: "Too Many Requests",
        503: "Service Unavailable",
    }
    problem = ProblemDetailsV1(
        title=titles.get(status_code, "HTTP Error"),
        status=status_code,
        code=code,
        detail="The request could not be completed safely.",
        instance=instance,
    )
    response_headers = {
        "Cache-Control": "no-store",
        "Pragma": "no-cache",
    }
    if headers is not None:
        response_headers.update(headers)
    return JSONResponse(
        status_code=status_code,
        content=problem.model_dump(mode="json"),
        media_type="application/problem+json",
        headers=response_headers,
    )
