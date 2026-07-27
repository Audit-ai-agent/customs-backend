from __future__ import annotations

import hashlib
import json
import uuid
from collections.abc import Callable
from typing import Any

import httpx
from pydantic import ValidationError

from customs_backend.domain import (
    AgentAnswerResponse,
    AgentReviewRequest,
    AgentRevisionRequest,
    MAX_TEXT_DOCUMENT_UTF8_BYTES,
    canonical_json_bytes,
)
from customs_backend.ports import AgentPreExecutionRetryableError, AgentResponseRejectedError


class InMemoryObjectStore:
    def __init__(self) -> None:
        self._objects: dict[str, str] = {}

    def health(self) -> bool:
        return True

    def put_text(self, *, key: str, content: str, content_sha256: str) -> None:
        actual = hashlib.sha256(content.encode("utf-8")).hexdigest()
        if actual != content_sha256:
            raise ValueError("object content hash does not match metadata")
        previous = self._objects.get(key)
        if previous is not None and previous != content:
            raise ValueError("immutable object key already contains different content")
        self._objects[key] = content

    def get_text(self, *, key: str, expected_sha256: str) -> str:
        content = self._objects[key]
        actual = hashlib.sha256(content.encode("utf-8")).hexdigest()
        if actual != expected_sha256:
            raise ValueError("object readback hash mismatch")
        return content


class MinioObjectStore:
    def __init__(
        self,
        *,
        endpoint: str,
        access_key: str,
        secret_key: str,
        bucket: str,
        secure: bool,
    ) -> None:
        try:
            from minio import Minio
            from minio.error import S3Error
        except ImportError as exc:  # pragma: no cover - deployment dependency
            raise RuntimeError("install the production extra to use MinIO") from exc
        self._client = Minio(
            endpoint,
            access_key=access_key,
            secret_key=secret_key,
            secure=secure,
        )
        self._bucket = bucket
        if not self._client.bucket_exists(bucket):
            try:
                self._client.make_bucket(bucket)
            except S3Error as exc:
                # Backend and worker can start together after MinIO readiness.
                # A competing process may create the exact bucket between the
                # existence check and make_bucket; only that ownership-safe race
                # is accepted. All authorization/configuration failures propagate.
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
            raise ValueError("object is not valid UTF-8 text") from exc


class InMemoryJobPublisher:
    def __init__(self) -> None:
        self.messages: list[tuple[str, str, str]] = []
        self.assistant_messages: list[tuple[str, str, str]] = []

    def health(self) -> bool:
        return True

    def publish(self, *, org_id: str, job_id: str, outbox_id: str) -> None:
        self.messages.append((org_id, job_id, outbox_id))

    def publish_assistant(self, *, org_id: str, assistant_job_id: str, outbox_id: str) -> None:
        self.assistant_messages.append((org_id, assistant_job_id, outbox_id))


class DramatiqJobPublisher:
    def __init__(self, redis_url: str, broker: Any | None = None) -> None:
        try:
            import dramatiq
        except ImportError as exc:  # pragma: no cover - deployment dependency
            raise RuntimeError("install the production extra to use Dramatiq") from exc
        self._dramatiq = dramatiq
        if broker is None:
            from dramatiq.brokers.redis import RedisBroker

            broker = RedisBroker(url=redis_url)
            dramatiq.set_broker(broker)
        self._broker = broker

    def health(self) -> bool:
        client = getattr(self._broker, "client", None)
        return bool(client is not None and client.ping())

    def publish(self, *, org_id: str, job_id: str, outbox_id: str) -> None:
        message: Any = self._dramatiq.Message(
            queue_name="customs-review",
            actor_name="customs_backend.execute_review_job",
            args=(org_id, job_id, outbox_id),
            kwargs={},
            options={},
        )
        self._broker.enqueue(message)

    def publish_assistant(self, *, org_id: str, assistant_job_id: str, outbox_id: str) -> None:
        message: Any = self._dramatiq.Message(
            queue_name="customs-review",
            actor_name="customs_backend.execute_assistant_job",
            args=(org_id, assistant_job_id, outbox_id),
            kwargs={},
            options={},
        )
        self._broker.enqueue(message)


