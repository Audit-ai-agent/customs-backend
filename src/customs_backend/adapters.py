"""Local object stores and the direct Agent HTTP adapter."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable
from typing import Any

import httpx

from customs_backend.domain import AgentReviewRequest, MAX_TEXT_DOCUMENT_UTF8_BYTES, canonical_json_bytes
from customs_backend.ports import AgentResponseRejectedError


class InMemoryObjectStore:
    def __init__(self) -> None:
        self._objects: dict[str, str] = {}

    def health(self) -> bool:
        return True

    def put_text(self, *, key: str, content: str, content_sha256: str) -> None:
        if hashlib.sha256(content.encode("utf-8")).hexdigest() != content_sha256:
            raise ValueError("object content hash does not match metadata")
        existing = self._objects.get(key)
        if existing is not None and existing != content:
            raise ValueError("immutable object key already contains different content")
        self._objects[key] = content

    def get_text(self, *, key: str, expected_sha256: str) -> str:
        content = self._objects[key]
        if hashlib.sha256(content.encode("utf-8")).hexdigest() != expected_sha256:
            raise ValueError("object readback hash mismatch")
        return content


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
        from io import BytesIO

        payload = content.encode("utf-8")
        if hashlib.sha256(payload).hexdigest() != content_sha256:
            raise ValueError("object content hash does not match metadata")
        self._client.put_object(
            self._bucket,
            key,
            BytesIO(payload),
            length=len(payload),
            content_type="text/plain; charset=utf-8",
            metadata={"sha256": content_sha256},
        )

    def get_text(self, *, key: str, expected_sha256: str) -> str:
        response = self._client.get_object(self._bucket, key)
        try:
            content_length = response.headers.get("content-length")
            if content_length is not None and int(content_length) > MAX_TEXT_DOCUMENT_UTF8_BYTES:
                raise ValueError("object exceeds the text document byte safety limit")
            payload = response.read(MAX_TEXT_DOCUMENT_UTF8_BYTES + 1)
        finally:
            response.close()
            response.release_conn()
        if len(payload) > MAX_TEXT_DOCUMENT_UTF8_BYTES:
            raise ValueError("object exceeds the text document byte safety limit")
        if hashlib.sha256(payload).hexdigest() != expected_sha256:
            raise ValueError("object readback hash mismatch")
        try:
            return payload.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise ValueError("object is not valid UTF-8") from exc


class HttpAgentClient:
    """The only Backend-to-Agent transport."""

    def __init__(
        self,
        *,
        base_url: str,
        service_token: str,
        timeout_seconds: float = 180.0,
        service_mode: str | None = None,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        self._service_mode = service_mode
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

    def _require_service_mode(self, response: httpx.Response) -> None:
        if self._service_mode is not None and response.headers.get(
            "X-Customs-Service-Mode"
        ) != self._service_mode:
            raise AgentResponseRejectedError("Agent service mode differs from Backend mode")

    @staticmethod
    def _decode_object(response: httpx.Response, *, maximum_bytes: int, label: str) -> dict[str, Any]:
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
        encoded = canonical_json_bytes(request.to_agent_wire())
        if len(encoded) > 2 * 1024 * 1024:
            raise ValueError("Agent request exceeds the 2 MiB transport limit")
        response = self._client.post(
            "/internal/agent/v2/reviews", content=encoded, headers={"Content-Type": "application/json"}
        )
        self._require_service_mode(response)
        try:
            response.raise_for_status()
        except httpx.HTTPStatusError as exc:
            raise AgentResponseRejectedError(f"Agent review returned HTTP {response.status_code}") from exc
        return self._decode_object(response, maximum_bytes=16_000_000, label="review")

    def metadata(self) -> dict[str, Any]:
        response = self._client.get("/internal/agent/v2/metadata")
        self._require_service_mode(response)
        try:
            response.raise_for_status()
        except httpx.HTTPStatusError as exc:
            raise AgentResponseRejectedError(f"Agent metadata returned HTTP {response.status_code}") from exc
        return self._decode_object(response, maximum_bytes=1_000_000, label="metadata")

    def close(self) -> None:
        self._client.close()


class CallableAgentClient:
    """Explicit deterministic test and fixture adapter."""

    def __init__(
        self,
        review: Callable[[AgentReviewRequest], dict[str, Any]],
        *,
        metadata: dict[str, Any] | None = None,
    ) -> None:
        self._review = review
        self._metadata = metadata or {"status": "ready"}

    def review(self, request: AgentReviewRequest) -> dict[str, Any]:
        return self._review(request)

    def metadata(self) -> dict[str, Any]:
        return dict(self._metadata)
