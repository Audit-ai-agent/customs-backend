from __future__ import annotations

import json
import time
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
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
    load_assertion_signer,
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
from customs_backend.release import (
    AgentReleasePin,
    AgentServicePin,
    ReleaseConfigurationError,
    load_auto_completion_approval,
    validate_agent_contract_bundle,
)
from customs_backend.repository import InMemoryWorkbenchRepository, PostgresWorkbenchRepository
from customs_backend.service import WorkbenchService
from customs_backend.settings import Settings, get_settings

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


@dataclass
class Container:
    settings: Settings
    repository: WorkbenchRepository
    object_store: ObjectStore
    publisher: JobPublisher
    agent: AgentClient
    token_verifier: TokenVerifier
    service: WorkbenchService


def build_container(settings: Settings) -> Container:
    if settings.process_role != "runtime":
        raise ReleaseConfigurationError("the HTTP/worker container requires the runtime role")
    validate_agent_contract_bundle()
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
    if settings.environment == "production":
        if (
            settings.database_url is None
            or settings.agent_service_token is None
            or settings.minio_endpoint is None
            or settings.minio_access_key is None
            or settings.minio_secret_key is None
            or settings.redis_url is None
        ):
            raise ReleaseConfigurationError("production dependencies are incomplete")
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
        assertion_signer=load_assertion_signer(
            settings.assertion_signing_key_registry_path,
            expected_trust_store_sha256=(
                settings.agent_expected_assertion_trust_store_sha256
                or "ccad11713d4c18b4f19246f32fc68cbd4faba2d66889f9c037845fdf66db3b13"
            ),
        ),
        requirement_policy_resolver=load_requirement_policy_resolver(
            policy_path=settings.requirement_policy_release_path,
            trust_store_path=settings.requirement_policy_trust_store_path,
            runtime_v3_path=settings.evidence_runtime_v3_release_path,
            expected_runtime_v3_sha256=settings.agent_expected_evidence_runtime_v3_sha256,
        ),
        job_lease_seconds=settings.job_lease_seconds,
        job_max_attempts=settings.job_max_attempts,
        outbox_lease_seconds=settings.outbox_lease_seconds,
    )
    return Container(settings, repository, object_store, publisher, agent, verifier, service)


class _UnavailableAgentClient:
    def _fail(self) -> Never:
        raise RuntimeError("Agent service token is not configured")

    def review(self, request: Any) -> dict[str, Any]:
        self._fail()

    def revise(self, request: Any) -> dict[str, Any]:
        self._fail()

    def answer(
        self,
        *,
        provider_result: dict[str, Any],
        question: str,
        question_id: str,
    ) -> Any:
        self._fail()

    def metadata(self) -> dict[str, Any]:
        self._fail()


def _service(request: Request) -> WorkbenchService:
    return request.app.state.container.service


def create_app(*, container: Container | None = None, settings: Settings | None = None) -> FastAPI:
    actual_settings = settings or get_settings()
    actual_container = container or build_container(actual_settings)
    app = FastAPI(
        title="Customs Review Workbench API",
        version="1.0.0",
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
        if request.url.path.startswith("/api/v1/"):
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
