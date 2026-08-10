"""The compact Browser-facing Backend API."""

from __future__ import annotations

import os
import re
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path
from typing import Annotated, Any, Literal

from fastapi import Depends, FastAPI, Header, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from pydantic import BaseModel, Field
from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from customs_backend.adapters import HttpAgentClient, InMemoryObjectStore
from customs_backend.agent_contract import BackendConfigurationError, validate_agent_contract_bundle
from customs_backend.auth import StaticTokenVerifier, principal_dependency, require_roles
from customs_backend.case_input_boundary import case_input_notice, safe_case_transcript_content
from customs_backend.domain import (
    CaseCreate,
    CaseRecord,
    ConversationCreate,
    ConversationMessageCreate,
    ConversationMessageExchange,
    ConversationMessageRecord,
    ConversationRecord,
    CorrectionProposalCreate,
    CorrectionProposalDecisionCreate,
    CorrectionProposalDecisionRecord,
    CorrectionProposalHistoryEntry,
    CorrectionProposalRecord,
    CorrectionProposalStatus,
    FindingDecisionRecord,
    FindingDecisionRequest,
    JobStatus,
    KnowledgeConversationCreate,
    KnowledgeConversationRecord,
    Principal,
    RevisionCreate,
    ReviewEnqueueRequest,
    ReviewRunRecord,
    Role,
    TextDocumentCreate,
)
from customs_backend.ports import (
    AgentClient,
    AuthorizationError,
    ConflictError,
    KnowledgeInputRejectedError,
    NotFoundError,
    ObjectStore,
    TokenVerifier,
    WorkbenchRepository,
)
from customs_backend.repository import InMemoryWorkbenchRepository
from customs_backend.scenario_transcript import (
    FunctionalTranscriptController,
    ScenarioTranscriptRoot,
    workspace_transcript_root,
)
from customs_backend.service import WorkbenchService
from customs_backend.settings import Settings, get_settings


IdempotencyKey = Annotated[
    str,
    Header(alias="Idempotency-Key", min_length=8, max_length=128, pattern=r"^[A-Za-z0-9._:-]+$"),
]
MAX_PUBLIC_REQUEST_BODY_BYTES = 4 * 1024 * 1024
_DEV_FIXTURE_REVIEW_AS_OF = date(2026, 8, 5)


class ProblemDetailsV1(BaseModel):
    model_config = {"extra": "forbid"}

    type: Literal["about:blank"] = "about:blank"
    title: str
    status: int
    code: str
    detail: str
    instance: str


class SessionResponse(BaseModel):
    subject: str
    org_id: str
    roles: list[Role]


class DevelopmentSessionResponse(BaseModel):
    access_token: str = Field(min_length=32, max_length=512)
    token_type: Literal["Bearer"] = "Bearer"
    expires_in: int = Field(ge=60, le=86_400)
    session: SessionResponse


class DevelopmentSessionRequest(BaseModel):
    """Explicit local identity selection; the endpoint is loopback-only."""

    actor: Literal["reviewer", "approver"] = "reviewer"


class AuthConfigResponse(BaseModel):
    issuer: str
    audience: str
    client_id: str
    authorization_endpoint: str
    token_endpoint: str
    end_session_endpoint: str | None = None
    flow: Literal["authorization_code_pkce_s256"] = "authorization_code_pkce_s256"
    token_storage: Literal["memory_only"] = "memory_only"


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
        replaces_document_id: str | None = None
        superseded_by_document_id: str | None = None
        superseded_at: datetime | None = None

    record: PublicDocumentRecord
    content_text: str | None = None


class CaseListResponse(BaseModel):
    items: list[CaseRecord]


class DocumentListResponse(BaseModel):
    items: list[DocumentViewResponse]


class ConversationView(BaseModel):
    conversation_id: str
    review_run_id: str
    title: str | None = None
    created_by: str
    created_at: datetime
    updated_at: datetime

    @classmethod
    def from_record(cls, record: ConversationRecord) -> "ConversationView":
        return cls(
            conversation_id=record.conversation_id,
            review_run_id=record.review_run_id,
            title=record.title,
            created_by=record.created_by,
            created_at=record.created_at,
            updated_at=record.updated_at,
        )


class ConversationListResponse(BaseModel):
    items: list[ConversationView]


class ConversationMessageListResponse(BaseModel):
    items: list[ConversationMessageRecord]


class KnowledgeConversationView(BaseModel):
    conversation_id: str
    title: str | None = None
    trade_type: Literal["import", "export", "both"]
    as_of: date
    snapshot_id: str | None = None
    created_by: str
    created_at: datetime
    updated_at: datetime

    @classmethod
    def from_record(cls, record: KnowledgeConversationRecord) -> "KnowledgeConversationView":
        return cls(
            conversation_id=record.conversation_id,
            title=record.title,
            trade_type=record.trade_type,
            as_of=record.as_of,
            snapshot_id=record.snapshot_id,
            created_by=record.created_by,
            created_at=record.created_at,
            updated_at=record.updated_at,
        )


class KnowledgeConversationListResponse(BaseModel):
    items: list[KnowledgeConversationView]


