"""Private, local-only detailed records for explicitly supported test runs.

The normal Backend never records document bodies.  ``dev-fixture`` enables a
recorder only when the caller explicitly provides the local root; the
``functional-uat`` profile constructs the same recorder automatically.  Both
paths reject anything except the workspace-local ``_local/state/scenario-runs``
directory so a detailed record cannot enter a product checkout or a shared
artifact path accidentally.
"""

from __future__ import annotations

import json
import os
import re
import threading
import uuid
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


_SAFE_RUN_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
_SENSITIVE_FIELD = re.compile(
    r"(?:authorization|cookie|token|api[_-]?key|secret|password|credential|dsn)", re.IGNORECASE
)
_TEXT_REDACTIONS = (
    (re.compile(r"(?i)\b(sk-ant-[A-Za-z0-9_-]{12,})\b"), "[REDACTED]"),
    (re.compile(r"(?i)\b(gh[pousr]_[A-Za-z0-9]{12,})\b"), "[REDACTED]"),
    (
        re.compile(r"(?i)(\bbearer\s+)[A-Za-z0-9._~+/-]{16,}"),
        r"\1[REDACTED]",
    ),
    (
        re.compile(
            r"(?i)(\b(?:anthropic_api_key|law_api_oc|api[_-]?key|token|secret|password)\s*[:=]\s*)[^\s,;]+"
        ),
        r"\1[REDACTED]",
    ),
)
_BACKEND_STREAMS = frozenset({"public-api", "backend-agent"})
_REQUIRED_STREAMS = (
    "scenario.json",
    "public-api.jsonl",
    "backend-agent.jsonl",
    "agent-data.jsonl",
    "claude.jsonl",
    "summary.json",
)
_WRITE_LOCK = threading.Lock()


class ScenarioTranscriptError(RuntimeError):
    """A detailed scenario record cannot be written or verified safely."""


def workspace_transcript_root() -> Path:
    """Return the one permitted root for this sibling checkout layout."""

    return Path(__file__).resolve().parents[3] / "_local" / "state" / "scenario-runs"


def _normalise_secret_values(values: Iterable[str]) -> tuple[str, ...]:
    # Longer values first avoids leaving a suffix visible when values overlap.
    return tuple(sorted({value for value in values if len(value) >= 6}, key=len, reverse=True))


@dataclass(frozen=True, slots=True)
class TranscriptRedactor:
    """Redact known secret values as well as conventional secret shapes."""

    secret_values: tuple[str, ...] = ()

    @classmethod
    def from_values(cls, values: Iterable[str] = ()) -> "TranscriptRedactor":
        return cls(secret_values=_normalise_secret_values(values))

    def text(self, value: str) -> str:
        for secret in self.secret_values:
            value = value.replace(secret, "[REDACTED]")
        for pattern, replacement in _TEXT_REDACTIONS:
            value = pattern.sub(replacement, value)
        return value

    def value(self, value: Any) -> Any:
        if isinstance(value, Mapping):
            result: dict[str, Any] = {}
            for key, nested in value.items():
                text_key = str(key)
                result[text_key] = (
                    "[REDACTED]" if _SENSITIVE_FIELD.search(text_key) else self.value(nested)
                )
            return result
        if isinstance(value, list | tuple):
            return [self.value(item) for item in value]
        if isinstance(value, str):
            return self.text(value)
        return value


def redact_for_transcript(value: Any, *, secret_values: Iterable[str] = ()) -> Any:
    """Public helper retained for tests and the manual dev-fixture runner."""

    return TranscriptRedactor.from_values(secret_values).value(value)


def _has_unredacted_secret(value: Any) -> bool:
    """Conservative offline check used by the local verifier.

    It deliberately inspects only field names and well-known credential forms;
    configured literal values are redacted at write time and never persisted
    for the verifier to rediscover.
    """

    if isinstance(value, Mapping):
        for key, nested in value.items():
            if _SENSITIVE_FIELD.search(str(key)) and nested != "[REDACTED]":
                return True
            if _has_unredacted_secret(nested):
                return True
        return False
    if isinstance(value, list | tuple):
        return any(_has_unredacted_secret(item) for item in value)
    if not isinstance(value, str):
        return False
    # The key/value-shaped expression intentionally also matches a redacted
    # value (for example ``ANTHROPIC_API_KEY=[REDACTED]``).  That is a safe
    # retained field name, not an exposed credential.
    return any(
        "[REDACTED]" not in match.group(0)
        for pattern, _replacement in _TEXT_REDACTIONS
        for match in [pattern.search(value)]
        if match is not None
    )