class HttpAgentClient:
    """Only server-side path to the private Agent; Browser never receives this token."""

    def __init__(
        self,
        *,
        base_url: str,
        service_token: str,
        timeout_seconds: float = 180.0,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        self._client = httpx.Client(
            base_url=base_url.rstrip("/"),
            timeout=httpx.Timeout(timeout_seconds, connect=10.0),
            headers={"Authorization": f"Bearer {service_token}"},
            follow_redirects=False,
            transport=transport,
        )

    @staticmethod
    def _decode_object(
        response: httpx.Response,
        *,
        maximum_bytes: int,
        response_name: str,
    ) -> dict[str, Any]:
        if response.headers.get("content-type", "").split(";", 1)[0].strip().casefold() != (
            "application/json"
        ):
            raise AgentResponseRejectedError(
                f"Agent {response_name} returned a non-JSON content type"
            )
        content_length = response.headers.get("content-length")
        if content_length is not None:
            try:
                if int(content_length) > maximum_bytes:
                    raise AgentResponseRejectedError(
                        f"Agent {response_name} exceeds the consumer safety limit"
                    )
            except ValueError as exc:
                raise AgentResponseRejectedError(
                    f"Agent {response_name} returned an invalid Content-Length"
                ) from exc
        payload = bytearray()
        for chunk in response.iter_bytes():
            payload.extend(chunk)
            if len(payload) > maximum_bytes:
                raise AgentResponseRejectedError(
                    f"Agent {response_name} exceeds the consumer safety limit"
                )

        def pairs(values: list[tuple[str, Any]]) -> dict[str, Any]:
            result: dict[str, Any] = {}
            for key, value in values:
                if key in result:
                    raise ValueError("duplicate JSON object key")
                result[key] = value
            return result

        try:
            value = json.loads(
                bytes(payload),
                object_pairs_hook=pairs,
                parse_constant=lambda value: (_ for _ in ()).throw(
                    ValueError(f"non-finite JSON number: {value}")
                ),
            )
        except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
            raise AgentResponseRejectedError(
                f"Agent {response_name} returned invalid canonical JSON"
            ) from exc
        if not isinstance(value, dict):
            raise AgentResponseRejectedError(
                f"Agent {response_name} returned a non-object response"
            )
        return value

    def _post(self, path: str, payload: dict[str, Any]) -> dict[str, Any]:
        encoded = canonical_json_bytes(payload)
        if len(encoded) > 2 * 1024 * 1024:
            raise ValueError("Agent request exceeds the 2 MiB transport limit")
        with self._client.stream(
            "POST",
            path,
            content=encoded,
            headers={"Content-Type": "application/json"},
        ) as response:
            if response.status_code == 503:
                error_response = self._decode_object(
                    response,
                    maximum_bytes=4_096,
                    response_name="error response",
                )
                if self._is_proven_pre_execution_rejection(response, error_response):
                    request_id = error_response["error"]["request_id"]
                    raise AgentPreExecutionRetryableError(request_id=request_id)
                raise AgentResponseRejectedError("Agent response returned HTTP 503")
            try:
                response.raise_for_status()
            except httpx.HTTPStatusError as exc:
                raise AgentResponseRejectedError(
                    f"Agent response returned HTTP {response.status_code}"
                ) from exc
            return self._decode_object(
                response,
                maximum_bytes=16_000_000,
                response_name="response",
            )

    def _post_ndjson(
        self,
        path: str,
        payload: dict[str, Any],
        *,
        on_event: Callable[[dict[str, Any]], None],
    ) -> dict[str, Any]:
        encoded = canonical_json_bytes(payload)
        if len(encoded) > 2 * 1024 * 1024:
            raise ValueError("Agent request exceeds the 2 MiB transport limit")
        with self._client.stream(
            "POST",
            path,
            content=encoded,
            headers={
                "Content-Type": "application/json",
                "Accept": "application/x-ndjson",
            },
        ) as response:
            if response.status_code == 503:
                error_response = self._decode_object(
                    response,
                    maximum_bytes=4_096,
                    response_name="stream error response",
                )
                if self._is_proven_pre_execution_rejection(response, error_response):
                    request_id = error_response["error"]["request_id"]
                    raise AgentPreExecutionRetryableError(request_id=request_id)
                raise AgentResponseRejectedError("Agent stream returned HTTP 503")
            try:
                response.raise_for_status()
            except httpx.HTTPStatusError as exc:
                raise AgentResponseRejectedError(
                    f"Agent stream returned HTTP {response.status_code}"
                ) from exc
            content_type = (
                response.headers.get("content-type", "").split(";", 1)[0].strip().casefold()
            )
            if content_type != "application/x-ndjson":
                raise AgentResponseRejectedError("Agent stream returned a non-NDJSON content type")
            total_bytes = 0
            terminal: dict[str, Any] | None = None
            seen_terminal = False
            pending = bytearray()

            def consume_line(encoded_line: bytes) -> None:
                nonlocal terminal
                nonlocal seen_terminal
                if encoded_line.endswith(b"\r"):
                    encoded_line = encoded_line[:-1]
                if len(encoded_line) > 4_000_000:
                    raise AgentResponseRejectedError("Agent stream exceeds the consumer limit")
                if not encoded_line:
                    return

                def pairs(values: list[tuple[str, Any]]) -> dict[str, Any]:
                    decoded: dict[str, Any] = {}
                    for key, value in values:
                        if key in decoded:
                            raise ValueError("duplicate JSON object key")
                        decoded[key] = value
                    return decoded

                try:
                    line = encoded_line.decode("utf-8")
                    item = json.loads(
                        line,
                        object_pairs_hook=pairs,
                        parse_constant=lambda value: (_ for _ in ()).throw(
                            ValueError(f"non-finite JSON number: {value}")
                        ),
                    )
                except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
                    raise AgentResponseRejectedError("Agent stream contains invalid JSON") from exc
                if not isinstance(item, dict) or set(item) != {
                    "type",
                    (
                        "event"
                        if item.get("type") == "stage_event"
                        else "result"
                        if item.get("type") == "result"
                        else "error"
                        if item.get("type") == "error"
                        else "invalid"
                    ),
                }:
                    raise AgentResponseRejectedError("Agent stream item shape is unsupported")
                item_type = item["type"]
                if seen_terminal:
                    raise AgentResponseRejectedError("Agent stream emitted data after its terminal")
                if item_type == "stage_event":
                    event = item["event"]
                    if not isinstance(event, dict):
                        raise AgentResponseRejectedError("Agent stream event is malformed")
                    on_event(event)
                    return
                seen_terminal = True
                if item_type == "error":
                    error = item["error"]
                    if (
                        not isinstance(error, dict)
                        or set(error) != {"code", "retryable"}
                        or not isinstance(error["code"], str)
                        or not isinstance(error["retryable"], bool)
                    ):
                        raise AgentResponseRejectedError("Agent stream error is malformed")
                    raise AgentResponseRejectedError(f"Agent stream failed with {error['code']}")
                terminal = item["result"]
                if not isinstance(terminal, dict):
                    raise AgentResponseRejectedError("Agent stream result is malformed")

            for chunk in response.iter_bytes(chunk_size=65_536):
                total_bytes += len(chunk)
                if total_bytes > 16_000_000:
                    raise AgentResponseRejectedError("Agent stream exceeds the consumer limit")
                pending.extend(chunk)
                while True:
                    newline = pending.find(b"\n")
                    if newline < 0:
                        if len(pending) > 4_000_000:
                            raise AgentResponseRejectedError(
                                "Agent stream line exceeds the consumer limit"
                            )
                        break
                    line_bytes = bytes(pending[:newline])
                    del pending[: newline + 1]
                    consume_line(line_bytes)
            if pending:
                consume_line(bytes(pending))
            if terminal is None:
                raise AgentResponseRejectedError("Agent stream ended without one result")
            return terminal

    @staticmethod
    def _is_proven_pre_execution_rejection(
        response: httpx.Response,
        payload: dict[str, Any],
    ) -> bool:
        if set(payload) != {"error"}:
            return False
        error = payload["error"]
        if not isinstance(error, dict) or set(error) != {
            "code",
            "message",
            "request_id",
            "retryable",
        }:
            return False
        if error["code"] != "dependency_unready" or error["retryable"] is not True:
            return False
        message = error["message"]
        request_id = error["request_id"]
        if message != "A required service dependency is not ready." or not isinstance(
            request_id, str
        ):
            return False
        try:
            if str(uuid.UUID(request_id)) != request_id:
                return False
        except ValueError:
            return False
        response_request_ids = response.headers.get_list("X-Request-ID")
        execution_states = response.headers.get_list("X-Agent-Execution-State")
        return response_request_ids == [request_id] and execution_states == ["not-started"]

    def review(self, request: AgentReviewRequest) -> dict[str, Any]:
        return self._post("/internal/agent/v2/reviews", request.to_agent_wire())

    def review_experience(self, request: AgentReviewRequest) -> dict[str, Any]:
        return self._post("/internal/agent/v3/reviews", request.to_agent_wire())

    def review_experience_stream(
        self,
        request: AgentReviewRequest,
        *,
        on_event: Callable[[dict[str, Any]], None],
    ) -> dict[str, Any]:
        return self._post_ndjson(
            "/internal/agent/v3/reviews/stream",
            request.to_agent_wire(),
            on_event=on_event,
        )

    def revise(self, request: AgentRevisionRequest) -> dict[str, Any]:
        return self._post("/internal/agent/v2/revisions", request.to_agent_wire())

    def answer(
        self,
        *,
        provider_result: dict[str, Any],
        question: str,
        question_id: str,
    ) -> AgentAnswerResponse:
        payload = self._post(
            "/internal/agent/v2/answers",
            {
                "question_id": question_id,
                "question": question,
                "locale": "ko-KR",
                "review_result": provider_result,
            },
        )
        try:
            return AgentAnswerResponse.model_validate(payload)
        except ValidationError as exc:
            raise AgentResponseRejectedError(
                "Agent answer violated the V3 response contract"
            ) from exc

    def answer_v2(self, payload: dict[str, Any]) -> dict[str, Any]:
        return self._post("/internal/agent/v3/answers", payload)

    def answer_v2_stream(
        self,
        payload: dict[str, Any],
        *,
        on_event: Callable[[dict[str, Any]], None],
    ) -> dict[str, Any]:
        return self._post_ndjson(
            "/internal/agent/v3/answers/stream",
            payload,
            on_event=on_event,
        )

    def metadata(self) -> dict[str, Any]:
        with self._client.stream("GET", "/internal/agent/v2/metadata") as response:
            try:
                response.raise_for_status()
            except httpx.HTTPStatusError as exc:
                raise AgentResponseRejectedError(
                    f"Agent metadata returned HTTP {response.status_code}"
                ) from exc
            return self._decode_object(
                response,
                maximum_bytes=1_000_000,
                response_name="metadata",
            )

    def experience_metadata(self) -> dict[str, Any]:
        with self._client.stream("GET", "/internal/agent/v3/metadata") as response:
            try:
                response.raise_for_status()
            except httpx.HTTPStatusError as exc:
                raise AgentResponseRejectedError(
                    f"Agent experience metadata returned HTTP {response.status_code}"
                ) from exc
            return self._decode_object(
                response,
                maximum_bytes=1_000_000,
                response_name="experience metadata",
            )

    def uat_readiness(self) -> dict[str, Any]:
        with self._client.stream("GET", "/health/uat-ready") as response:
            try:
                response.raise_for_status()
            except httpx.HTTPStatusError as exc:
                raise AgentResponseRejectedError(
                    f"Agent UAT readiness returned HTTP {response.status_code}"
                ) from exc
            return self._decode_object(
                response,
                maximum_bytes=1_000_000,
                response_name="UAT readiness",
            )

    def close(self) -> None:
        self._client.close()


class CallableAgentClient:
    """Test adapter that makes all external behavior explicit."""

    def __init__(
        self,
        review: Callable[[AgentReviewRequest], dict[str, Any]],
        *,
        revise: Callable[[AgentRevisionRequest], dict[str, Any]] | None = None,
        answer: Callable[[dict[str, Any], str, str], AgentAnswerResponse] | None = None,
        review_experience: Callable[[AgentReviewRequest], dict[str, Any]] | None = None,
        answer_v2: Callable[[dict[str, Any]], dict[str, Any]] | None = None,
        review_events: list[dict[str, Any]] | None = None,
        answer_events: list[dict[str, Any]] | None = None,
        metadata: dict[str, Any] | None = None,
        experience_metadata: dict[str, Any] | None = None,
        uat_readiness: dict[str, Any] | None = None,
    ) -> None:
        self._review = review
        self._revise = revise or (
            lambda request: {
                "parent_result_sha256": request.parent_result_sha256,
                "child_result": review(request.revised_request),
                "applied_decision_ids": [decision.decision_id for decision in request.decisions],
            }
        )
        self._answer = answer or (
            lambda _result, _question, question_id: AgentAnswerResponse(
                question_id=question_id,
                answer="근거가 충분하지 않습니다.",
                status="insufficient_evidence",
                generation_mode="deterministic_fallback",
            )
        )
        self._metadata = metadata or {"assurance_level": "diagnostic"}
        self._experience_metadata = experience_metadata or {
            "status": "unavailable",
        }
        self._uat_readiness = uat_readiness or {"status": "unavailable"}
        self._review_experience = review_experience
        self._answer_v2 = answer_v2
        self._review_events = list(review_events or [])
        self._answer_events = list(answer_events or [])

    def review(self, request: AgentReviewRequest) -> dict[str, Any]:
        return self._review(request)

    def revise(self, request: AgentRevisionRequest) -> dict[str, Any]:
        return self._revise(request)

    def review_experience(self, request: AgentReviewRequest) -> dict[str, Any]:
        if self._review_experience is None:
            raise RuntimeError("test Agent experience behavior was not configured")
        return self._review_experience(request)

    def review_experience_stream(
        self,
        request: AgentReviewRequest,
        *,
        on_event: Callable[[dict[str, Any]], None],
    ) -> dict[str, Any]:
        for event in self._review_events:
            on_event(dict(event))
        return self.review_experience(request)

    def answer(
        self,
        *,
        provider_result: dict[str, Any],
        question: str,
        question_id: str,
    ) -> AgentAnswerResponse:
        return self._answer(provider_result, question, question_id)

    def answer_v2(self, payload: dict[str, Any]) -> dict[str, Any]:
        if self._answer_v2 is None:
            raise RuntimeError("test Agent Chat V2 behavior was not configured")
        return self._answer_v2(payload)

    def answer_v2_stream(
        self,
        payload: dict[str, Any],
        *,
        on_event: Callable[[dict[str, Any]], None],
    ) -> dict[str, Any]:
        for event in self._answer_events:
            on_event(dict(event))
        return self.answer_v2(payload)

    def metadata(self) -> dict[str, Any]:
        return dict(self._metadata)

    def experience_metadata(self) -> dict[str, Any]:
        return dict(self._experience_metadata)

    def uat_readiness(self) -> dict[str, Any]:
        return dict(self._uat_readiness)
