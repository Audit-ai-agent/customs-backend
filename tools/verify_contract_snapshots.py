"""Fail closed when Backend-owned contract snapshots are absent or drifted.

``git diff`` alone does not report an untracked snapshot.  This verifier first
requires every expected file to be present in Git's index, then requires the
contract snapshot path groups to be clean (including untracked files), validates
the packaged Agent review and assistant schemas, and finally compares a
freshly rendered OpenAPI document byte-for-byte with the tracked snapshot.
"""

from __future__ import annotations

import subprocess  # nosec B404 - fixed Git executable and fixed argv only
from collections.abc import Callable
from pathlib import Path

from customs_backend.agent_contract import validate_agent_contract_bundle
from customs_backend.guidance_registry import verify_guidance_registry_projection_bundle

if __package__:
    from tools.export_product_openapi import render_product_openapi
else:
    from export_product_openapi import render_product_openapi


ROOT = Path(__file__).resolve().parents[1]
OPENAPI_PATH = Path("docs/openapi-v1.json")
AGENT_SCHEMA_DIRECTORY = Path("src/customs_backend/contracts/agent/v3")
EVIDENCE_MANIFEST_DIRECTORY = Path("src/customs_backend/contracts/evidence/v3")
AGENT_SCHEMA_PATHS = (
    AGENT_SCHEMA_DIRECTORY / "review-request.schema.json",
    AGENT_SCHEMA_DIRECTORY / "review-result.schema.json",
    AGENT_SCHEMA_DIRECTORY / "assistant-answer-request.schema.json",
    AGENT_SCHEMA_DIRECTORY / "assistant-answer-response.schema.json",
    AGENT_SCHEMA_DIRECTORY / "knowledge-answer-request.schema.json",
    AGENT_SCHEMA_DIRECTORY / "knowledge-answer-response.schema.json",
)
EVIDENCE_MANIFEST_PATHS = (
    EVIDENCE_MANIFEST_DIRECTORY / "guidance_registry_v1.manifest.json",
    EVIDENCE_MANIFEST_DIRECTORY / "guidance_registry_v2.manifest.json",
)
TRACKED_FILES = (
    OPENAPI_PATH,
    *AGENT_SCHEMA_PATHS,
    *EVIDENCE_MANIFEST_PATHS,
)
CONTRACT_PATH_GROUPS = (
    OPENAPI_PATH,
    AGENT_SCHEMA_DIRECTORY,
    EVIDENCE_MANIFEST_DIRECTORY,
)


class ContractSnapshotVerificationError(RuntimeError):
    """A required contract snapshot is untracked, dirty, or non-canonical."""


def _git(root: Path, *arguments: str) -> subprocess.CompletedProcess[str]:
    # Fixed executable/argv, no shell, and no user-controlled arguments.
    return subprocess.run(  # nosec B603 B607
        ["git", *arguments],
        cwd=root,
        check=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )


def _path_arguments(paths: tuple[Path, ...]) -> list[str]:
    return [path.as_posix() for path in paths]


def verify_contract_snapshots(
    root: Path = ROOT,
    *,
    openapi_renderer: Callable[[], bytes] = render_product_openapi,
    contract_validator: Callable[[], object] = validate_agent_contract_bundle,
) -> None:
    resolved_root = root.resolve(strict=True)
    git_root_result = _git(resolved_root, "rev-parse", "--show-toplevel")
    if git_root_result.returncode != 0:
        raise ContractSnapshotVerificationError("Backend root is not a Git working tree")
    try:
        git_root = Path(git_root_result.stdout.strip()).resolve(strict=True)
    except (OSError, RuntimeError) as exc:
        raise ContractSnapshotVerificationError("Git returned an invalid repository root") from exc
    if git_root != resolved_root:
        raise ContractSnapshotVerificationError(
            "Verifier must run against the Backend repository root"
        )

    for relative_path in TRACKED_FILES:
        candidate = resolved_root / relative_path
        if not candidate.is_file():
            raise ContractSnapshotVerificationError(
                f"required contract snapshot is missing: {relative_path.as_posix()}"
            )
        tracked = _git(
            resolved_root,
            "ls-files",
            "--error-unmatch",
            "--",
            relative_path.as_posix(),
        )
        if tracked.returncode != 0:
            raise ContractSnapshotVerificationError(
                f"required contract snapshot is not Git-tracked: {relative_path.as_posix()}"
            )

    status = _git(
        resolved_root,
        "status",
        "--porcelain=v1",
        "--untracked-files=all",
        "--",
        *_path_arguments(CONTRACT_PATH_GROUPS),
    )
    if status.returncode != 0:
        raise ContractSnapshotVerificationError("Git could not inspect contract snapshot state")
    if status.stdout:
        raise ContractSnapshotVerificationError(
            "contract snapshot paths are not clean:\n" + status.stdout.rstrip()
        )

    # Validate the packaged Agent schema and exact public registry projections
    # after the Git checks.  Neither projection contains raw source material.
    contract_validator()
    verify_guidance_registry_projection_bundle()

    stored_openapi = (resolved_root / OPENAPI_PATH).read_bytes()
    rendered_openapi = openapi_renderer()
    if b"\r" in stored_openapi or not stored_openapi.endswith(b"\n"):
        raise ContractSnapshotVerificationError("OpenAPI snapshot is not canonical LF JSON")
    if stored_openapi.endswith(b"\n\n"):
        raise ContractSnapshotVerificationError("OpenAPI snapshot has multiple final newlines")
    if rendered_openapi != stored_openapi:
        raise ContractSnapshotVerificationError(
            "generated OpenAPI bytes differ from docs/openapi-v1.json"
        )

    final_status = _git(
        resolved_root,
        "status",
        "--porcelain=v1",
        "--untracked-files=all",
        "--",
        *_path_arguments(CONTRACT_PATH_GROUPS),
    )
    if final_status.returncode != 0 or final_status.stdout:
        raise ContractSnapshotVerificationError(
            "contract paths changed while snapshots were being verified"
        )


def main() -> None:
    verify_contract_snapshots()
    print("Backend contract snapshots tracked/clean/canonical: PASS")


if __name__ == "__main__":
    main()
