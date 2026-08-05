"""The compact Browser-facing Backend API."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from typing import Annotated, Any, Literal

from fastapi import Depends, FastAPI, Header, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from pydantic import BaseModel, Field
from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from customs_backend.adapters import HttpAgentClient, InMemoryObjectStore
from customs_backend.agent_contract import BackendConfigurationError, validate_agent_contract_bundle
from customs_backend.auth import StaticTokenVerifier, principal_dependency, require_roles
from customs_backend.domain import (
    CaseCreate,
    CaseRecord,
    JobStatus,
    Principal,
    ReviewEnqueueRequest,
    ReviewRunRecord,
    Role,
    TextDocumentCreate,
)
from customs_backend.ports import (
    AgentClient,
    ConflictError,
    NotFoundError,
    ObjectStore,
    TokenVerifier,
    WorkbenchRepository,
)
from customs_backend.repository import InMemoryWorkbenchRepository
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


class SessionResponse(BaseModel):
    subject: str
    org_id: str
    roles: list[Role]


class DevelopmentSessionResponse(BaseModel):
    access_token: str = Field(min_length=32, max_length=512)
    token_type: Literal["Bearer"] = "Bearer"
    expires_in: int = Field(ge=60, le=86_400)
    session: SessionResponse


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

    record: PublicDocumentRecord
    content_text: str | None = None


class CaseListResponse(BaseModel):
    items: list[CaseRecord]


class DocumentListResponse(BaseModel):
    items: list[DocumentViewResponse]


class ReviewRunListResponse(BaseModel):
    items: list[ReviewRunRecord]


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
        response = _error_response(413, "request_body_too_large", instance=str(scope.get("path", "/")))
        await response(scope, receive, send)


@dataclass
class Container:
    settings: Settings
    repository: WorkbenchRepository
    object_store: ObjectStore
    agent: AgentClient
    token_verifier: TokenVerifier
    service: WorkbenchService


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
    agent: AgentClient = HttpAgentClient(
        base_url=settings.agent_base_url,
        service_token=settings.agent_service_token.get_secret_value(),
        timeout_seconds=settings.agent_timeout_seconds,
        service_mode=settings.service_mode,
    )
    if settings.environment == "local-development":
        if settings.dev_session_token is None:
            raise BackendConfigurationError("local development session token is unavailable")
        repository: WorkbenchRepository = InMemoryWorkbenchRepository()
        object_store: ObjectStore = InMemoryObjectStore()
        principal = Principal(
            subject=settings.dev_session_subject,
            org_id=settings.dev_session_org_id,
            roles=frozenset({Role.reviewer}),
        )
        verifier: TokenVerifier = StaticTokenVerifier(
            {settings.dev_session_token.get_secret_value(): principal}
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
            exc.status_code, codes.get(exc.status_code, "http_error"), instance=request.url.path,
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
        if not _verifier_ready(actual_container.token_verifier) or not agent_ready or not all(
            dependencies.values()
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
    def create_development_session(request: Request) -> DevelopmentSessionResponse:
        if actual_container.settings.environment != "local-development":
            raise HTTPException(status_code=404, detail="development session is unavailable")
        client = request.client
        if client is None or client.host not in {"127.0.0.1", "::1"}:
            raise HTTPException(status_code=404, detail="development session is unavailable")
        token = actual_container.settings.dev_session_token
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
    readers = require_roles(Role.reviewer)

    @app.post("/api/v1/cases", response_model=CaseRecord, status_code=201)
    def create_case_endpoint(
        body: CaseCreate,
        principal: Principal = Depends(writers),
        service: WorkbenchService = Depends(_service),
    ) -> CaseRecord:
        return service.create_case(principal, body)

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
        return DocumentViewResponse(
            record=DocumentViewResponse.PublicDocumentRecord.model_validate(record.model_dump(mode="json")),
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
        if actual_container.settings.environment == "functional-uat":
            required_as_of = actual_container.settings.functional_uat_as_of
            if body.review_as_of is not None and body.review_as_of != required_as_of:
                raise HTTPException(status_code=422, detail="functional UAT uses its configured review date")
            body = body.model_copy(update={"review_as_of": required_as_of})
        job, created = service.review(principal, case_id, body, idempotency_key)
        return JobResponse(job=JobView.model_validate(job.model_dump(mode="json")), idempotency_reused=not created)

    @app.get("/api/v1/jobs/{job_id}", response_model=JobView)
    def get_job_endpoint(
        job_id: str,
        principal: Principal = Depends(readers),
        service: WorkbenchService = Depends(_service),
    ) -> JobView:
        return JobView.model_validate(service.get_job(principal, job_id).model_dump(mode="json"))

    @app.get("/api/v1/cases/{case_id}/review-runs", response_model=ReviewRunListResponse)
    def list_runs_endpoint(
        case_id: str,
        principal: Principal = Depends(readers),
        service: WorkbenchService = Depends(_service),
    ) -> ReviewRunListResponse:
        return ReviewRunListResponse(items=service.list_runs(principal, case_id))

    @app.get("/api/v1/review-runs/{review_run_id}", response_model=ReviewRunRecord)
    def get_run_endpoint(
        review_run_id: str,
        principal: Principal = Depends(readers),
        service: WorkbenchService = Depends(_service),
    ) -> ReviewRunRecord:
        return service.get_run(principal, review_run_id)

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
