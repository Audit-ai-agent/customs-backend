from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from tools import verify_contract_snapshots as VERIFIER


def _git(root: Path, *arguments: str) -> None:
    subprocess.run(
        ["git", *arguments],
        cwd=root,
        check=True,
        capture_output=True,
        text=True,
    )


def _repository(tmp_path: Path) -> tuple[Path, bytes]:
    openapi = b'{"openapi":"3.1.0"}\n'
    files = {
        "docs/openapi-v1.json": openapi,
        "docs/openapi-v2.json": openapi,
        "src/customs_backend/agent_contract.lock.json": b"{}\n",
        "src/customs_backend/agent_experience_contract.lock.json": b"{}\n",
        "src/customs_backend/contracts/agent/v3/review-request.schema.json": b"{}\n",
        "src/customs_backend/contracts/agent/v3/review-result.schema.json": b"{}\n",
    }
    for filename in (
        "manifest.json",
        "asset-manifest.json",
        "meaning-catalog-v1.json",
        "openapi.lock.json",
        "agent-uat-capability-v1.schema.json",
        "assistant-answer-v2.schema.json",
        "case-conversation-request-v2.schema.json",
        "review-experience-envelope-v1.schema.json",
        "review-stage-event-v1.schema.json",
    ):
        files[f"src/customs_backend/contracts/agent/service/v3/{filename}"] = b"{}\n"
    for relative, content in files.items():
        path = tmp_path / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)
    _git(tmp_path, "init", "--initial-branch=main")
    _git(tmp_path, "config", "user.email", "contract-gate@example.invalid")
    _git(tmp_path, "config", "user.name", "Contract Gate")
    _git(tmp_path, "add", ".")
    _git(tmp_path, "commit", "-m", "seed snapshots")
    return tmp_path, openapi


def test_contract_snapshot_gate_accepts_tracked_clean_exact_bytes(tmp_path: Path) -> None:
    root, openapi = _repository(tmp_path)
    VERIFIER.verify_contract_snapshots(
        root,
        openapi_renderer=lambda: openapi,
        contract_validator=lambda: None,
        experience_contract_validator=lambda: None,
    )


@pytest.mark.parametrize("failure", ["untracked", "dirty", "generated-drift"])
def test_contract_snapshot_gate_fails_closed(tmp_path: Path, failure: str) -> None:
    root, openapi = _repository(tmp_path)

    def exact_renderer() -> bytes:
        return openapi

    renderer = exact_renderer
    if failure == "untracked":
        _git(root, "rm", "--cached", "docs/openapi-v2.json")
    elif failure == "dirty":
        (root / "src/customs_backend/agent_contract.lock.json").write_bytes(b'{"dirty":true}\n')
    else:

        def drift_renderer() -> bytes:
            return b'{"openapi":"different"}\n'

        renderer = drift_renderer

    with pytest.raises(VERIFIER.ContractSnapshotVerificationError):
        VERIFIER.verify_contract_snapshots(
            root,
            openapi_renderer=renderer,
            contract_validator=lambda: None,
            experience_contract_validator=lambda: None,
        )
