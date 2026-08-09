"""Run the Frontend-owned Functional-UAT Browser journey without exposing secrets.

This local-only wrapper deliberately owns only the boundary around the
Frontend Playwright command.  The Frontend owns browser interaction, while
the Backend owns the local Functional-UAT credentials and validates the safe
receipt plus the service-owned scenario transcripts afterwards.

It is not a release gate.  It neither starts nor stops services, resets data,
downloads a browser, nor publishes anything.  Run it only after the documented
Functional-UAT services are ready on the literal loopback endpoints.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import stat
import subprocess
import uuid
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import httpx

from customs_backend.scenario_transcript import (
    ScenarioTranscriptError,
    verify_run,
    workspace_transcript_root,
)


@dataclass(frozen=True, slots=True)
class BrowserFailureLocation:
    """The only failure location that may cross the Browser subprocess boundary."""

    file: str
    line: int
    column: int


class BrowserE2eError(RuntimeError):
    """A local Browser Functional-UAT precondition or receipt was unsafe."""

    def __init__(self, reason: str, *, first_failure: BrowserFailureLocation | None = None) -> None:
        super().__init__(reason)
        self.first_failure = first_failure


_PROFILE_REQUIRED = frozenset(
    {
        "CUSTOMS_FUNCTIONAL_REVIEWER_USERNAME",
        "CUSTOMS_FUNCTIONAL_REVIEWER_PASSWORD",
        "CUSTOMS_FUNCTIONAL_APPROVER_USERNAME",
        "CUSTOMS_FUNCTIONAL_APPROVER_PASSWORD",
    }
)
_REPOSITORIES = (
    "customs-frontend",
    "customs-backend",
    "customs-ai-agent",
    "customs-data-platform",
)
_RECEIPT_SCHEMA = "functional-uat-browser-e2e/v1"
_RECEIPT_KEYS = frozenset(
    {
        "schema_version",
        "status",
        "profile",
        "base_url",
        "checks",
        "review_run_ids",
        "browser_api_origins",
    }
)
_FAILURE_RECEIPT_SCHEMA = "functional-uat-browser-failure/v1"
_FAILURE_RECEIPT_KEYS = frozenset(
    {
        "schema_version",
        "status",
        "failure_count",
        "first_failure",
    }
)
_FAILURE_LOCATION_KEYS = frozenset({"file", "line", "column"})
_FAILURE_FILE_NAME = "failure.json"
_FAILURE_SPEC_FILE = "functional-uat-browser.spec.ts"
_RECEIPT_ROOT_PARTS = ("_local", "state", "e2e-runs")
_REQUIRED_CHECKS = frozenset(
    {
        "reviewer_login",
        "case_created",
        "document_registered",
        "review_completed",
        "analysis_displayed",
        "korean_user_prose",
        "raw_filename_hidden",
        "evidence_displayed",
        "regulatory_requirement_deduplicated",
        "data_retrieval_semantics",
        "citation_navigation",
        "assistant_conversation",
        "assistant_mixed_sections",
        "assistant_case_application",
        "case_document_paste_notice",
        "knowledge_conversation",
        "knowledge_recent_history",
        "knowledge_official_provenance",
        "knowledge_default_boundary",
        "knowledge_v2_scope_coverage",
        "knowledge_multiscope_clarification",
        "finding_decision",
        "correction_proposed",
        "approver_login",
        "correction_approved",
        "child_review_completed",
        "document_replaced",
        "replacement_review_completed",
        "export_document_registered",
        "export_partial_coverage",
        "export_human_review_required",
        "backend_only_network",
    }
)
_SAFE_RUN_ID = re.compile(r"^run_[0-9a-f]{32}$")
_SAFE_E2E_RUN_DIRECTORY = re.compile(r"^[0-9a-f]{32}$")
_MAX_RECEIPT_BYTES = 16 * 1024
_MAX_FAILURE_RECEIPT_BYTES = 1024
_MAX_RUN_DIRECTORY_CREATE_ATTEMPTS = 3
_BACKEND_API_URL = "http://127.0.0.1:18081/api/v1"
_FRONTEND_URL = "http://127.0.0.1:5173"
_CHILD_ENVIRONMENT_KEYS = (
    "ALLUSERSPROFILE",
    "APPDATA",
    "COMSPEC",
    "HOMEDRIVE",
    "HOMEPATH",
    "LOCALAPPDATA",
    "NUMBER_OF_PROCESSORS",
    "OS",
    "PATH",
    "PATHEXT",
    "PROCESSOR_ARCHITECTURE",
    "PROCESSOR_IDENTIFIER",
    "PROCESSOR_LEVEL",
    "PROCESSOR_REVISION",
    "ProgramData",
    "ProgramFiles",
    "ProgramFiles(x86)",
    "PUBLIC",
    "SystemDrive",
    "SystemRoot",
    "TEMP",
    "TMP",
    "USERDOMAIN",
    "USERNAME",
    "USERPROFILE",
    "WINDIR",
)


@dataclass(frozen=True, slots=True)
class BrowserReceipt:
    """The only Browser result shape allowed to cross the local boundary."""

    checks: tuple[str, ...]
    review_run_ids: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class BrowserFailureReceipt:
    """Strictly bounded failure metadata emitted by the Frontend reporter."""

    failure_count: int
    first_failure: BrowserFailureLocation | None


def _workspace_root() -> Path:
    return Path(__file__).resolve().parents[2]


def _literal_loopback_url(value: str, *, expected: str) -> str:
    parsed = urlsplit(value)
    if (
        value != expected
        or parsed.scheme != "http"
        or parsed.hostname != "127.0.0.1"
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
    ):
        raise argparse.ArgumentTypeError("endpoint must remain the fixed literal loopback URL")
    return value


def _backend_api_url(value: str) -> str:
    return _literal_loopback_url(value.rstrip("/"), expected=_BACKEND_API_URL)


def _frontend_url(value: str) -> str:
    return _literal_loopback_url(value.rstrip("/"), expected=_FRONTEND_URL)


def _profile_path(value: str) -> Path:
    workspace = _workspace_root()
    expected_root = (workspace / "_local" / "profiles" / "functional-uat").resolve()
    candidate = Path(value).resolve()
    try:
        candidate.relative_to(expected_root)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("profile must remain under local functional-uat") from exc
    if not candidate.is_file() or candidate.is_symlink():
        raise argparse.ArgumentTypeError("local functional-UAT profile is unavailable")
    return candidate


def _is_link_or_reparse_point(details: os.stat_result) -> bool:
    reparse_point = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0)
    return stat.S_ISLNK(details.st_mode) or bool(
        getattr(details, "st_file_attributes", 0) & reparse_point
    )


def _ensure_safe_directory(path: Path, *, create: bool) -> bool:
    """Reject links/reparse points before and after a one-level directory creation."""

    try:
        details = path.lstat()
    except FileNotFoundError:
        if not create:
            return False
        try:
            path.mkdir()
        except FileExistsError:
            pass
        except OSError as exc:
            raise BrowserE2eError("functional_uat_browser_receipt_root_unsafe") from exc
        try:
            details = path.lstat()
        except OSError as exc:
            raise BrowserE2eError("functional_uat_browser_receipt_root_unsafe") from exc
    except OSError as exc:
        raise BrowserE2eError("functional_uat_browser_receipt_root_unsafe") from exc
    if _is_link_or_reparse_point(details):
        raise BrowserE2eError("functional_uat_browser_receipt_root_unsafe")
    try:
        if not path.is_dir():
            raise BrowserE2eError("functional_uat_browser_receipt_root_unsafe")
    except OSError as exc:
        raise BrowserE2eError("functional_uat_browser_receipt_root_unsafe") from exc
    return True


def _trusted_receipt_root(*, create: bool) -> Path:
    """Derive the only permitted root without resolving user-supplied paths."""

    workspace = _workspace_root()
    if not workspace.is_absolute() or not _ensure_safe_directory(workspace, create=False):
        raise BrowserE2eError("functional_uat_browser_receipt_root_unsafe")
    current = workspace
    for component in _RECEIPT_ROOT_PARTS:
        current = current / component
        _ensure_safe_directory(current, create=create)
    return current


def _absolute_path_without_resolving_links(value: str | Path) -> Path:
    return Path(os.path.abspath(os.fspath(value)))


def _same_lexical_path(left: Path, right: Path) -> bool:
    return os.path.normcase(os.path.normpath(os.fspath(left))) == os.path.normcase(
        os.path.normpath(os.fspath(right))
    )


def _receipt_root(value: str) -> Path:
    try:
        expected = _trusted_receipt_root(create=False)
    except BrowserE2eError as exc:
        raise argparse.ArgumentTypeError("receipt root is unsafe") from exc
    candidate = _absolute_path_without_resolving_links(value)
    if not _same_lexical_path(candidate, expected):
        raise argparse.ArgumentTypeError("receipt root must remain under local e2e-runs")
    return expected


def _load_profile(path: Path) -> dict[str, str]:
    """Read only the two local login identities and passwords without echoing them."""

    values: dict[str, str] = {}
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError as exc:
        raise BrowserE2eError("functional_uat_browser_profile_unavailable") from exc
    for line in lines:
        if "=" not in line or line.lstrip().startswith("#"):
            continue
        key, value = line.split("=", 1)
        if key in _PROFILE_REQUIRED:
            values[key] = value
    if set(values) != _PROFILE_REQUIRED or any(not value for value in values.values()):
        raise BrowserE2eError("functional_uat_browser_profile_incomplete")
    return values


def _git_output(repository: Path, *arguments: str) -> str:
    completed = subprocess.run(
        ("git", *arguments),
        cwd=repository,
        check=False,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    if completed.returncode != 0:
        raise BrowserE2eError("functional_uat_browser_git_check_failed")
    return completed.stdout.strip()


def _validate_workspace_main(workspace: Path) -> None:
    """Require all product siblings to be clean checked-out ``main`` trees."""

    for repository_name in _REPOSITORIES:
        repository = workspace / repository_name
        if not repository.is_dir() or not (repository / ".git").exists():
            raise BrowserE2eError("functional_uat_browser_workspace_invalid")
        if _git_output(repository, "branch", "--show-current") != "main":
            raise BrowserE2eError("functional_uat_browser_workspace_not_main")
        if _git_output(repository, "status", "--porcelain=v1", "--untracked-files=all"):
            raise BrowserE2eError("functional_uat_browser_workspace_dirty")
        if _git_output(repository, "rev-parse", "HEAD") != _git_output(
            repository, "rev-parse", "refs/remotes/origin/main"
        ):
            raise BrowserE2eError("functional_uat_browser_workspace_not_origin_main")


def _corepack_command(platform: str | None = None) -> str:
    """Return an executable name that subprocess can launch on the host OS."""

    return "corepack.cmd" if (platform or os.name) == "nt" else "corepack"


def _validate_frontend_command(frontend: Path) -> None:
    package_file = frontend / "package.json"
    try:
        package = json.loads(package_file.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise BrowserE2eError("functional_uat_browser_frontend_command_missing") from exc
    scripts = package.get("scripts") if isinstance(package, Mapping) else None
    command = scripts.get("functional-uat:browser") if isinstance(scripts, Mapping) else None
    if not isinstance(command, str) or not command.strip():
        raise BrowserE2eError("functional_uat_browser_frontend_command_missing")
    if shutil.which(_corepack_command()) is None:
        raise BrowserE2eError("functional_uat_browser_corepack_unavailable")


def _validate_readiness(*, backend_api_url: str, frontend_url: str) -> None:
    try:
        backend = httpx.get(
            backend_api_url.rsplit("/api/v1", 1)[0] + "/health/functional-uat-ready",
            timeout=httpx.Timeout(15.0, connect=5.0),
        )
        frontend = httpx.get(frontend_url + "/", timeout=httpx.Timeout(15.0, connect=5.0))
    except httpx.HTTPError as exc:
        raise BrowserE2eError("functional_uat_browser_service_unavailable") from exc
    if backend.status_code != 200 or frontend.status_code != 200:
        raise BrowserE2eError("functional_uat_browser_service_unavailable")
    try:
        health = backend.json()
    except json.JSONDecodeError as exc:
        raise BrowserE2eError("functional_uat_browser_health_invalid") from exc
    if not isinstance(health, Mapping) or any(
        health.get(key) != expected
        for key, expected in {
            "status": "ready",
            "mode": "functional-uat",
            "as_of": "2026-08-05",
            "human_review_only": True,
        }.items()
    ):
        raise BrowserE2eError("functional_uat_browser_health_invalid")


def _child_environment(
    *, profile: Mapping[str, str], frontend_url: str, receipt_path: Path, failure_path: Path
) -> dict[str, str]:
    """Pass only OS necessities plus the declared Browser inputs to Node."""

    allowed_names = {name.casefold() for name in _CHILD_ENVIRONMENT_KEYS}
    allowed = {key: value for key, value in os.environ.items() if key.casefold() in allowed_names}
    allowed.update(
        {
            "CUSTOMS_FUNCTIONAL_UAT_BROWSER_E2E": "1",
            "CUSTOMS_FUNCTIONAL_UAT_BROWSER_BASE_URL": frontend_url,
            "CUSTOMS_FUNCTIONAL_UAT_BROWSER_REVIEWER_USERNAME": profile[
                "CUSTOMS_FUNCTIONAL_REVIEWER_USERNAME"
            ],
            "CUSTOMS_FUNCTIONAL_UAT_BROWSER_REVIEWER_PASSWORD": profile[
                "CUSTOMS_FUNCTIONAL_REVIEWER_PASSWORD"
            ],
            "CUSTOMS_FUNCTIONAL_UAT_BROWSER_APPROVER_USERNAME": profile[
                "CUSTOMS_FUNCTIONAL_APPROVER_USERNAME"
            ],
            "CUSTOMS_FUNCTIONAL_UAT_BROWSER_APPROVER_PASSWORD": profile[
                "CUSTOMS_FUNCTIONAL_APPROVER_PASSWORD"
            ],
            "CUSTOMS_FUNCTIONAL_UAT_BROWSER_RECEIPT_PATH": str(receipt_path),
            "CUSTOMS_FUNCTIONAL_UAT_SAFE_FAILURE_FILE": str(failure_path),
        }
    )
    return allowed


def _receipt_path(receipt_root: Path) -> Path:
    trusted_root = _trusted_receipt_root(create=True)
    if not _same_lexical_path(receipt_root, trusted_root):
        raise BrowserE2eError("functional_uat_browser_receipt_root_unsafe")
    for _ in range(_MAX_RUN_DIRECTORY_CREATE_ATTEMPTS):
        run_directory = trusted_root / uuid.uuid4().hex
        try:
            run_directory.mkdir()
        except FileExistsError:
            continue
        except OSError as exc:
            raise BrowserE2eError("functional_uat_browser_receipt_root_unsafe") from exc
        if (
            run_directory.parent != trusted_root
            or not _SAFE_E2E_RUN_DIRECTORY.fullmatch(run_directory.name)
            or not _ensure_safe_directory(trusted_root, create=False)
            or not _ensure_safe_directory(run_directory, create=False)
        ):
            raise BrowserE2eError("functional_uat_browser_receipt_root_unsafe")
        return run_directory / "receipt.json"
    raise BrowserE2eError("functional_uat_browser_receipt_root_unsafe")


def _validated_run_directory(*, receipt_path: Path, receipt_root: Path) -> Path:
    trusted_root = _trusted_receipt_root(create=False)
    run_directory = receipt_path.parent
    if (
        not receipt_path.is_absolute()
        or receipt_path.name != "receipt.json"
        or not receipt_root.is_absolute()
        or not _same_lexical_path(receipt_root, trusted_root)
        or run_directory.parent != trusted_root
        or not _SAFE_E2E_RUN_DIRECTORY.fullmatch(run_directory.name)
        or not _ensure_safe_directory(trusted_root, create=False)
        or not _ensure_safe_directory(run_directory, create=False)
    ):
        raise BrowserE2eError("functional_uat_browser_receipt_root_unsafe")
    return run_directory


def _entry_details_or_none(path: Path, *, error_code: str) -> os.stat_result | None:
    try:
        return path.lstat()
    except FileNotFoundError:
        return None
    except OSError as exc:
        raise BrowserE2eError(error_code) from exc


def _failure_receipt_path(*, receipt_path: Path, receipt_root: Path, require_absent: bool) -> Path:
    """Reserve an absent sidecar path in the same task-local Browser run directory."""

    try:
        run_directory = _validated_run_directory(
            receipt_path=receipt_path, receipt_root=receipt_root
        )
    except BrowserE2eError:
        raise BrowserE2eError("functional_uat_browser_failure_receipt_path_unsafe") from None
    failure_path = run_directory / _FAILURE_FILE_NAME
    receipt_details = _entry_details_or_none(
        receipt_path, error_code="functional_uat_browser_failure_receipt_path_unsafe"
    )
    failure_details = _entry_details_or_none(
        failure_path, error_code="functional_uat_browser_failure_receipt_path_unsafe"
    )
    if (
        (receipt_details is not None and _is_link_or_reparse_point(receipt_details))
        or (failure_details is not None and _is_link_or_reparse_point(failure_details))
        or (require_absent and (receipt_details is not None or failure_details is not None))
    ):
        raise BrowserE2eError("functional_uat_browser_failure_receipt_path_unsafe")
    return failure_path


def _load_receipt(path: Path) -> BrowserReceipt:
    try:
        if (
            _is_link_or_reparse_point(path.lstat())
            or not path.is_file()
            or path.stat().st_size > _MAX_RECEIPT_BYTES
        ):
            raise BrowserE2eError("functional_uat_browser_receipt_invalid")
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise BrowserE2eError("functional_uat_browser_receipt_invalid") from exc
    if not isinstance(payload, Mapping) or set(payload) != _RECEIPT_KEYS:
        raise BrowserE2eError("functional_uat_browser_receipt_invalid")
    if (
        payload.get("schema_version") != _RECEIPT_SCHEMA
        or payload.get("status") != "passed"
        or payload.get("profile") != "functional-uat"
        or payload.get("base_url") != _FRONTEND_URL
        or payload.get("browser_api_origins") != [_FRONTEND_URL]
    ):
        raise BrowserE2eError("functional_uat_browser_receipt_invalid")
    checks = payload.get("checks")
    review_run_ids = payload.get("review_run_ids")
    if (
        not isinstance(checks, list)
        or not all(isinstance(value, str) for value in checks)
        or len(checks) != len(set(checks))
        or set(checks) != _REQUIRED_CHECKS
        or not isinstance(review_run_ids, list)
        or len(review_run_ids) < 3
        or not all(
            isinstance(value, str) and _SAFE_RUN_ID.fullmatch(value) for value in review_run_ids
        )
        or len(review_run_ids) != len(set(review_run_ids))
    ):
        raise BrowserE2eError("functional_uat_browser_receipt_invalid")
    return BrowserReceipt(checks=tuple(sorted(checks)), review_run_ids=tuple(review_run_ids))


def _positive_int(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value > 0


def _load_failure_receipt(path: Path) -> BrowserFailureReceipt:
    """Load a Frontend failure receipt without reading or surfacing process output."""

    try:
        if _is_link_or_reparse_point(path.lstat()):
            raise BrowserE2eError("functional_uat_browser_failure_receipt_invalid")
        if not path.is_file() or path.stat().st_size > _MAX_FAILURE_RECEIPT_BYTES:
            raise BrowserE2eError("functional_uat_browser_failure_receipt_invalid")
        payload = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise BrowserE2eError("functional_uat_browser_failure_receipt_missing") from exc
    except BrowserE2eError:
        raise
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise BrowserE2eError("functional_uat_browser_failure_receipt_invalid") from exc
    if (
        not isinstance(payload, Mapping)
        or set(payload) != _FAILURE_RECEIPT_KEYS
        or payload.get("schema_version") != _FAILURE_RECEIPT_SCHEMA
        or payload.get("status") != "failed"
        or not _positive_int(payload.get("failure_count"))
    ):
        raise BrowserE2eError("functional_uat_browser_failure_receipt_invalid")
    first_failure = payload.get("first_failure")
    if first_failure is None:
        return BrowserFailureReceipt(failure_count=payload["failure_count"], first_failure=None)
    if (
        not isinstance(first_failure, Mapping)
        or set(first_failure) != _FAILURE_LOCATION_KEYS
        or first_failure.get("file") != _FAILURE_SPEC_FILE
        or not _positive_int(first_failure.get("line"))
        or not _positive_int(first_failure.get("column"))
    ):
        raise BrowserE2eError("functional_uat_browser_failure_receipt_invalid")
    return BrowserFailureReceipt(
        failure_count=payload["failure_count"],
        first_failure=BrowserFailureLocation(
            file=first_failure["file"],
            line=first_failure["line"],
            column=first_failure["column"],
        ),
    )


def _verify_transcripts(receipt: BrowserReceipt) -> int:
    verified_count = 0
    for review_run_id in receipt.review_run_ids:
        try:
            result = verify_run(workspace_transcript_root(), review_run_id)
        except ScenarioTranscriptError as exc:
            raise BrowserE2eError("functional_uat_browser_transcript_invalid") from exc
        if result.get("profile") != "functional-uat" or result.get("status") != "completed":
            raise BrowserE2eError("functional_uat_browser_transcript_invalid")
        verified_count += 1
    return verified_count


def run(
    *,
    backend_api_url: str = _BACKEND_API_URL,
    frontend_url: str = _FRONTEND_URL,
    profile_file: Path | None = None,
    receipt_root: Path | None = None,
    runner: Any = subprocess.run,
) -> dict[str, Any]:
    """Execute the Frontend-owned Browser journey and return a safe summary only."""

    workspace = _workspace_root()
    profile_path = profile_file or (
        workspace / "_local" / "profiles" / "functional-uat" / "backend.env"
    )
    output_root = receipt_root or (workspace / "_local" / "state" / "e2e-runs")
    _validate_workspace_main(workspace)
    _validate_frontend_command(workspace / "customs-frontend")
    _validate_readiness(backend_api_url=backend_api_url, frontend_url=frontend_url)
    profile = _load_profile(_profile_path(str(profile_path)))
    try:
        safe_root = _receipt_root(str(output_root))
    except argparse.ArgumentTypeError as exc:
        raise BrowserE2eError("functional_uat_browser_receipt_root_unsafe") from exc
    receipt_path = _receipt_path(safe_root)
    failure_path = _failure_receipt_path(
        receipt_path=receipt_path, receipt_root=safe_root, require_absent=True
    )
    try:
        completed = runner(
            (_corepack_command(), "npm", "run", "functional-uat:browser"),
            cwd=workspace / "customs-frontend",
            env=_child_environment(
                profile=profile,
                frontend_url=frontend_url,
                receipt_path=receipt_path,
                failure_path=failure_path,
            ),
            check=False,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=900,
        )
    except subprocess.TimeoutExpired as exc:
        raise BrowserE2eError("functional_uat_browser_command_timed_out") from exc
    except OSError as exc:
        raise BrowserE2eError("functional_uat_browser_command_unavailable") from exc
    if completed.returncode != 0:
        _failure_receipt_path(
            receipt_path=receipt_path, receipt_root=safe_root, require_absent=False
        )
        failure = _load_failure_receipt(failure_path)
        raise BrowserE2eError(
            "functional_uat_browser_command_failed", first_failure=failure.first_failure
        )
    _validated_run_directory(receipt_path=receipt_path, receipt_root=safe_root)
    receipt = _load_receipt(receipt_path)
    transcripts_verified = _verify_transcripts(receipt)
    return {
        "status": "passed",
        "profile": "functional-uat",
        "checks_verified": len(receipt.checks),
        "transcripts_verified": transcripts_verified,
    }


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--backend-api-url", type=_backend_api_url, default=_BACKEND_API_URL)
    parser.add_argument("--frontend-url", type=_frontend_url, default=_FRONTEND_URL)
    parser.add_argument(
        "--profile-file",
        type=_profile_path,
        default=_workspace_root() / "_local" / "profiles" / "functional-uat" / "backend.env",
    )
    parser.add_argument(
        "--receipt-root",
        type=_receipt_root,
        default=_workspace_root() / "_local" / "state" / "e2e-runs",
    )
    args = parser.parse_args(argv)
    try:
        result = run(
            backend_api_url=args.backend_api_url,
            frontend_url=args.frontend_url,
            profile_file=args.profile_file,
            receipt_root=args.receipt_root,
        )
    except BrowserE2eError as exc:
        payload: dict[str, Any] = {"functional_uat_browser_e2e": "failed", "reason": str(exc)}
        if exc.first_failure is not None:
            payload["first_failure"] = {
                "file": exc.first_failure.file,
                "line": exc.first_failure.line,
            }
        print(json.dumps(payload, ensure_ascii=False))
        return 1
    print(json.dumps({"functional_uat_browser_e2e": "passed", **result}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