class KnowledgeConversationMessageListResponse(BaseModel):
    items: list[ConversationMessageRecord]


class CorrectionProposalView(BaseModel):
    correction_proposal_id: str
    review_run_id: str
    document_id: str
    field_name: str
    new_value: str
    unit: str | None = None
    fact_id: str | None = None
    item_id: str | None = None
    finding_id: str | None = None
    reason: str
    status: CorrectionProposalStatus
    created_by: str
    created_at: datetime
    decision: CorrectionProposalDecisionRecord | None = None
    applied_review_run_id: str | None = None
    applied_by: str | None = None
    applied_at: datetime | None = None
    history: list[CorrectionProposalHistoryEntry]

    @classmethod
    def from_record(cls, record: CorrectionProposalRecord) -> "CorrectionProposalView":
        return cls(
            correction_proposal_id=record.correction_proposal_id,
            review_run_id=record.review_run_id,
            document_id=record.document_id,
            field_name=record.field_name,
            new_value=record.new_value,
            unit=record.unit,
            fact_id=record.fact_id,
            item_id=record.item_id,
            finding_id=record.finding_id,
            reason=record.reason,
            status=record.status,
            created_by=record.created_by,
            created_at=record.created_at,
            decision=record.decision,
            applied_review_run_id=record.applied_review_run_id,
            applied_by=record.applied_by,
            applied_at=record.applied_at,
            history=record.history,
        )


class CorrectionProposalListResponse(BaseModel):
    items: list[CorrectionProposalView]


class ReviewRunSummary(BaseModel):
    """Compact case-history projection; use the detail endpoint for facts."""

    review_run_id: str
    case_id: str
    job_id: str
    review_as_of: date
    trade_type: Literal["import", "export"]
    execution_status: str
    assessment: str
    review_disposition: str
    result_digest: str
    finding_count: int = Field(ge=0)
    reviewer_decision_count: int = Field(ge=0)
    parent_review_run_id: str | None = None
    applied_correction_proposal_ids: list[str] = Field(default_factory=list, max_length=50)
    created_at: datetime

    @classmethod
    def from_run(cls, run: ReviewRunRecord) -> "ReviewRunSummary":
        return cls(
            review_run_id=run.review_run_id,
            case_id=run.case_id,
            job_id=run.job_id,
            review_as_of=run.review_as_of,
            trade_type=run.trade_type,
            execution_status=run.execution_status.value,
            assessment=run.assessment.value,
            review_disposition=run.review_disposition.value,
            result_digest=run.result_digest,
            finding_count=len(run.findings),
            reviewer_decision_count=len(run.reviewer_decisions),
            parent_review_run_id=run.parent_review_run_id,
            applied_correction_proposal_ids=list(run.applied_correction_proposal_ids),
            created_at=run.created_at,
        )


class ReviewRunListResponse(BaseModel):
    items: list[ReviewRunSummary]


class JobView(BaseModel):
    job_id: str
    case_id: str
    status: JobStatus
    review_run_id: str
    error_code: str | None = None
    created_by: str
    created_at: datetime
    updated_at: datetime


class JobResponse(BaseModel):
    job: JobView
    idempotency_reused: bool


class _RequestBodyTooLarge(Exception):
    pass


class RequestBodyLimitMiddleware:
    """Bound public JSON bodies before FastAPI parses them."""

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
            413, "request_body_too_large", instance=str(scope.get("path", "/"))
        )
        await response(scope, receive, send)


@dataclass
class Container:
    settings: Settings
    repository: WorkbenchRepository
    object_store: ObjectStore
    agent: AgentClient
    token_verifier: TokenVerifier
    service: WorkbenchService
    transcript: FunctionalTranscriptController | None = None


_SECRET_ENVIRONMENT_NAME = re.compile(
    r"(?:authorization|cookie|token|api[_-]?key|secret|password|credential|dsn)", re.IGNORECASE
)


def _transcript_secret_values(settings: Settings) -> tuple[str, ...]:
    """Collect local configured values for in-memory redaction only."""

    values: list[str] = []
    for value in (
        settings.agent_service_token,
        settings.dev_reviewer_session_token,
        settings.dev_approver_session_token,
        settings.database_url,
        settings.minio_access_key,
        settings.minio_secret_key,
    ):
        if value is not None:
            values.append(value.get_secret_value())
    values.extend(
        value
        for name, value in os.environ.items()
        if value and _SECRET_ENVIRONMENT_NAME.search(name)
    )
    return tuple(values)


def _build_transcript_controller(settings: Settings) -> FunctionalTranscriptController | None:
    """Create the automatic functional recorder or an explicit dev trace writer."""

    if settings.environment == "functional-uat":
        root = workspace_transcript_root()
        profile = "functional-uat"
    elif settings.environment == "local-development":
        configured = os.environ.get("CUSTOMS_SCENARIO_TRANSCRIPT_ROOT", "").strip()
        if not configured:
            return None
        root = Path(configured)
        profile = "dev-fixture"
    else:
        return None
    return FunctionalTranscriptController(
        root=ScenarioTranscriptRoot.create(root, secret_values=_transcript_secret_values(settings)),
        profile=profile,
    )


