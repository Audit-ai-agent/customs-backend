from __future__ import annotations

import json
from pathlib import Path, PurePosixPath, PureWindowsPath
from typing import Any

import pytest

import customs_backend.uat_evidence_trace as trace_module
from customs_backend.uat_evidence_trace import (
    UatEvidenceTraceError,
    UatEvidenceTraceReader,
    _is_allowed_trace_path_parts,
)


_RUNTIME_SHA256 = "a" * 64
_SNAPSHOT = "three-law-snapshot-2026-07-10"
_REQUEST_ID = "run-1-retrieve-0"


def _trace_record() -> dict[str, Any]:
    return {
        "schema_version": "three-law-evidence-v3-scenario-eval-trace/v2",
        "request_id": _REQUEST_ID,
        "http_status_code": 200,
        "evidence_profile": "preserved-3law-v3-scenario-eval",
        "evidence_scope": "ACTUAL_3_LAW_RAG_SCENARIO_EVAL_3_OF_58",
        "evidence_data_source": "ACTUAL_POSTGRESQL_QDRANT_NEO4J_READ_ONLY",
        "evidence_policy_mode": "SEALED_TEST_ONLY_ACTUAL_CCCT_HUMAN_ONLY",
        "knowledge_snapshot_id": _SNAPSHOT,
        "provider_identity": {
            "provider_release": "evidence-api-v1.2.0",
            "provider_revision": "1" * 40,
            "provider_build_sha256": "2" * 64,
            "knowledge_generation_id": "3" * 64,
            "knowledge_generation_attestation_sha256": "4" * 64,
            "knowledge_source_manifest_sha256": "5" * 64,
            "supported_review_as_of": "2026-07-10",
            "runtime_release_sha256": "6" * 64,
            "runtime_release_v3_sha256": _RUNTIME_SHA256,
            "requirement_policy_release": ("test-only-three-law-scenario-eval-human-only.v1"),
            "requirement_policy_manifest_sha256": "7" * 64,
            "requirement_policy_approval_sha256": "8" * 64,
            "reviewer_trust_store_sha256": "9" * 64,
        },
        "retrieval": {
            "structured": {
                "attempted": True,
                "succeeded": True,
                "attempt_count": 1,
                "result_count": 1,
                "top_k_result_count": 0,
                "duration_ms": 7,
            },
            "dense": {
                "attempted": True,
                "succeeded": True,
                "attempt_count": 1,
                "result_count": 20,
                "top_k_result_count": 2,
                "duration_ms": 11,
            },
            "bm25": {
                "attempted": True,
                "succeeded": True,
                "attempt_count": 1,
                "result_count": 20,
                "top_k_result_count": 1,
                "duration_ms": 13,
            },
            "rerank": {
                "attempted": True,
                "succeeded": True,
                "attempt_count": 1,
                "result_count": 20,
                "top_k_result_count": 2,
                "duration_ms": 17,
            },
            "graph": {
                "attempted": True,
                "succeeded": True,
                "attempt_count": 1,
                "result_count": 0,
                "top_k_result_count": 0,
                "duration_ms": 5,
            },
        },
    }


def _write_trace(root: Path, records: list[dict[str, Any]]) -> Path:
    path = (
        root
        / "_local"
        / "state"
        / "local-user-uat"
        / "three-law-scenario-eval-trace.jsonl"
    )
    path.parent.mkdir(parents=True)
    path.write_text(
        "".join(
            json.dumps(record, ensure_ascii=False, separators=(",", ":")) + "\n"
            for record in records
        ),
        encoding="utf-8",
    )
    return path