def _validate_root(path: Path) -> Path:
    if not path.is_absolute():
        raise ScenarioTranscriptError("scenario_transcript_root_not_absolute")
    candidate = path.resolve(strict=False)
    if (
        candidate.name != "scenario-runs"
        or candidate.parent.name != "state"
        or candidate.parent.parent.name != "_local"
    ):
        raise ScenarioTranscriptError("scenario_transcript_root_invalid")
    try:
        candidate.mkdir(parents=True, exist_ok=True)
        resolved = candidate.resolve(strict=True)
    except OSError as exc:
        raise ScenarioTranscriptError("scenario_transcript_root_unavailable") from exc
    if (
        resolved.name != "scenario-runs"
        or resolved.parent.name != "state"
        or resolved.parent.parent.name != "_local"
    ):
        raise ScenarioTranscriptError("scenario_transcript_root_invalid")
    return resolved


@dataclass(frozen=True, slots=True)
class ScenarioTranscriptRun:
    directory: Path
    redactor: TranscriptRedactor

    def append(self, stream: str, event: str, payload: Mapping[str, Any]) -> None:
        if stream not in _BACKEND_STREAMS or not event or len(event) > 128:
            raise ScenarioTranscriptError("scenario_transcript_stream_invalid")
        record = {
            "schema_version": "scenario-transcript/v1",
            "recorded_at": datetime.now(timezone.utc).isoformat(),
            "event": event,
            "payload": self.redactor.value(dict(payload)),
        }
        self._write_jsonl(self.directory / f"{stream}.jsonl", record)

    def write_json(self, filename: str, payload: Mapping[str, Any]) -> None:
        if filename not in {"scenario.json", "summary.json"}:
            raise ScenarioTranscriptError("scenario_transcript_filename_invalid")
        encoded = json.dumps(
            self.redactor.value(dict(payload)),
            ensure_ascii=False,
            allow_nan=False,
            indent=2,
            sort_keys=True,
            default=str,
        )
        try:
            with (self.directory / filename).open("x", encoding="utf-8", newline="\n") as handle:
                handle.write(encoded + "\n")
                handle.flush()
                os.fsync(handle.fileno())
        except FileExistsError as exc:
            raise ScenarioTranscriptError("scenario_transcript_file_exists") from exc
        except OSError as exc:
            raise ScenarioTranscriptError("scenario_transcript_write_failed") from exc

    def replace_summary(self, payload: Mapping[str, Any]) -> None:
        """Atomically refresh a terminal summary without freezing its run.

        A functional-UAT run can continue with grounded conversations,
        correction decisions, and child revisions after the first detailed
        result is read.  The summary is therefore a safe checkpoint, not a
        write barrier for the surrounding transcript streams.
        """

        encoded = json.dumps(
            self.redactor.value(dict(payload)),
            ensure_ascii=False,
            allow_nan=False,
            indent=2,
            sort_keys=True,
            default=str,
        )
        target = self.directory / "summary.json"
        temporary = self.directory / f".summary.{uuid.uuid4().hex}.tmp"
        try:
            with _WRITE_LOCK:
                with temporary.open("x", encoding="utf-8", newline="\n") as handle:
                    handle.write(encoded + "\n")
                    handle.flush()
                    os.fsync(handle.fileno())
                os.replace(temporary, target)
        except OSError as exc:
            raise ScenarioTranscriptError("scenario_transcript_write_failed") from exc
        finally:
            try:
                temporary.unlink(missing_ok=True)
            except OSError:
                pass

    def _write_jsonl(self, path: Path, record: Mapping[str, Any]) -> None:
        encoded = json.dumps(
            record,
            ensure_ascii=False,
            allow_nan=False,
            separators=(",", ":"),
            sort_keys=True,
            default=str,
        )
        try:
            with _WRITE_LOCK:
                with path.open("a", encoding="utf-8", newline="\n") as handle:
                    handle.write(encoded + "\n")
                    handle.flush()
                    os.fsync(handle.fileno())
        except OSError as exc:
            raise ScenarioTranscriptError("scenario_transcript_write_failed") from exc