def _build_local_profile_container(settings: Settings) -> Container:
    """Build the only two supported local profiles.

    Imports for the functional-UAT storage and OIDC adapters occur only in its
    branch, so the zero-external-input development profile needs no database,
    object-store, identity, or migration dependency.
    """

    if settings.environment not in {"local-development", "functional-uat"}:
        raise BackendConfigurationError("unsupported Backend local profile")
    validate_agent_contract_bundle()
    if settings.agent_service_token is None or settings.service_mode is None:
        raise BackendConfigurationError("Backend Agent service configuration is incomplete")
    transcript = _build_transcript_controller(settings)
    agent: AgentClient = HttpAgentClient(
        base_url=settings.agent_base_url,
        service_token=settings.agent_service_token.get_secret_value(),
        timeout_seconds=settings.agent_timeout_seconds,
        service_mode=settings.service_mode,
        review_trace=transcript.record_backend_agent if transcript is not None else None,
    )
    if settings.environment == "local-development":
        reviewer_token = settings.dev_reviewer_session_token
        approver_token = settings.dev_approver_session_token
        if reviewer_token is None or approver_token is None:
            raise BackendConfigurationError("local development session identities are unavailable")
        repository: WorkbenchRepository = InMemoryWorkbenchRepository()
        object_store: ObjectStore = InMemoryObjectStore()
        reviewer = Principal(
            subject=settings.dev_reviewer_subject,
            org_id=settings.dev_session_org_id,
            roles=frozenset({Role.reviewer}),
        )
        approver = Principal(
            subject=settings.dev_approver_subject,
            org_id=settings.dev_session_org_id,
            roles=frozenset({Role.approver}),
        )
        verifier: TokenVerifier = StaticTokenVerifier(
            {
                reviewer_token.get_secret_value(): reviewer,
                approver_token.get_secret_value(): approver,
            }
        )
    else:
        # Keep functional-only dependencies optional for the default developer
        # install.  This code path is reached only after Settings validates all
        # loopback store and identity configuration.
        from customs_backend.adapters import MinioObjectStore
        from customs_backend.auth import OidcJwtVerifier
        from customs_backend.repository import PostgresWorkbenchRepository

        assert settings.database_url is not None
        assert settings.minio_endpoint is not None
        assert settings.minio_access_key is not None
        assert settings.minio_secret_key is not None
        repository = PostgresWorkbenchRepository(settings.database_url.get_secret_value())
        object_store = MinioObjectStore(
            endpoint=settings.minio_endpoint,
            access_key=settings.minio_access_key.get_secret_value(),
            secret_key=settings.minio_secret_key.get_secret_value(),
            bucket=settings.minio_bucket,
            secure=settings.minio_secure,
        )
        verifier = OidcJwtVerifier(
            issuer=settings.oidc_issuer,
            audience=settings.oidc_audience,
            jwks_url=settings.oidc_jwks_url,
            org_claim=settings.oidc_org_claim,
            roles_claim=settings.oidc_roles_claim,
        )
    service = WorkbenchService(
        repository=repository,
        object_store=object_store,
        agent=agent,
    )
    return Container(
        settings=settings,
        repository=repository,
        object_store=object_store,
        agent=agent,
        token_verifier=verifier,
        service=service,
        transcript=transcript,
    )


def build_container(settings: Settings) -> Container:
    if settings.process_role != "runtime":
        raise BackendConfigurationError("the Backend HTTP process supports only the runtime role")
    if settings.environment in {"local-development", "functional-uat"}:
        return _build_local_profile_container(settings)
    raise BackendConfigurationError(
        "unsupported Backend runtime profile; use local-development or functional-uat"
    )


def _service(request: Request) -> WorkbenchService:
    return request.app.state.container.service


def _safe_case_transcript_request(body: ConversationMessageCreate) -> dict[str, str]:
    """Keep authorised Case persistence separate from transcript retention."""

    return {"content": safe_case_transcript_content(body.content)}


def _safe_case_transcript_response(value: Mapping[str, Any]) -> dict[str, Any]:
    """Redact only document-shaped user message bodies from transcript output."""

    def redact(item: Any) -> Any:
        if isinstance(item, list):
            return [redact(entry) for entry in item]
        if not isinstance(item, dict):
            return item
        copied = {key: redact(entry) for key, entry in item.items()}
        if copied.get("role") in {"user", "assistant"} and isinstance(copied.get("content"), str):
            copied["content"] = safe_case_transcript_content(copied["content"])
        return copied

    redacted = redact(dict(value))
    assert isinstance(redacted, dict)
    return redacted


def _verifier_ready(verifier: TokenVerifier) -> bool:
    health = getattr(verifier, "health", None)
    if health is None:
        return True
    try:
        return bool(health())
    except Exception:
        return False