@pytest.fixture
def isolated_trace_content_guard(monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep JSONL contract tests independent from the deployment filesystem layout."""

    def assert_regular_test_file(path: str | Path) -> Path:
        resolved = Path(path).resolve(strict=True)
        if not resolved.is_file():
            raise UatEvidenceTraceError("local UAT Evidence trace path is invalid")
        return resolved

    monkeypatch.setattr(trace_module, "_assert_external_trace_path", assert_regular_test_file)


def test_trace_reader_returns_only_the_run_bound_safe_stage_projection(
    tmp_path: Path,
    isolated_trace_content_guard: None,
) -> None:
    path = _write_trace(tmp_path, [_trace_record()])
    reader = UatEvidenceTraceReader(
        path,
        expected_runtime_v3_sha256=_RUNTIME_SHA256,
    )

    projection = reader.projection(
        request_id=_REQUEST_ID,
        knowledge_snapshot_id=_SNAPSHOT,
    )
    payload = projection.model_dump(mode="json")

    assert payload == {
        "schema_version": "customs-backend-uat-evidence-trace/v1",
        "request_id": _REQUEST_ID,
        "knowledge_snapshot_id": _SNAPSHOT,
        "retrieval": {
            "structured": {
                "attempted": True,
                "succeeded": True,
                "result_count": 1,
                "duration_ms": 7,
                "top_k_count": 0,
            },
            "dense": {
                "attempted": True,
                "succeeded": True,
                "result_count": 20,
                "duration_ms": 11,
                "top_k_count": 2,
            },
            "bm25": {
                "attempted": True,
                "succeeded": True,
                "result_count": 20,
                "duration_ms": 13,
                "top_k_count": 1,
            },
            "graph": {
                "attempted": True,
                "succeeded": True,
                "result_count": 0,
                "duration_ms": 5,
                "top_k_count": 0,
            },
        },
    }
    serialized = json.dumps(payload)
    for forbidden in (
        "provider_identity",
        "provider_release",
        _RUNTIME_SHA256,
        "attempt_count",
        "top_k_result_count",
        str(path),
    ):
        assert forbidden not in serialized


def test_trace_reader_rejects_duplicate_or_failed_request_records(
    tmp_path: Path,
    isolated_trace_content_guard: None,
) -> None:
    duplicate_path = _write_trace(tmp_path / "duplicate", [_trace_record(), _trace_record()])
    duplicate_reader = UatEvidenceTraceReader(
        duplicate_path,
        expected_runtime_v3_sha256=_RUNTIME_SHA256,
    )
    with pytest.raises(UatEvidenceTraceError, match="duplicated"):
        duplicate_reader.projection(
            request_id=_REQUEST_ID,
            knowledge_snapshot_id=_SNAPSHOT,
        )

    failed = _trace_record()
    failed["retrieval"]["graph"]["succeeded"] = False
    failed_path = _write_trace(tmp_path / "failed", [failed])
    with pytest.raises(UatEvidenceTraceError, match="record is invalid"):
        UatEvidenceTraceReader(
            failed_path,
            expected_runtime_v3_sha256=_RUNTIME_SHA256,
        )


def test_trace_reader_rejects_identity_drift_and_extra_fields(
    tmp_path: Path,
    isolated_trace_content_guard: None,
) -> None:
    path = _write_trace(tmp_path / "drift", [_trace_record()])
    reader = UatEvidenceTraceReader(
        path,
        expected_runtime_v3_sha256="b" * 64,
    )
    with pytest.raises(UatEvidenceTraceError, match="runtime identity differs"):
        reader.projection(
            request_id=_REQUEST_ID,
            knowledge_snapshot_id=_SNAPSHOT,
        )

    extra = _trace_record()
    extra["raw_query"] = "MUST-NOT-BE-ACCEPTED"
    extra_path = _write_trace(tmp_path / "extra", [extra])
    with pytest.raises(UatEvidenceTraceError, match="record is invalid"):
        UatEvidenceTraceReader(
            extra_path,
            expected_runtime_v3_sha256=_RUNTIME_SHA256,
        )

    extra_provider_identity = _trace_record()
    extra_provider_identity["provider_identity"]["private_provider_marker"] = "MUST-NOT-BE-ACCEPTED"
    extra_provider_path = _write_trace(
        tmp_path / "extra-provider",
        [extra_provider_identity],
    )
    with pytest.raises(UatEvidenceTraceError, match="record is invalid"):
        UatEvidenceTraceReader(
            extra_provider_path,
            expected_runtime_v3_sha256=_RUNTIME_SHA256,
        )


def test_trace_reader_rejects_partial_jsonl_records(
    tmp_path: Path,
    isolated_trace_content_guard: None,
) -> None:
    path = _write_trace(tmp_path, [_trace_record()])
    path.write_bytes(path.read_bytes().removesuffix(b"\n"))
    with pytest.raises(UatEvidenceTraceError, match="incomplete record"):
        UatEvidenceTraceReader(
            path,
            expected_runtime_v3_sha256=_RUNTIME_SHA256,
        )


def test_trace_reader_fails_closed_for_excessive_json_nesting(
    tmp_path: Path,
    isolated_trace_content_guard: None,
) -> None:
    path = _write_trace(tmp_path, [])
    path.write_text("[" * 1_100 + "0" + "]" * 1_100 + "\n", encoding="utf-8")

    with pytest.raises(UatEvidenceTraceError, match="record is invalid"):
        UatEvidenceTraceReader(
            path,
            expected_runtime_v3_sha256=_RUNTIME_SHA256,
        )


def test_trace_reader_rejects_path_outside_runtime_allowlist(tmp_path: Path) -> None:
    outside = tmp_path / "trace.jsonl"
    outside.write_text("{}\n", encoding="utf-8")

    with pytest.raises(UatEvidenceTraceError, match="path is invalid"):
        UatEvidenceTraceReader(
            outside,
            expected_runtime_v3_sha256=_RUNTIME_SHA256,
        )


def test_trace_path_allowlist_matches_harness_windows_and_container_runtime() -> None:
    windows_parts = PureWindowsPath(
        "C:/workspace/_local/state/local-user-uat/three-law-scenario-eval-trace.jsonl"
    ).parts
    container_parts = PurePosixPath("/run/local-user-uat/three-law-scenario-eval-trace.jsonl").parts

    assert _is_allowed_trace_path_parts(windows_parts) is True
    assert _is_allowed_trace_path_parts(container_parts) is True

    uppercase_windows_parts = tuple(
        PureWindowsPath(
            "C:/WORKSPACE/_LOCAL/STATE/LOCAL-USER-UAT/THREE-LAW-SCENARIO-EVAL-TRACE.JSONL"
        ).parts
    )
    assert _is_allowed_trace_path_parts(uppercase_windows_parts) is True


@pytest.mark.parametrize(
    "path",
    [
        "C:/workspace/_artifacts/runtime/local-user-uat/three-law-scenario-eval-trace.jsonl",
        "C:/workspace/_artifacts/uat/runtime/three-law-scenario-eval-trace.jsonl",
        "C:/workspace/_local/runtime/local-user-uat/three-law-scenario-eval-trace.jsonl",
        "C:/workspace/_local/state/runtime/local-user-uat/three-law-scenario-eval-trace.jsonl",
        "C:/workspace/_local/state/local-user-uat-decoy/three-law-scenario-eval-trace.jsonl",
        "C:/workspace/_local/state/local-user-uat/nested/three-law-scenario-eval-trace.jsonl",
        "C:/workspace/_local/state/local-user-uat/other.jsonl",
    ],
)
def test_trace_path_allowlist_rejects_obsolete_and_decoy_windows_paths(path: str) -> None:
    parts = PureWindowsPath(path).parts

    assert _is_allowed_trace_path_parts(parts) is False


@pytest.mark.parametrize(
    "path",
    [
        "/run/local-user-uat-decoy/three-law-scenario-eval-trace.jsonl",
        "/run/local-user-uat/nested/three-law-scenario-eval-trace.jsonl",
        "/run/local-user-uat/other.jsonl",
        "/tmp/run/local-user-uat/three-law-scenario-eval-trace.jsonl",
        "/tmp/_local/state/runtime/local-user-uat/three-law-scenario-eval-trace.jsonl",
        "/RUN/LOCAL-USER-UAT/THREE-LAW-SCENARIO-EVAL-TRACE.JSONL",
    ],
)
def test_trace_path_allowlist_rejects_decoy_container_paths(path: str) -> None:
    parts = PurePosixPath(path).parts

    assert _is_allowed_trace_path_parts(parts) is False
