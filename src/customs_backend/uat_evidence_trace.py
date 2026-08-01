from __future__ import annotations

import json
from datetime import date
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


_MAX_TRACE_RECORD_BYTES = 32 * 1024
_MAX_TRACE_FILE_BYTES = _MAX_TRACE_RECORD_BYTES * 100
_MAX_TRACE_RECORDS = 100
_TRACE_FILENAME = "three-law-scenario-eval-trace.jsonl"
_WINDOWS_CANONICAL_TRACE_SUFFIX = (
    "_local",
    "state",
    "runtime",
    "local-user-uat",
    _TRACE_FILENAME,
)
_CONTAINER_CANONICAL_TRACE_PARTS = (
    "/",
    "run",
    "local-user-uat",
    _TRACE_FILENAME,
)


class UatEvidenceTraceError(RuntimeError):
    pass


class _RawRetrievalStage(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    attempted: bool
    succeeded: bool
    attempt_count: int = Field(ge=0, le=1_000)
    result_count: int = Field(ge=0, le=10_000_000)
    top_k_result_count: int = Field(ge=0, le=10_000)
    duration_ms: int = Field(ge=0, le=300_000)

    @model_validator(mode="after")
    def successful_stage_is_consistent(self) -> "_RawRetrievalStage":
        if (
            not self.attempted
            or not self.succeeded
            or self.attempt_count < 1
            or self.top_k_result_count > self.result_count
        ):
            raise ValueError("scenario retrieval stage is incomplete")
        return self


class _RawRetrievalStages(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    structured: _RawRetrievalStage
    dense: _RawRetrievalStage
    bm25: _RawRetrievalStage
    graph: _RawRetrievalStage


class _RawProviderIdentity(BaseModel):
    """Exact safe identity projection emitted by the scenario-eval Data runner."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    provider_release: Literal["evidence-api-v1.2.0"]
    provider_revision: str = Field(pattern=r"^[0-9a-f]{40}$")
    provider_build_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    knowledge_generation_id: str = Field(pattern=r"^[0-9a-f]{64}$")
    knowledge_generation_attestation_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    knowledge_source_manifest_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    supported_review_as_of: str = Field(pattern=r"^\d{4}-\d{2}-\d{2}$")
    runtime_release_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    runtime_release_v3_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    requirement_policy_release: Literal["test-only-three-law-scenario-eval-human-only.v1"]
    requirement_policy_manifest_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    requirement_policy_approval_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    reviewer_trust_store_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")

    @model_validator(mode="after")
    def supported_review_date_is_valid(self) -> "_RawProviderIdentity":
        try:
            date.fromisoformat(self.supported_review_as_of)
        except ValueError:
            raise ValueError("supported_review_as_of must be a valid ISO date") from None
        return self


class _RawScenarioTrace(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    schema_version: Literal["three-law-evidence-v3-scenario-eval-trace/v1"]
    request_id: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
    http_status_code: Literal[200]
    evidence_profile: Literal["preserved-3law-v3-scenario-eval"]
    evidence_scope: Literal["ACTUAL_3_LAW_RAG_SCENARIO_EVAL_3_OF_58"]
    evidence_data_source: Literal["ACTUAL_POSTGRESQL_QDRANT_NEO4J_READ_ONLY"]
    evidence_policy_mode: Literal["SEALED_TEST_ONLY_ACTUAL_CCCT_HUMAN_ONLY"]
    knowledge_snapshot_id: str = Field(min_length=1, max_length=256)
    provider_identity: _RawProviderIdentity
    retrieval: _RawRetrievalStages


class UatEvidenceRetrievalStageResponse(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    attempted: Literal[True] = True
    succeeded: Literal[True] = True
    result_count: int = Field(ge=0, le=10_000_000)
    duration_ms: int = Field(ge=0, le=300_000)
    top_k_count: int = Field(ge=0, le=10_000)


class UatEvidenceRetrievalResponse(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    structured: UatEvidenceRetrievalStageResponse
    dense: UatEvidenceRetrievalStageResponse
    bm25: UatEvidenceRetrievalStageResponse
    graph: UatEvidenceRetrievalStageResponse


class UatEvidenceTraceResponse(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal["customs-backend-uat-evidence-trace/v1"] = (
        "customs-backend-uat-evidence-trace/v1"
    )
    request_id: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
    knowledge_snapshot_id: str = Field(min_length=1, max_length=256)
    retrieval: UatEvidenceRetrievalResponse


def _assert_external_trace_path(path: str | Path) -> Path:
    unresolved = Path(path).expanduser()
    if not unresolved.is_absolute() or unresolved.is_symlink():
        raise UatEvidenceTraceError("local UAT Evidence trace path is invalid")
    try:
        resolved = unresolved.resolve(strict=True)
    except OSError:
        raise UatEvidenceTraceError("local UAT Evidence trace is unavailable") from None
    parts = resolved.parts
    if not resolved.is_file() or not _is_allowed_trace_path_parts(parts):
        raise UatEvidenceTraceError("local UAT Evidence trace path is invalid")
    return resolved


def _is_allowed_trace_path_parts(parts: tuple[str, ...]) -> bool:
    has_windows_drive_root = (
        bool(parts) and len(parts[0]) == 3 and parts[0][0].isalpha() and parts[0][1:] == ":\\"
    )
    if has_windows_drive_root:
        normalized = tuple(part.casefold() for part in parts)
        return (
            len(normalized) >= len(_WINDOWS_CANONICAL_TRACE_SUFFIX)
            and normalized[-len(_WINDOWS_CANONICAL_TRACE_SUFFIX) :]
            == _WINDOWS_CANONICAL_TRACE_SUFFIX
        )
    return parts == _CONTAINER_CANONICAL_TRACE_PARTS


def _strict_trace_records(path: Path) -> list[_RawScenarioTrace]:
    try:
        raw = path.read_bytes()
    except OSError:
        raise UatEvidenceTraceError("local UAT Evidence trace is unavailable") from None
    if len(raw) > _MAX_TRACE_FILE_BYTES:
        raise UatEvidenceTraceError("local UAT Evidence trace exceeds its bounded size")
    if raw and not raw.endswith(b"\n"):
        raise UatEvidenceTraceError("local UAT Evidence trace has an incomplete record")
    lines = raw.splitlines()
    if len(lines) > _MAX_TRACE_RECORDS:
        raise UatEvidenceTraceError("local UAT Evidence trace has too many records")

    def unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, value in pairs:
            if key in result:
                raise ValueError("duplicate JSON key")
            result[key] = value
        return result

    records: list[_RawScenarioTrace] = []
    for line in lines:
        if not line or len(line) > _MAX_TRACE_RECORD_BYTES:
            raise UatEvidenceTraceError("local UAT Evidence trace record size is invalid")
        try:
            payload = json.loads(
                line.decode("utf-8"),
                object_pairs_hook=unique_object,
                parse_constant=lambda _value: (_ for _ in ()).throw(
                    ValueError("non-finite JSON number")
                ),
            )
            records.append(_RawScenarioTrace.model_validate(payload))
        except (UnicodeError, json.JSONDecodeError, ValueError, RecursionError):
            raise UatEvidenceTraceError("local UAT Evidence trace record is invalid") from None
    return records


class UatEvidenceTraceReader:
    """Read only the safe per-stage projection of a scenario-eval JSONL trace."""

    def __init__(
        self,
        path: str | Path,
        *,
        expected_runtime_v3_sha256: str,
    ) -> None:
        self._path = _assert_external_trace_path(path)
        self._expected_runtime_v3_sha256 = expected_runtime_v3_sha256
        # The file may be empty before the first Case, but its location and all
        # already-written records must be valid before Backend becomes ready.
        _strict_trace_records(self._path)

    def projection(
        self,
        *,
        request_id: str,
        knowledge_snapshot_id: str,
    ) -> UatEvidenceTraceResponse:
        path = _assert_external_trace_path(self._path)
        matches = [
            record for record in _strict_trace_records(path) if record.request_id == request_id
        ]
        if len(matches) != 1:
            raise UatEvidenceTraceError(
                "local UAT Evidence trace request identity is unavailable or duplicated"
            )
        record = matches[0]
        if (
            record.knowledge_snapshot_id != knowledge_snapshot_id
            or record.provider_identity.runtime_release_v3_sha256
            != self._expected_runtime_v3_sha256
        ):
            raise UatEvidenceTraceError("local UAT Evidence trace runtime identity differs")

        def stage(value: _RawRetrievalStage) -> UatEvidenceRetrievalStageResponse:
            return UatEvidenceRetrievalStageResponse(
                result_count=value.result_count,
                duration_ms=value.duration_ms,
                top_k_count=value.top_k_result_count,
            )

        return UatEvidenceTraceResponse(
            request_id=record.request_id,
            knowledge_snapshot_id=record.knowledge_snapshot_id,
            retrieval=UatEvidenceRetrievalResponse(
                structured=stage(record.retrieval.structured),
                dense=stage(record.retrieval.dense),
                bm25=stage(record.retrieval.bm25),
                graph=stage(record.retrieval.graph),
            ),
        )


__all__ = [
    "UatEvidenceRetrievalResponse",
    "UatEvidenceRetrievalStageResponse",
    "UatEvidenceTraceError",
    "UatEvidenceTraceReader",
    "UatEvidenceTraceResponse",
]