@dataclass(frozen=True, slots=True)
class ScenarioTranscriptRoot:
    directory: Path
    redactor: TranscriptRedactor = field(default_factory=TranscriptRedactor)

    @classmethod
    def create(cls, root: Path, *, secret_values: Iterable[str] = ()) -> "ScenarioTranscriptRoot":
        return cls(
            directory=_validate_root(root), redactor=TranscriptRedactor.from_values(secret_values)
        )

    def open_or_create_run(
        self, review_run_id: str, *, allow_terminal_summary: bool = False
    ) -> ScenarioTranscriptRun:
        if _SAFE_RUN_ID.fullmatch(review_run_id) is None:
            raise ScenarioTranscriptError("scenario_transcript_run_id_invalid")
        candidate = self.directory / review_run_id
        try:
            candidate.mkdir(exist_ok=True)
            resolved = candidate.resolve(strict=True)
        except OSError as exc:
            raise ScenarioTranscriptError("scenario_transcript_run_unavailable") from exc
        if resolved.parent != self.directory:
            raise ScenarioTranscriptError("scenario_transcript_run_invalid")
        if (resolved / "summary.json").exists() and not allow_terminal_summary:
            raise ScenarioTranscriptError("scenario_transcript_run_finalized")
        return ScenarioTranscriptRun(directory=resolved, redactor=self.redactor)

    def is_finalized(self, review_run_id: str) -> bool:
        if _SAFE_RUN_ID.fullmatch(review_run_id) is None:
            raise ScenarioTranscriptError("scenario_transcript_run_id_invalid")
        return (self.directory / review_run_id / "summary.json").is_file()


