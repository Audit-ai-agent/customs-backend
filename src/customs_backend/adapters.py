"""Local object stores and the direct Agent HTTP adapter."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable
from typing import Any

import httpx
from pydantic import ValidationError

from customs_backend.agent_contract import AgentContractPayloadError, validate_agent_v3_payload
from customs_backend.domain import (
    AgentDocumentExtractionRequest,
    AgentRasterDocumentExtractionRequest,
    AgentRasterTextDocumentExtractionRequest,
    AgentAssistantAnswerRequest,
    AgentKnowledgeAnswerRequest,
    AgentReviewRequest,
    MAX_DOCUMENT_ASSET_BYTES,
    MAX_RASTER_VIEWER_PAGE_BYTES,
    RasterDocumentPageRender,
    RasterDocumentPreviewStatus,
    RasterTextDocumentPreviewStatus,
    MAX_TEXT_DOCUMENT_UTF8_BYTES,
    canonical_json_bytes,
)
from customs_backend.ports import AgentResponseRejectedError, KnowledgeInputRejectedError
from customs_backend.raster_limits import (
    RASTER_AGENT_EXECUTION_TIMEOUT_SECONDS,
    RASTER_PAGE_RENDER_TIMEOUT_SECONDS,
    RASTER_PROFILE_STATUS_TIMEOUT_SECONDS,
)


class InMemoryObjectStore:
    def __init__(self) -> None:
        self._objects: dict[str, bytes] = {}

    def health(self) -> bool:
        return True

    def put_text(self, *, key: str, content: str, content_sha256: str) -> None:
        self.put_bytes(
            key=key,
            content=content.encode("utf-8"),
            content_sha256=content_sha256,
            media_type="text/plain; charset=utf-8",
        )

    def get_text(self, *, key: str, expected_sha256: str) -> str:
        payload = self.get_bytes(
            key=key,
            expected_sha256=expected_sha256,
            maximum_bytes=MAX_TEXT_DOCUMENT_UTF8_BYTES,
        )
        try:
            return payload.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise ValueError("object is not valid UTF-8") from exc

    def put_bytes(
        self, *, key: str, content: bytes, content_sha256: str, media_type: str
    ) -> None:
        del media_type  # The test adapter has no content-type metadata surface.
        if hashlib.sha256(content).hexdigest() != content_sha256:
            raise ValueError("object content hash does not match metadata")
        existing = self._objects.get(key)
        if existing is not None and existing != content:
            raise ValueError("immutable object key already contains different content")
        self._objects[key] = bytes(content)

    def get_bytes(self, *, key: str, expected_sha256: str, maximum_bytes: int) -> bytes:
        content = self._objects[key]
        if len(content) > maximum_bytes:
            raise ValueError("object exceeds the requested byte safety limit")
        if hashlib.sha256(content).hexdigest() != expected_sha256:
            raise ValueError("object readback hash mismatch")
        return bytes(content)


class MinioObjectStore:
    """Functional-UAT object store; MinIO remains an optional dependency."""

    def __init__(
        self, *, endpoint: str, access_key: str, secret_key: str, bucket: str, secure: bool
    ) -> None:
        try:
            from minio import Minio
            from minio.error import S3Error
        except ImportError as exc:  # pragma: no cover - optional install path
            raise RuntimeError("install the functional-uat extra to use MinIO") from exc
        self._client = Minio(endpoint, access_key=access_key, secret_key=secret_key, secure=secure)
        self._bucket = bucket
        if not self._client.bucket_exists(bucket):
            try:
                self._client.make_bucket(bucket)
            except S3Error as exc:
                if exc.code != "BucketAlreadyOwnedByYou":
                    raise

    def health(self) -> bool:
        return bool(self._client.bucket_exists(self._bucket))

    def put_text(self, *, key: str, content: str, content_sha256: str) -> None:
        payload = content.encode("utf-8")
        self.put_bytes(
            key=key,
            content=payload,
            content_sha256=content_sha256,
            media_type="text/plain; charset=utf-8",
        )

    def put_bytes(
        self, *, key: str, content: bytes, content_sha256: str, media_type: str
    ) -> None:
        from io import BytesIO

        if hashlib.sha256(content).hexdigest() != content_sha256:
            raise ValueError("object content hash does not match metadata")
        self._client.put_object(
            self._bucket,
            key,
            BytesIO(content),
            length=len(content),
            content_type=media_type,
            metadata={"sha256": content_sha256},
        )

    def get_text(self, *, key: str, expected_sha256: str) -> str:
        payload = self.get_bytes(
            key=key,
            expected_sha256=expected_sha256,
            maximum_bytes=MAX_TEXT_DOCUMENT_UTF8_BYTES,
        )
        try:
            return payload.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise ValueError("object is not valid UTF-8") from exc

    def get_bytes(self, *, key: str, expected_sha256: str, maximum_bytes: int) -> bytes:
        response = self._client.get_object(self._bucket, key)
        try:
            content_length = response.headers.get("content-length")
            if content_length is not None and int(content_length) > maximum_bytes:
                raise ValueError("object exceeds the requested byte safety limit")
            payload = response.read(maximum_bytes + 1)
        finally:
            response.close()
            response.release_conn()
        if len(payload) > maximum_bytes:
            raise ValueError("object exceeds the requested byte safety limit")
        if hashlib.sha256(payload).hexdigest() != expected_sha256:
            raise ValueError("object readback hash mismatch")
        return payload


class HttpAgentClient:
    """The only Backend-to-Agent transport."""

    def __init__(
        self,
        *,
        base_url: str,
        service_token: str,
        timeout_seconds: float = 180.0,
        service_mode: str | None = None,
        review_trace: Callable[[str, str, dict[str, Any]], None] | None = None,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        self._service_mode = service_mode
        self._review_trace = review_trace
        headers = {"Authorization": f"Bearer {service_token}"}
        if service_mode is not None:
            headers["X-Customs-Service-Mode"] = service_mode
        self._client = httpx.Client(
            base_url=base_url.rstrip("/"),
            timeout=httpx.Timeout(timeout_seconds, connect=10.0),
            headers=headers,
            follow_redirects=False,
            transport=transport,
        )

    def _trace_review(self, review_run_id: str, event: str, payload: dict[str, Any]) -> None:
        if self._review_trace is not None:
            self._review_trace(review_run_id, event, payload)

    def _require_service_mode(self, response: httpx.Response) -> None:
        if (
            self._service_mode is not None
            and response.headers.get("X-Customs-Service-Mode") != self._service_mode
        ):
            raise AgentResponseRejectedError("Agent service mode differs from Backend mode")

    @staticmethod
    def _decode_object(
        response: httpx.Response, *, maximum_bytes: int, label: str
    ) -> dict[str, Any]:
        content_type = response.headers.get("content-type", "").split(";", 1)[0].strip().casefold()
        if content_type != "application/json":
            raise AgentResponseRejectedError(f"Agent {label} returned a non-JSON content type")
        content = response.content
        if len(content) > maximum_bytes:
            raise AgentResponseRejectedError(f"Agent {label} exceeds the consumer safety limit")

        def pairs(values: list[tuple[str, Any]]) -> dict[str, Any]:
            decoded: dict[str, Any] = {}
            for key, value in values:
                if key in decoded:
                    raise ValueError("duplicate JSON object key")
                decoded[key] = value
            return decoded

        try:
            value = json.loads(
                content,
                object_pairs_hook=pairs,
                parse_constant=lambda value: (_ for _ in ()).throw(
                    ValueError(f"non-finite JSON number: {value}")
                ),
            )
        except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
            raise AgentResponseRejectedError(f"Agent {label} returned invalid JSON") from exc
        if not isinstance(value, dict):
            raise AgentResponseRejectedError(f"Agent {label} returned a non-object response")
        return value

    def review(self, request: AgentReviewRequest) -> dict[str, Any]:
        wire = request.to_agent_wire()
        encoded = canonical_json_bytes(wire)
        if len(encoded) > 2 * 1024 * 1024:
            raise ValueError("Agent request exceeds the 2 MiB transport limit")
        request_payload = {
            "method": "POST",
            "path": "/internal/agent/v2/reviews",
            "request_body": wire,
        }
        self._trace_review(request.review_run_id, "review_request", request_payload)
        try:
            response = self._client.post(
                "/internal/agent/v2/reviews",
                content=encoded,
                headers={"Content-Type": "application/json"},
            )
        except httpx.HTTPError as exc:
            self._trace_review(
                request.review_run_id,
                "review_failure",
                {**request_payload, "error_code": type(exc).__name__},
            )
            raise
        try:
            self._require_service_mode(response)
            response.raise_for_status()
            result = self._decode_object(response, maximum_bytes=16_000_000, label="review")
        except (AgentResponseRejectedError, httpx.HTTPStatusError) as exc:
            self._trace_review(
                request.review_run_id,
                "review_failure",
                {
                    **request_payload,
                    "response_status": response.status_code,
                    "error_code": type(exc).__name__,
                },
            )
            if isinstance(exc, httpx.HTTPStatusError):
                raise AgentResponseRejectedError(
                    f"Agent review returned HTTP {response.status_code}"
                ) from exc
            raise
        self._trace_review(
            request.review_run_id,
            "review_response",
            {**request_payload, "response_status": response.status_code, "response_body": result},
        )
        return result

    def extract_document(
        self, request: AgentDocumentExtractionRequest, content: bytes
    ) -> dict[str, Any]:
        """Call the isolated binary-preview endpoint without tracing source bytes."""

        if len(content) != request.byte_size or len(content) > MAX_DOCUMENT_ASSET_BYTES:
            raise ValueError("document extraction source bytes do not match the declared size")
        if hashlib.sha256(content).hexdigest() != request.source_sha256:
            raise ValueError("document extraction source bytes do not match the declared hash")
        try:
            response = self._client.post(
                "/internal/agent/v2/document-extractions",
                content=content,
                headers={
                    "Content-Type": request.media_type,
                    "X-Document-Asset-ID": request.document_asset_id,
                    "X-Extraction-Request-ID": request.extraction_request_id,
                    "X-Document-SHA256": request.source_sha256,
                },
            )
        except httpx.HTTPError as exc:
            raise AgentResponseRejectedError("Agent document extraction transport failed") from exc
        try:
            self._require_service_mode(response)
            response.raise_for_status()
            return self._decode_object(
                response, maximum_bytes=8_000_000, label="document extraction"
            )
        except (AgentResponseRejectedError, httpx.HTTPStatusError) as exc:
            if isinstance(exc, httpx.HTTPStatusError):
                raise AgentResponseRejectedError(
                    f"Agent document extraction returned HTTP {response.status_code}"
                ) from exc
            raise

    def extract_raster_document(
        self, request: AgentRasterDocumentExtractionRequest, content: bytes
    ) -> dict[str, Any]:
        """Call the v3 raster OCR/layout boundary without tracing source bytes.

        Raster work is deliberately isolated from the v1 text-layer preview.
        Its transport deadline is a fixed 60 seconds, separately aligned with
        the raster worker lease; neither raw source bytes nor the provider
        response body enter traces.
        """

        if len(content) != request.byte_size or len(content) > MAX_DOCUMENT_ASSET_BYTES:
            raise ValueError("raster extraction source bytes do not match the declared size")
        if hashlib.sha256(content).hexdigest() != request.source_sha256:
            raise ValueError("raster extraction source bytes do not match the declared hash")
        try:
            response = self._client.post(
                "/internal/agent/v3/raster-document-extractions",
                content=content,
                headers={
                    "Content-Type": request.media_type,
                    "X-Document-Asset-ID": request.document_asset_id,
                    "X-Extraction-Request-ID": request.extraction_request_id,
                    "X-Document-SHA256": request.source_sha256,
                    "X-Raster-Profile-Revision": request.raster_profile_revision,
                },
                timeout=RASTER_AGENT_EXECUTION_TIMEOUT_SECONDS,
            )
        except httpx.HTTPError as exc:
            raise AgentResponseRejectedError("Agent raster extraction transport failed") from exc
        try:
            self._require_service_mode(response)
            response.raise_for_status()
            return self._decode_object(
                response, maximum_bytes=16_000_000, label="raster document extraction"
            )
        except (AgentResponseRejectedError, httpx.HTTPStatusError) as exc:
            if isinstance(exc, httpx.HTTPStatusError):
                raise AgentResponseRejectedError(
                    f"Agent raster extraction returned HTTP {response.status_code}"
                ) from exc
            raise

    def extract_raster_text_document(
        self, request: AgentRasterTextDocumentExtractionRequest, content: bytes
    ) -> dict[str, Any]:
        """Call the independent v3 text-only raster OCR boundary.

        It has the same fixed bounded transport as structured raster OCR, but
        a distinct profile header and endpoint.  Raw source bytes and OCR text
        never enter request tracing or adapter error details.
        """

        if len(content) != request.byte_size or len(content) > MAX_DOCUMENT_ASSET_BYTES:
            raise ValueError("raster text extraction source bytes do not match the declared size")
        if hashlib.sha256(content).hexdigest() != request.source_sha256:
            raise ValueError("raster text extraction source bytes do not match the declared hash")
        try:
            response = self._client.post(
                "/internal/agent/v3/raster-text-extractions",
                content=content,
                headers={
                    "Content-Type": request.media_type,
                    "X-Document-Asset-ID": request.document_asset_id,
                    "X-Extraction-Request-ID": request.extraction_request_id,
                    "X-Document-SHA256": request.source_sha256,
                    "X-Raster-Text-Profile-Revision": request.raster_text_profile_revision,
                },
                timeout=RASTER_AGENT_EXECUTION_TIMEOUT_SECONDS,
            )
        except httpx.HTTPError as exc:
            raise AgentResponseRejectedError("Agent raster text extraction transport failed") from exc
        try:
            self._require_service_mode(response)
            response.raise_for_status()
            return self._decode_object(
                response, maximum_bytes=4_000_000, label="raster text extraction"
            )
        except (AgentResponseRejectedError, httpx.HTTPStatusError) as exc:
            if isinstance(exc, httpx.HTTPStatusError):
                raise AgentResponseRejectedError(
                    f"Agent raster text extraction returned HTTP {response.status_code}"
                ) from exc
            raise

    def raster_document_preview_status(self) -> RasterDocumentPreviewStatus:
        """Read one small, authenticated activation snapshot from Agent.

        New jobs must bind to this exact revision.  The endpoint is deliberately
        separate from OCR execution so Backend never guesses a profile or
        reuses the historical hard-coded revision.
        """

        try:
            response = self._client.get(
                "/internal/agent/v3/raster-document-preview-status",
                timeout=RASTER_PROFILE_STATUS_TIMEOUT_SECONDS,
            )
        except httpx.HTTPError as exc:
            raise AgentResponseRejectedError("Agent raster preview status transport failed") from exc
        try:
            self._require_service_mode(response)
            response.raise_for_status()
            raw = self._decode_object(
                response, maximum_bytes=16_384, label="raster document preview status"
            )
            validate_agent_v3_payload("raster_document_preview_status", raw)
            return RasterDocumentPreviewStatus.model_validate(raw)
        except (AgentContractPayloadError, ValidationError, AgentResponseRejectedError, httpx.HTTPStatusError) as exc:
            if isinstance(exc, httpx.HTTPStatusError):
                raise AgentResponseRejectedError(
                    f"Agent raster preview status returned HTTP {response.status_code}"
                ) from exc
            raise AgentResponseRejectedError("Agent raster preview status is invalid") from exc

    def raster_text_document_preview_status(self) -> RasterTextDocumentPreviewStatus:
        """Read one small, authenticated text-only profile snapshot."""

        try:
            response = self._client.get(
                "/internal/agent/v3/raster-text-preview-status",
                timeout=RASTER_PROFILE_STATUS_TIMEOUT_SECONDS,
            )
        except httpx.HTTPError as exc:
            raise AgentResponseRejectedError("Agent raster text preview status transport failed") from exc
        try:
            self._require_service_mode(response)
            response.raise_for_status()
            raw = self._decode_object(
                response, maximum_bytes=16_384, label="raster text preview status"
            )
            validate_agent_v3_payload("raster_text_preview_status", raw)
            return RasterTextDocumentPreviewStatus.model_validate(raw)
        except (
            AgentContractPayloadError,
            ValidationError,
            AgentResponseRejectedError,
            httpx.HTTPStatusError,
        ) as exc:
            if isinstance(exc, httpx.HTTPStatusError):
                raise AgentResponseRejectedError(
                    f"Agent raster text preview status returned HTTP {response.status_code}"
                ) from exc
            raise AgentResponseRejectedError("Agent raster text preview status is invalid") from exc

    def render_raster_document_page(
        self,
        request: AgentRasterDocumentExtractionRequest,
        content: bytes,
        page_number: int,
    ) -> RasterDocumentPageRender:
        """Request one approved PDFium page without tracing source/page bytes."""

        if (
            request.media_type != "application/pdf"
            or page_number < 1
            or page_number > 20
            or len(content) != request.byte_size
            or len(content) > MAX_DOCUMENT_ASSET_BYTES
            or hashlib.sha256(content).hexdigest() != request.source_sha256
        ):
            raise ValueError("raster page source binding is invalid")
        try:
            response = self._client.post(
                f"/internal/agent/v3/raster-document-pages/{page_number}",
                content=content,
                headers={
                    "Content-Type": "application/pdf",
                    "X-Document-Asset-ID": request.document_asset_id,
                    "X-Extraction-Request-ID": request.extraction_request_id,
                    "X-Document-SHA256": request.source_sha256,
                    "X-Raster-Profile-Revision": request.raster_profile_revision,
                },
                timeout=RASTER_PAGE_RENDER_TIMEOUT_SECONDS,
            )
        except httpx.HTTPError as exc:
            raise AgentResponseRejectedError("Agent raster page transport failed") from exc
        try:
            self._require_service_mode(response)
            response.raise_for_status()
            content_type = response.headers.get("content-type", "").split(";", 1)[0].strip()
            if content_type.casefold() != "image/png":
                raise AgentResponseRejectedError("Agent raster page returned a non-PNG content type")
            page_content = response.content
            if not page_content or len(page_content) > MAX_RASTER_VIEWER_PAGE_BYTES:
                raise AgentResponseRejectedError("Agent raster page exceeds the consumer safety limit")

            def required_header(name: str) -> str:
                values = response.headers.get_list(name)
                if len(values) != 1 or not values[0]:
                    raise AgentResponseRejectedError("Agent raster page response binding is invalid")
                return values[0]

            result = RasterDocumentPageRender.model_validate(
                {
                    "page_number": required_header("X-Raster-Page-Number"),
                    "width": required_header("X-Raster-Page-Width"),
                    "height": required_header("X-Raster-Page-Height"),
                    "media_type": "image/png",
                    "content": page_content,
                    "content_sha256": required_header("X-Raster-Page-SHA256"),
                    "source_sha256": required_header("X-Document-SHA256"),
                    "raster_profile_revision": required_header("X-Raster-Profile-Revision"),
                    "renderer_revision": required_header("X-Raster-Renderer-Revision"),
                }
            )
        except (AgentResponseRejectedError, ValueError, httpx.HTTPStatusError) as exc:
            if isinstance(exc, httpx.HTTPStatusError):
                raise AgentResponseRejectedError(
                    f"Agent raster page returned HTTP {response.status_code}"
                ) from exc
            if isinstance(exc, ValueError):
                raise AgentResponseRejectedError("Agent raster page response binding is invalid") from exc
            raise
        if (
            result.page_number != page_number
            or result.source_sha256 != request.source_sha256
            or result.raster_profile_revision != request.raster_profile_revision
        ):
            raise AgentResponseRejectedError("Agent raster page provenance does not match")
        return result

    def answer(self, request: AgentAssistantAnswerRequest) -> dict[str, Any]:
        """Call the one bounded, synchronous assistant endpoint."""

        wire = request.to_agent_wire()
        encoded = canonical_json_bytes(wire)
        if len(encoded) > 4 * 1024 * 1024:
            raise ValueError("Agent assistant request exceeds the 4 MiB transport limit")
        request_payload = {
            "method": "POST",
            "path": "/internal/agent/v2/assistant/answers",
            "request_body": wire,
        }
        self._trace_review(request.review_run_id, "assistant_request", request_payload)
        try:
            response = self._client.post(
                "/internal/agent/v2/assistant/answers",
                content=encoded,
                headers={"Content-Type": "application/json"},
            )
        except httpx.HTTPError as exc:
            self._trace_review(
                request.review_run_id,
                "assistant_failure",
                {**request_payload, "error_code": type(exc).__name__},
            )
            raise AgentResponseRejectedError("Agent assistant transport failed") from exc
        try:
            self._require_service_mode(response)
            response.raise_for_status()
            result = self._decode_object(
                response, maximum_bytes=1_000_000, label="assistant answer"
            )
        except (AgentResponseRejectedError, httpx.HTTPStatusError) as exc:
            self._trace_review(
                request.review_run_id,
                "assistant_failure",
                {
                    **request_payload,
                    "response_status": response.status_code,
                    "error_code": type(exc).__name__,
                },
            )
            if isinstance(exc, httpx.HTTPStatusError):
                raise AgentResponseRejectedError(
                    f"Agent assistant answer returned HTTP {response.status_code}"
                ) from exc
            raise
        self._trace_review(
            request.review_run_id,
            "assistant_response",
            {**request_payload, "response_status": response.status_code, "response_body": result},
        )
        return result

    def answer_knowledge(self, request: AgentKnowledgeAnswerRequest) -> dict[str, Any]:
        """Call the bounded standalone official-trade guidance endpoint.

        Unlike a case assistant turn there is no Review Run to attach to a
        Functional-UAT transcript.  The method therefore deliberately does
        not reuse the review trace callback or retain request/response bodies.
        """

        wire = request.to_agent_wire()
        encoded = canonical_json_bytes(wire)
        if len(encoded) > 1_000_000:
            raise ValueError("Agent knowledge request exceeds the 1 MiB transport limit")
        try:
            response = self._client.post(
                "/internal/agent/v2/knowledge/answers",
                content=encoded,
                headers={"Content-Type": "application/json"},
            )
        except httpx.HTTPError as exc:
            raise AgentResponseRejectedError("Agent knowledge transport failed") from exc
        try:
            self._require_service_mode(response)
            # Agent's independent ingress guard deliberately returns a
            # generic 422.  Preserve that safe outcome for the public
            # knowledge boundary without reading or exposing its body.
            if response.status_code == 422:
                raise KnowledgeInputRejectedError()
            response.raise_for_status()
            return self._decode_object(response, maximum_bytes=1_000_000, label="knowledge answer")
        except KnowledgeInputRejectedError:
            raise
        except (AgentResponseRejectedError, httpx.HTTPStatusError) as exc:
            if isinstance(exc, httpx.HTTPStatusError):
                raise AgentResponseRejectedError(
                    f"Agent knowledge answer returned HTTP {response.status_code}"
                ) from exc
            raise

    def metadata(self) -> dict[str, Any]:
        response = self._client.get("/internal/agent/v2/metadata")
        self._require_service_mode(response)
        try:
            response.raise_for_status()
        except httpx.HTTPStatusError as exc:
            raise AgentResponseRejectedError(
                f"Agent metadata returned HTTP {response.status_code}"
            ) from exc
        return self._decode_object(response, maximum_bytes=1_000_000, label="metadata")

    def close(self) -> None:
        self._client.close()


class CallableAgentClient:
    """Explicit deterministic test and fixture adapter."""

    def __init__(
        self,
        review: Callable[[AgentReviewRequest], dict[str, Any]],
        *,
        answer: Callable[[AgentAssistantAnswerRequest], dict[str, Any]] | None = None,
        knowledge_answer: Callable[[AgentKnowledgeAnswerRequest], dict[str, Any]] | None = None,
        extract_document: Callable[[AgentDocumentExtractionRequest, bytes], dict[str, Any]] | None = None,
        extract_raster_document: Callable[
            [AgentRasterDocumentExtractionRequest, bytes], dict[str, Any]
        ]
        | None = None,
        extract_raster_text_document: Callable[
            [AgentRasterTextDocumentExtractionRequest, bytes], dict[str, Any]
        ]
        | None = None,
        render_raster_document_page: Callable[
            [AgentRasterDocumentExtractionRequest, bytes, int], RasterDocumentPageRender
        ]
        | None = None,
        raster_preview_status: Callable[[], dict[str, Any]] | None = None,
        raster_text_preview_status: Callable[[], dict[str, Any]] | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> None:
        self._review = review
        self._answer = answer
        self._knowledge_answer = knowledge_answer
        self._extract_document = extract_document
        self._extract_raster_document = extract_raster_document
        self._extract_raster_text_document = extract_raster_text_document
        self._render_raster_document_page = render_raster_document_page
        self._raster_preview_status = raster_preview_status
        self._raster_text_preview_status = raster_text_preview_status
        self._metadata = metadata or {"status": "ready"}

    def review(self, request: AgentReviewRequest) -> dict[str, Any]:
        return self._review(request)

    def extract_document(
        self, request: AgentDocumentExtractionRequest, content: bytes
    ) -> dict[str, Any]:
        if self._extract_document is not None:
            return self._extract_document(request, content)
        return {
            "schema_version": "1.0",
            "document_asset_id": request.document_asset_id,
            "extraction_request_id": request.extraction_request_id,
            "status": "unavailable",
            "media_type": request.media_type,
            "byte_size": request.byte_size,
            "source_sha256": request.source_sha256,
            "canonical_text_sha256": None,
            "extractor_revision": None,
            "pages": [],
            "warnings": [{"code": "preview_engine_unavailable"}],
        }

    def extract_raster_document(
        self, request: AgentRasterDocumentExtractionRequest, content: bytes
    ) -> dict[str, Any]:
        if self._extract_raster_document is not None:
            return self._extract_raster_document(request, content)
        return {
            "schema_version": "2.0",
            "document_asset_id": request.document_asset_id,
            "extraction_request_id": request.extraction_request_id,
            "media_type": request.media_type,
            "byte_size": request.byte_size,
            "source_sha256": request.source_sha256,
            "raster_profile_revision": request.raster_profile_revision,
            "status": "unavailable",
            "canonical_text_sha256": None,
            "extractor_revision": None,
            "document_type": "unknown",
            "classification_confidence": None,
            "pages": [],
            "field_candidates": [],
            "tables": [],
            "warnings": [{"code": "raster_engine_unavailable"}],
            "needs_review_reasons": [{"code": "raster_engine_unavailable"}],
        }

    def extract_raster_text_document(
        self, request: AgentRasterTextDocumentExtractionRequest, content: bytes
    ) -> dict[str, Any]:
        if self._extract_raster_text_document is not None:
            return self._extract_raster_text_document(request, content)
        return {
            "schema_version": "1.0",
            "document_asset_id": request.document_asset_id,
            "extraction_request_id": request.extraction_request_id,
            "media_type": request.media_type,
            "byte_size": request.byte_size,
            "source_sha256": request.source_sha256,
            "raster_text_profile_revision": request.raster_text_profile_revision,
            "status": "unavailable",
            "canonical_text": None,
            "canonical_text_sha256": None,
            "extractor_revision": None,
            "pages": [],
            "warnings": [{"code": "raster_text_engine_unavailable"}],
            "needs_review_reasons": [{"code": "raster_text_engine_unavailable"}],
        }

    def raster_document_preview_status(self) -> RasterDocumentPreviewStatus:
        """Fixture default retains the formerly enabled test profile only.

        Production uses ``HttpAgentClient`` and therefore the real activation
        status.  Tests can supply a mutable callback to model an activation
        replacement or pre-activation unavailability.
        """

        raw = (
            self._raster_preview_status()
            if self._raster_preview_status is not None
            else {
                "schema_version": "1.0",
                "availability": "available",
                "raster_profile_revision": "synthetic-raster-v1",
            }
        )
        try:
            validate_agent_v3_payload("raster_document_preview_status", raw)
            return RasterDocumentPreviewStatus.model_validate(raw)
        except (AgentContractPayloadError, ValidationError, ValueError) as exc:
            raise AgentResponseRejectedError("deterministic raster preview status is invalid") from exc

    def raster_text_document_preview_status(self) -> RasterTextDocumentPreviewStatus:
        raw = (
            self._raster_text_preview_status()
            if self._raster_text_preview_status is not None
            else {
                "schema_version": "1.0",
                "availability": "unavailable",
                "raster_text_profile_revision": None,
            }
        )
        try:
            validate_agent_v3_payload("raster_text_preview_status", raw)
            return RasterTextDocumentPreviewStatus.model_validate(raw)
        except (AgentContractPayloadError, ValidationError, ValueError) as exc:
            raise AgentResponseRejectedError("deterministic raster text preview status is invalid") from exc

    def render_raster_document_page(
        self,
        request: AgentRasterDocumentExtractionRequest,
        content: bytes,
        page_number: int,
    ) -> RasterDocumentPageRender:
        if self._render_raster_document_page is None:
            raise AgentResponseRejectedError("deterministic raster page renderer is unavailable")
        return self._render_raster_document_page(request, content, page_number)

    def answer(self, request: AgentAssistantAnswerRequest) -> dict[str, Any]:
        if self._answer is not None:
            return self._answer(request)
        return {
            "schema_version": "1.0",
            "case_id": request.case_id,
            "review_run_id": request.review_run_id,
            "conversation_id": request.conversation_id,
            "answer_scope": "clarification_needed",
            "direct_answer": "Deterministic test assistant response.",
            "citations": [],
            "suggested_questions": [],
            "correction_draft": None,
            "engine": {"kind": "deterministic", "model_id": "backend-fixture"},
        }

    def answer_knowledge(self, request: AgentKnowledgeAnswerRequest) -> dict[str, Any]:
        if self._knowledge_answer is not None:
            return self._knowledge_answer(request)
        return {
            "schema_version": "1.0",
            "conversation_id": request.conversation_id,
            "answer_scope": "clarification_needed",
            "direct_answer": "질문의 통관 단계 또는 품목을 조금 더 구체적으로 알려 주세요.",
            "citations": [],
            "answer_sections": [],
            "suggested_questions": [],
            "coverage": "unavailable",
            "snapshot_id": None,
            "as_of": request.as_of.isoformat(),
            "engine": {"kind": "deterministic", "model_id": "backend-fixture"},
        }

    def metadata(self) -> dict[str, Any]:
        return dict(self._metadata)