def create_app(*, container: Container | None = None, settings: Settings | None = None) -> FastAPI:
    actual_settings = settings or get_settings()
    actual_container = container or build_container(actual_settings)
    # Detailed Browser traffic is a functional-UAT record only.  A developer
    # can still opt in to the compact internal trace through the local profile,
    # but ordinary dev-fixture Browser use must not start retaining bodies.
    public_transcript = (
        actual_container.transcript
        if actual_container.settings.environment == "functional-uat"
        else None
    )

    def record_public_exchange(
        *,
        case_id: str,
        method: str,
        path: str,
        request_body: Mapping[str, Any] | None,
        response_status: int,
        response_body: Mapping[str, Any] | None,
        review_run_id: str | None = None,
    ) -> None:
        if public_transcript is not None:
            public_transcript.record_public(
                case_id=case_id,
                method=method,
                path=path,
                request_body=request_body,
                response_status=response_status,
                response_body=response_body,
                review_run_id=review_run_id,
            )

    app = FastAPI(
        title="Customs Review Workbench API",
        version="1.0.0",
        description="Local text-review API. Results always require human review.",
        responses={
            code: {"model": ProblemDetailsV1, "description": "Fail-closed problem response."}
            for code in (400, 401, 403, 404, 409, 413, 422, 503)
        },
    )
    app.add_middleware(RequestBodyLimitMiddleware, max_bytes=MAX_PUBLIC_REQUEST_BODY_BYTES)
    app.state.container = actual_container

    @app.middleware("http")
    async def local_mode_headers(request: Request, call_next):
        response = await call_next(request)
        if actual_container.settings.service_mode is not None:
            response.headers["X-Customs-Service-Mode"] = actual_container.settings.service_mode
        if request.url.path.startswith("/api/v1/"):
            response.headers["Cache-Control"] = "no-store"
            response.headers["Pragma"] = "no-cache"
        return response

    @app.exception_handler(NotFoundError)
    async def not_found_handler(request: Request, _exc: NotFoundError):
        return _error_response(404, "not_found", instance=request.url.path)

    @app.exception_handler(ConflictError)
    async def conflict_handler(request: Request, _exc: ConflictError):
        return _error_response(409, "stale_or_conflicting_state", instance=request.url.path)

    @app.exception_handler(AuthorizationError)
    async def authorization_handler(request: Request, _exc: AuthorizationError):
        return _error_response(403, "permission_denied", instance=request.url.path)

    @app.exception_handler(KnowledgeInputRejectedError)
    async def knowledge_input_handler(request: Request, _exc: KnowledgeInputRejectedError):
        # This stable response deliberately contains no rejected value.
        return _error_response(422, "knowledge_input_not_allowed", instance=request.url.path)

    @app.exception_handler(ValueError)
    async def value_handler(request: Request, _exc: ValueError):
        return _error_response(422, "invalid_operation", instance=request.url.path)

    @app.exception_handler(RequestValidationError)
    async def request_validation_handler(request: Request, _exc: RequestValidationError):
        return _error_response(422, "request_validation_failed", instance=request.url.path)

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
        dependencies: dict[str, bool] = {}
        for name, check in (
            ("repository", actual_container.repository.health),
            ("object_store", actual_container.object_store.health),
        ):
            try:
                dependencies[name] = bool(check())
            except Exception:
                dependencies[name] = False
        try:
            agent_ready = isinstance(actual_container.agent.metadata(), dict)
        except Exception:
            agent_ready = False
        if (
            not _verifier_ready(actual_container.token_verifier)
            or not agent_ready
            or not all(dependencies.values())
        ):
            raise HTTPException(status_code=503, detail="required dependency is not ready")
        return {
            "status": "ready",
            "mode": actual_container.settings.service_mode or "test",
            "human_review_only": True,
            "dependencies": dependencies,
        }

    @app.get("/health/functional-uat-ready", include_in_schema=False)
    def health_functional_uat_ready() -> dict[str, Any]:
        if actual_container.settings.environment != "functional-uat":
            raise HTTPException(status_code=404, detail="functional UAT is unavailable")
        health_ready()
        return {
            "status": "ready",
            "mode": "functional-uat",
            "as_of": actual_container.settings.functional_uat_as_of.isoformat(),
            "human_review_only": True,
        }

    @app.get("/api/v1/auth/config", response_model=AuthConfigResponse)
    def auth_config() -> AuthConfigResponse:
        values = actual_container.settings
        if values.environment != "functional-uat":
            raise HTTPException(status_code=404, detail="OIDC is disabled for local development")
        required = {
            "issuer": values.oidc_issuer,
            "audience": values.oidc_audience,
            "client_id": values.oidc_client_id,
            "authorization_endpoint": values.oidc_authorization_endpoint,
            "token_endpoint": values.oidc_token_endpoint,
        }
        if any(value is None for value in required.values()):
            raise HTTPException(status_code=503, detail="OIDC client configuration is unavailable")
        return AuthConfigResponse(
            **required,  # type: ignore[arg-type]
            end_session_endpoint=values.oidc_end_session_endpoint,
        )

    @app.post("/api/v1/dev/session", response_model=DevelopmentSessionResponse)
    def create_development_session(
        request: Request,
        body: DevelopmentSessionRequest | None = None,
    ) -> DevelopmentSessionResponse:
        if actual_container.settings.environment != "local-development":
            raise HTTPException(status_code=404, detail="development session is unavailable")
        client = request.client
        if client is None or client.host not in {"127.0.0.1", "::1"}:
            raise HTTPException(status_code=404, detail="development session is unavailable")
        actor = "reviewer" if body is None else body.actor
        token = (
            actual_container.settings.dev_reviewer_session_token
            if actor == "reviewer"
            else actual_container.settings.dev_approver_session_token
        )
        if token is None:
            raise HTTPException(status_code=503, detail="development session is unavailable")
        principal = actual_container.token_verifier.verify(token.get_secret_value())
        return DevelopmentSessionResponse(
            access_token=token.get_secret_value(),
            expires_in=actual_container.settings.dev_session_ttl_seconds,
            session=SessionResponse(
                subject=principal.subject,
                org_id=principal.org_id,
                roles=sorted(principal.roles, key=lambda role: role.value),
            ),
        )

    @app.get("/api/v1/session", response_model=SessionResponse)
    def session(principal: Principal = Depends(principal_dependency)) -> SessionResponse:
        return SessionResponse(
            subject=principal.subject,
            org_id=principal.org_id,
            roles=sorted(principal.roles, key=lambda role: role.value),
        )

    writers = require_roles(Role.reviewer)
    readers = require_roles(Role.reviewer, Role.approver)
    approvers = require_roles(Role.approver)

    @app.post("/api/v1/cases", response_model=CaseRecord, status_code=201)
    def create_case_endpoint(
        body: CaseCreate,
        principal: Principal = Depends(writers),
        service: WorkbenchService = Depends(_service),
    ) -> CaseRecord:
        result = service.create_case(principal, body)
        record_public_exchange(
            case_id=result.case_id,
            method="POST",
            path="/api/v1/cases",
            request_body=body.model_dump(mode="json"),
            response_status=201,
            response_body=result.model_dump(mode="json"),
        )
        return result

    @app.get("/api/v1/cases", response_model=CaseListResponse)
    def list_cases_endpoint(
        principal: Principal = Depends(readers), service: WorkbenchService = Depends(_service)
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
        result = DocumentViewResponse(
            record=DocumentViewResponse.PublicDocumentRecord.model_validate(
                record.model_dump(mode="json")
            ),
            content_text=body.content_text,
        )
        record_public_exchange(
            case_id=case_id,
            method="POST",
            path=f"/api/v1/cases/{case_id}/documents",
            request_body=body.model_dump(mode="json"),
            response_status=201,
            response_body=result.model_dump(mode="json"),
        )
        return result

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
                    )
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

    @app.post("/api/v1/cases/{case_id}/reviews", response_model=JobResponse, status_code=202)
    def enqueue_review_endpoint(
        case_id: str,
        body: ReviewEnqueueRequest,
        idempotency_key: IdempotencyKey,
        principal: Principal = Depends(writers),
        service: WorkbenchService = Depends(_service),
    ) -> JobResponse:
        public_request = body.model_dump(mode="json", exclude_none=True)
        if (
            actual_container.settings.environment == "local-development"
            and body.review_as_of is None
        ):
            # The deterministic Evidence fixture has one fixed synthetic
            # knowledge date.  Keep an omitted date reproducible instead of
            # silently making a normal team-development run fail as wall time
            # moves beyond that fixture date.
            body = body.model_copy(update={"review_as_of": _DEV_FIXTURE_REVIEW_AS_OF})
        elif actual_container.settings.environment == "functional-uat":
            # A missing date remains reproducible at the Functional-UAT
            # baseline.  An explicit date must reach Agent/Data unchanged:
            # law-history coverage and its partial status are owned there,
            # not silently replaced by Backend's HSK/requirements baseline.
            if body.review_as_of is None:
                body = body.model_copy(
                    update={"review_as_of": actual_container.settings.functional_uat_as_of}
                )
        job, created = service.review(principal, case_id, body, idempotency_key)
        result = JobResponse(
            job=JobView.model_validate(job.model_dump(mode="json")), idempotency_reused=not created
        )
        if public_transcript is not None:
            if created:
                public_transcript.bind_review(
                    case_id=case_id,
                    review_run_id=job.review_run_id,
                    job_id=job.job_id,
                    review_request=public_request,
                    response_status=202,
                    response_body=result.model_dump(mode="json"),
                    job_status=job.status.value,
                )
            else:
                record_public_exchange(
                    case_id=case_id,
                    method="POST",
                    path=f"/api/v1/cases/{case_id}/reviews",
                    request_body=public_request,
                    response_status=202,
                    response_body=result.model_dump(mode="json"),
                    review_run_id=job.review_run_id,
                )
        return result

    @app.get("/api/v1/jobs/{job_id}", response_model=JobView)
    def get_job_endpoint(
        job_id: str,
        principal: Principal = Depends(readers),
        service: WorkbenchService = Depends(_service),
    ) -> JobView:
        job = service.get_job(principal, job_id)
        result = JobView.model_validate(job.model_dump(mode="json"))
        record_public_exchange(
            case_id=job.case_id,
            method="GET",
            path=f"/api/v1/jobs/{job_id}",
            request_body=None,
            response_status=200,
            response_body=result.model_dump(mode="json"),
            review_run_id=job.review_run_id,
        )
        if public_transcript is not None and job.status == JobStatus.failed:
            public_transcript.finalize(
                job.review_run_id,
                status="failed",
                fields={"job_id": job.job_id, "job_status": job.status.value},
            )
        return result

    @app.get("/api/v1/cases/{case_id}/review-runs", response_model=ReviewRunListResponse)
    def list_runs_endpoint(
        case_id: str,
        principal: Principal = Depends(readers),
        service: WorkbenchService = Depends(_service),
    ) -> ReviewRunListResponse:
        return ReviewRunListResponse(
            items=[ReviewRunSummary.from_run(run) for run in service.list_runs(principal, case_id)]
        )

    @app.get("/api/v1/review-runs/{review_run_id}", response_model=ReviewRunRecord)
    def get_run_endpoint(
        review_run_id: str,
        principal: Principal = Depends(readers),
        service: WorkbenchService = Depends(_service),
    ) -> ReviewRunRecord:
        result = service.get_run(principal, review_run_id)
        record_public_exchange(
            case_id=result.case_id,
            method="GET",
            path=f"/api/v1/review-runs/{review_run_id}",
            request_body=None,
            response_status=200,
            response_body=result.model_dump(mode="json"),
            review_run_id=review_run_id,
        )
        if public_transcript is not None:
            public_transcript.finalize(
                review_run_id,
                status="completed",
                fields={
                    "case_id": result.case_id,
                    "job_id": result.job_id,
                    "execution_status": result.execution_status.value,
                    "assessment": result.assessment.value,
                    "human_review_required": result.review_disposition.value
                    == "human_review_required",
                },
            )
        return result

    @app.put(
        "/api/v1/review-runs/{review_run_id}/findings/{finding_id}/decision",
        response_model=FindingDecisionRecord,
    )
    def record_finding_decision_endpoint(
        review_run_id: str,
        finding_id: str,
        body: FindingDecisionRequest,
        principal: Principal = Depends(writers),
        service: WorkbenchService = Depends(_service),
    ) -> FindingDecisionRecord:
        result = service.record_finding_decision(principal, review_run_id, finding_id, body)
        run = service.get_run(principal, review_run_id)
        record_public_exchange(
            case_id=run.case_id,
            method="PUT",
            path=(f"/api/v1/review-runs/{review_run_id}/findings/{finding_id}/decision"),
            request_body=body.model_dump(mode="json"),
            response_status=200,
            response_body=result.model_dump(mode="json"),
            review_run_id=review_run_id,
        )
        return result

    @app.post(
        "/api/v1/review-runs/{review_run_id}/assistant/conversations",
        response_model=ConversationView,
        response_model_exclude_none=True,
        status_code=201,
    )
    def create_assistant_conversation_endpoint(
        review_run_id: str,
        body: ConversationCreate,
        principal: Principal = Depends(readers),
        service: WorkbenchService = Depends(_service),
    ) -> ConversationView:
        record = service.create_conversation(principal, review_run_id, body)
        result = ConversationView.from_record(record)
        run = service.get_run(principal, review_run_id)
        record_public_exchange(
            case_id=run.case_id,
            method="POST",
            path=f"/api/v1/review-runs/{review_run_id}/assistant/conversations",
            request_body=body.model_dump(mode="json", exclude_none=True),
            response_status=201,
            response_body=result.model_dump(mode="json", exclude_none=True),
            review_run_id=review_run_id,
        )
        return result

    @app.get(
        "/api/v1/review-runs/{review_run_id}/assistant/conversations",
        response_model=ConversationListResponse,
        response_model_exclude_none=True,
    )
    def list_assistant_conversations_endpoint(
        review_run_id: str,
        principal: Principal = Depends(readers),
        service: WorkbenchService = Depends(_service),
    ) -> ConversationListResponse:
        result = ConversationListResponse(
            items=[
                ConversationView.from_record(record)
                for record in service.list_conversations(principal, review_run_id)
            ]
        )
        run = service.get_run(principal, review_run_id)
        record_public_exchange(
            case_id=run.case_id,
            method="GET",
            path=f"/api/v1/review-runs/{review_run_id}/assistant/conversations",
            request_body=None,
            response_status=200,
            response_body=result.model_dump(mode="json", exclude_none=True),
            review_run_id=review_run_id,
        )
        return result

    @app.post(
        "/api/v1/assistant/conversations/{conversation_id}/messages",
        response_model=ConversationMessageExchange,
        response_model_exclude_none=True,
    )
    def send_assistant_message_endpoint(
        conversation_id: str,
        body: ConversationMessageCreate,
        idempotency_key: IdempotencyKey,
        principal: Principal = Depends(readers),
        service: WorkbenchService = Depends(_service),
    ) -> ConversationMessageExchange:
        run = service.get_conversation_run(principal, conversation_id)
        # A raw document-shaped paste is retained only in the authorised Case
        # conversation.  Do not create a Functional-UAT public-transcript
        # event for it: the fixed assistant notice is sufficient evidence of
        # the boundary and the pasted source text must not enter any
        # transcript stream.
        document_paste = case_input_notice(body.content) is not None
        result = service.send_conversation_message(
            principal,
            conversation_id,
            body,
            idempotency_key,
            functional_uat=actual_container.settings.environment == "functional-uat",
        )
        if not document_paste:
            record_public_exchange(
                case_id=run.case_id,
                method="POST",
                path=f"/api/v1/assistant/conversations/{conversation_id}/messages",
                request_body=_safe_case_transcript_request(body),
                response_status=200,
                response_body=_safe_case_transcript_response(
                    result.model_dump(mode="json", exclude_none=True)
                ),
                review_run_id=run.review_run_id,
            )
        return result

    @app.get(
        "/api/v1/assistant/conversations/{conversation_id}/messages",
        response_model=ConversationMessageListResponse,
        response_model_exclude_none=True,
    )
    def list_assistant_messages_endpoint(
        conversation_id: str,
        principal: Principal = Depends(readers),
        service: WorkbenchService = Depends(_service),
    ) -> ConversationMessageListResponse:
        result = ConversationMessageListResponse(
            items=service.list_conversation_messages(principal, conversation_id)
        )
        run = service.get_conversation_run(principal, conversation_id)
        record_public_exchange(
            case_id=run.case_id,
            method="GET",
            path=f"/api/v1/assistant/conversations/{conversation_id}/messages",
            request_body=None,
            response_status=200,
            response_body=_safe_case_transcript_response(
                result.model_dump(mode="json", exclude_none=True)
            ),
            review_run_id=run.review_run_id,
        )
        return result

    # Standalone official-trade guidance stays separate from Case/Review Run
    # conversations.  In particular it cannot be recorded under a scenario
    # transcript's review-run directory or accidentally receive case facts.
    @app.post(
        "/api/v1/knowledge/conversations",
        response_model=KnowledgeConversationView,
        response_model_exclude_none=True,
        status_code=201,
    )
    def create_knowledge_conversation_endpoint(
        body: KnowledgeConversationCreate,
        principal: Principal = Depends(readers),
        service: WorkbenchService = Depends(_service),
    ) -> KnowledgeConversationView:
        # The public field is optional for a simple general-question start,
        # but Agent/Data must never receive an ambiguous knowledge date.
        # Functional-UAT general knowledge remains on its independently fixed
        # snapshot date; document reviews may use a law-history date instead.
        if actual_container.settings.environment == "functional-uat":
            resolved_as_of = actual_container.settings.functional_uat_as_of
            if body.as_of is not None and body.as_of != resolved_as_of:
                raise HTTPException(
                    status_code=422,
                    detail="functional UAT uses its configured knowledge date",
                )
        elif body.as_of is not None:
            resolved_as_of = body.as_of
        elif actual_container.settings.environment == "local-development":
            resolved_as_of = _DEV_FIXTURE_REVIEW_AS_OF
        else:
            # Unit-test containers use the same documented functional-UAT
            # baseline instead of deriving a wall-clock date.
            resolved_as_of = actual_container.settings.functional_uat_as_of
        body = body.model_copy(update={"as_of": resolved_as_of})
        return KnowledgeConversationView.from_record(
            service.create_knowledge_conversation(principal, body)
        )

    @app.get(
        "/api/v1/knowledge/conversations",
        response_model=KnowledgeConversationListResponse,
        response_model_exclude_none=True,
    )
    def list_knowledge_conversations_endpoint(
        principal: Principal = Depends(readers),
        service: WorkbenchService = Depends(_service),
    ) -> KnowledgeConversationListResponse:
        return KnowledgeConversationListResponse(
            items=[
                KnowledgeConversationView.from_record(record)
                for record in service.list_knowledge_conversations(principal)
            ]
        )

    @app.post(
        "/api/v1/knowledge/conversations/{conversation_id}/messages",
        response_model=ConversationMessageExchange,
        response_model_exclude_none=True,
    )
    def send_knowledge_conversation_message_endpoint(
        conversation_id: str,
        body: ConversationMessageCreate,
        idempotency_key: IdempotencyKey,
        principal: Principal = Depends(readers),
        service: WorkbenchService = Depends(_service),
    ) -> ConversationMessageExchange:
        return service.send_knowledge_conversation_message(
            principal,
            conversation_id,
            body,
            idempotency_key,
            functional_uat=actual_container.settings.environment == "functional-uat",
        )

    @app.get(
        "/api/v1/knowledge/conversations/{conversation_id}/messages",
        response_model=KnowledgeConversationMessageListResponse,
        response_model_exclude_none=True,
    )
    def list_knowledge_conversation_messages_endpoint(
        conversation_id: str,
        principal: Principal = Depends(readers),
        service: WorkbenchService = Depends(_service),
    ) -> KnowledgeConversationMessageListResponse:
        return KnowledgeConversationMessageListResponse(
            items=service.list_knowledge_conversation_messages(principal, conversation_id)
        )

    @app.post(
        "/api/v1/review-runs/{review_run_id}/correction-proposals",
        response_model=CorrectionProposalView,
        response_model_exclude_none=True,
        status_code=201,
    )
    def create_correction_proposal_endpoint(
        review_run_id: str,
        body: CorrectionProposalCreate,
        principal: Principal = Depends(writers),
        service: WorkbenchService = Depends(_service),
    ) -> CorrectionProposalView:
        result = CorrectionProposalView.from_record(
            service.create_correction_proposal(principal, review_run_id, body)
        )
        run = service.get_run(principal, review_run_id)
        record_public_exchange(
            case_id=run.case_id,
            method="POST",
            path=f"/api/v1/review-runs/{review_run_id}/correction-proposals",
            request_body=body.model_dump(mode="json", exclude_none=True),
            response_status=201,
            response_body=result.model_dump(mode="json", exclude_none=True),
            review_run_id=review_run_id,
        )
        return result

    @app.get(
        "/api/v1/review-runs/{review_run_id}/correction-proposals",
        response_model=CorrectionProposalListResponse,
        response_model_exclude_none=True,
    )
    def list_correction_proposals_endpoint(
        review_run_id: str,
        principal: Principal = Depends(readers),
        service: WorkbenchService = Depends(_service),
    ) -> CorrectionProposalListResponse:
        result = CorrectionProposalListResponse(
            items=[
                CorrectionProposalView.from_record(record)
                for record in service.list_correction_proposals(principal, review_run_id)
            ]
        )
        run = service.get_run(principal, review_run_id)
        record_public_exchange(
            case_id=run.case_id,
            method="GET",
            path=f"/api/v1/review-runs/{review_run_id}/correction-proposals",
            request_body=None,
            response_status=200,
            response_body=result.model_dump(mode="json", exclude_none=True),
            review_run_id=review_run_id,
        )
        return result

    @app.put(
        "/api/v1/correction-proposals/{correction_proposal_id}/decision",
        response_model=CorrectionProposalView,
        response_model_exclude_none=True,
    )
    def decide_correction_proposal_endpoint(
        correction_proposal_id: str,
        body: CorrectionProposalDecisionCreate,
        principal: Principal = Depends(approvers),
        service: WorkbenchService = Depends(_service),
    ) -> CorrectionProposalView:
        result = CorrectionProposalView.from_record(
            service.decide_correction_proposal(principal, correction_proposal_id, body)
        )
        run = service.get_run(principal, result.review_run_id)
        record_public_exchange(
            case_id=run.case_id,
            method="PUT",
            path=f"/api/v1/correction-proposals/{correction_proposal_id}/decision",
            request_body=body.model_dump(mode="json"),
            response_status=200,
            response_body=result.model_dump(mode="json", exclude_none=True),
            review_run_id=run.review_run_id,
        )
        return result

    @app.post(
        "/api/v1/review-runs/{review_run_id}/revisions",
        response_model=JobResponse,
        status_code=202,
    )
    def create_revision_endpoint(
        review_run_id: str,
        body: RevisionCreate,
        idempotency_key: IdempotencyKey,
        principal: Principal = Depends(approvers),
        service: WorkbenchService = Depends(_service),
    ) -> JobResponse:
        job, created = service.create_revision(principal, review_run_id, body, idempotency_key)
        result = JobResponse(
            job=JobView.model_validate(job.model_dump(mode="json")), idempotency_reused=not created
        )
        public_request = body.model_dump(mode="json")
        public_path = f"/api/v1/review-runs/{review_run_id}/revisions"
        if public_transcript is not None and created:
            # The request belongs to the parent review, while the initial job
            # envelope is also the first public event of the new child run.
            # Keep both links explicit instead of routing by case identity.
            record_public_exchange(
                case_id=job.case_id,
                method="POST",
                path=public_path,
                request_body=public_request,
                response_status=202,
                response_body=result.model_dump(mode="json"),
                review_run_id=review_run_id,
            )
            public_transcript.bind_review(
                case_id=job.case_id,
                review_run_id=job.review_run_id,
                job_id=job.job_id,
                review_request=public_request,
                response_status=202,
                response_body=result.model_dump(mode="json"),
                job_status=job.status.value,
                public_path=public_path,
            )
        else:
            record_public_exchange(
                case_id=job.case_id,
                method="POST",
                path=public_path,
                request_body=public_request,
                response_status=202,
                response_body=result.model_dump(mode="json"),
                review_run_id=job.review_run_id,
            )
        return result

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
        503: "Service Unavailable",
    }
    response_headers = {"Cache-Control": "no-store", "Pragma": "no-cache"}
    if headers is not None:
        response_headers.update(headers)
    return JSONResponse(
        status_code=status_code,
        content=ProblemDetailsV1(
            title=titles.get(status_code, "HTTP Error"),
            status=status_code,
            code=code,
            detail="The request could not be completed safely.",
            instance=instance,
        ).model_dump(mode="json"),
        media_type="application/problem+json",
        headers=response_headers,
    )