@dataclass(slots=True)
class FunctionalTranscriptController:
    """Backend-owned streams and public-workflow buffering for functional UAT."""

    root: ScenarioTranscriptRoot
    profile: str = "functional-uat"
    _pending_public: dict[str, list[dict[str, Any]]] = field(default_factory=dict)
    _latest_run_by_case: dict[str, str] = field(default_factory=dict)
    _state_lock: threading.RLock = field(default_factory=threading.RLock, repr=False)

    def _append(
        self, review_run_id: str, stream: str, event: str, payload: Mapping[str, Any]
    ) -> None:
        with self._state_lock:
            run = self.root.open_or_create_run(review_run_id, allow_terminal_summary=True)
            run.append(stream, event, payload)
            self._refresh_summary_checkpoint(review_run_id)

    def _refresh_summary_checkpoint(self, review_run_id: str) -> None:
        """Keep an existing terminal checkpoint honest after later activity."""

        directory = self.root.directory / review_run_id
        summary_path = directory / "summary.json"
        if not summary_path.is_file():
            return
        try:
            existing = json.loads(summary_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise ScenarioTranscriptError("scenario_transcript_json_invalid") from exc
        if not isinstance(existing, Mapping):
            raise ScenarioTranscriptError("scenario_transcript_json_invalid")
        self.root.open_or_create_run(review_run_id, allow_terminal_summary=True).replace_summary(
            {
                **dict(existing),
                "last_recorded_at": datetime.now(timezone.utc).isoformat(),
            }
        )

    def record_backend_agent(
        self, review_run_id: str, event: str, payload: Mapping[str, Any]
    ) -> None:
        self._append(review_run_id, "backend-agent", event, payload)

    def record_public(
        self,
        *,
        case_id: str,
        method: str,
        path: str,
        request_body: Mapping[str, Any] | None,
        response_status: int,
        response_body: Mapping[str, Any] | None,
        review_run_id: str | None = None,
    ) -> None:
        event = {
            "method": method,
            "path": path,
            "request_body": dict(request_body) if request_body is not None else None,
            "response_status": response_status,
            "response_body": dict(response_body) if response_body is not None else None,
        }
        with self._state_lock:
            if review_run_id is None:
                review_run_id = self._latest_run_by_case.get(case_id)
            if review_run_id is None:
                self._pending_public.setdefault(case_id, []).append(event)
                return
            self._append(review_run_id, "public-api", "http_exchange", event)

    def bind_review(
        self,
        *,
        case_id: str,
        review_run_id: str,
        job_id: str,
        review_request: Mapping[str, Any],
        response_status: int,
        response_body: Mapping[str, Any],
        job_status: str,
        public_path: str | None = None,
    ) -> None:
        with self._state_lock:
            run = self.root.open_or_create_run(review_run_id)
            self._latest_run_by_case[case_id] = review_run_id
            run.write_json(
                "scenario.json",
                {
                    "schema_version": "scenario-transcript/v1",
                    "profile": self.profile,
                    "case_id": case_id,
                    "review_run_id": review_run_id,
                    "job_id": job_id,
                    "started_at": datetime.now(timezone.utc).isoformat(),
                    "review_request": dict(review_request),
                },
            )
            for event in self._pending_public.pop(case_id, []):
                run.append("public-api", "http_exchange", event)
            run.append(
                "public-api",
                "http_exchange",
                {
                    "method": "POST",
                    "path": public_path or f"/api/v1/cases/{case_id}/reviews",
                    "request_body": dict(review_request),
                    "response_status": response_status,
                    "response_body": dict(response_body),
                },
            )
            if job_status == "failed":
                self.finalize(
                    review_run_id,
                    status="failed",
                    fields={"job_id": job_id, "job_status": job_status},
                )

    def finalize(self, review_run_id: str, *, status: str, fields: Mapping[str, Any]) -> None:
        with self._state_lock:
            run = self.root.open_or_create_run(review_run_id, allow_terminal_summary=True)
            payload = {
                "schema_version": "scenario-transcript/v1",
                "profile": self.profile,
                "review_run_id": review_run_id,
                "status": status,
                "completed_at": datetime.now(timezone.utc).isoformat(),
                "required_streams": list(_REQUIRED_STREAMS),
                **dict(fields),
            }
            if (run.directory / "summary.json").is_file():
                run.replace_summary(payload)
            else:
                run.write_json("summary.json", payload)


def verify_run(root: Path, review_run_id: str) -> dict[str, Any]:
    """Validate a local record without echoing any recorded body."""

    directory = _validate_root(root) / review_run_id
    if _SAFE_RUN_ID.fullmatch(review_run_id) is None or not directory.is_dir():
        raise ScenarioTranscriptError("scenario_transcript_run_missing")
    missing = [name for name in _REQUIRED_STREAMS if not (directory / name).is_file()]
    if missing:
        raise ScenarioTranscriptError(
            "scenario_transcript_required_stream_missing:" + ",".join(missing)
        )
    try:
        scenario = json.loads((directory / "scenario.json").read_text(encoding="utf-8"))
        summary = json.loads((directory / "summary.json").read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ScenarioTranscriptError("scenario_transcript_json_invalid") from exc
    if not isinstance(scenario, Mapping) or not isinstance(summary, Mapping):
        raise ScenarioTranscriptError("scenario_transcript_json_invalid")
    if (
        scenario.get("schema_version") != "scenario-transcript/v1"
        or summary.get("schema_version") != "scenario-transcript/v1"
    ):
        raise ScenarioTranscriptError("scenario_transcript_schema_invalid")
    if (
        scenario.get("review_run_id") != review_run_id
        or summary.get("review_run_id") != review_run_id
    ):
        raise ScenarioTranscriptError("scenario_transcript_run_identity_invalid")
    profile = scenario.get("profile")
    if profile not in {"functional-uat", "dev-fixture"} or summary.get("profile") != profile:
        raise ScenarioTranscriptError("scenario_transcript_profile_invalid")
    if summary.get("status") not in {"completed", "failed", "passed"}:
        raise ScenarioTranscriptError("scenario_transcript_not_terminal")
    if _has_unredacted_secret(scenario) or _has_unredacted_secret(summary):
        raise ScenarioTranscriptError("scenario_transcript_redaction_failed")
    counts: dict[str, int] = {}
    for name in _REQUIRED_STREAMS[1:-1]:
        try:
            records = [
                json.loads(line)
                for line in (directory / name).read_text(encoding="utf-8").splitlines()
                if line
            ]
        except (OSError, json.JSONDecodeError) as exc:
            raise ScenarioTranscriptError("scenario_transcript_stream_invalid:" + name) from exc
        if not records or not all(isinstance(record, Mapping) for record in records):
            raise ScenarioTranscriptError("scenario_transcript_stream_invalid:" + name)
        if not all(
            record.get("schema_version") == "scenario-transcript/v1"
            and isinstance(record.get("event"), str)
            and isinstance(record.get("payload"), Mapping)
            and not _has_unredacted_secret(record)
            for record in records
        ):
            raise ScenarioTranscriptError("scenario_transcript_stream_invalid:" + name)
        counts[name] = len(records)
    return {
        "review_run_id": review_run_id,
        "profile": scenario.get("profile"),
        "status": summary.get("status"),
        "streams": counts,
    }


__all__ = [
    "FunctionalTranscriptController",
    "ScenarioTranscriptError",
    "ScenarioTranscriptRoot",
    "ScenarioTranscriptRun",
    "TranscriptRedactor",
    "redact_for_transcript",
    "verify_run",
    "workspace_transcript_root",
]
