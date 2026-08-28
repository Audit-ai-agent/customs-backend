from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from tools import verify_contract_snapshots as verifier


def _git(root: Path, *arguments: str) -> None:
    subprocess.run(["git", *arguments], cwd=root, check=True, capture_output=True, text=True)


def _repository(tmp_path: Path) -> tuple[Path, bytes]:
    openapi = b'{"openapi":"3.1.0"}\n'
    files = {
        "docs/openapi-v1.json": openapi,
        "src/customs_backend/contracts/agent/v3/review-request.schema.json": b"{}\n",
        "src/customs_backend/contracts/agent/v3/review-result.schema.json": b"{}\n",
        "src/customs_backend/contracts/agent/v3/assistant-answer-request.schema.json": b"{}\n",
        "src/customs_backend/contracts/agent/v3/assistant-answer-response.schema.json": b"{}\n",
        "src/customs_backend/contracts/agent/v3/knowledge-answer-request.schema.json": b"{}\n",
        "src/customs_backend/contracts/agent/v3/knowledge-answer-response.schema.json": b"{}\n",
        "src/customs_backend/contracts/agent/v3/document-extraction-request.schema.json": b"{}\n",
        "src/customs_backend/contracts/agent/v3/document-extraction-result.schema.json": b"{}\n",
        "src/customs_backend/contracts/agent/v3/raster-document-extraction-request.schema.json": b"{}\n",
        "src/customs_backend/contracts/agent/v3/raster-document-extraction-result.schema.json": b"{}\n",
        "src/customs_backend/contracts/agent/v3/raster-document-preview-status.schema.json": b"{}\n",
        "src/customs_backend/contracts/agent/v3/raster-text-extraction-request.schema.json": b"{}\n",
        "src/customs_backend/contracts/agent/v3/raster-text-extraction-result.schema.json": b"{}\n",
        "src/customs_backend/contracts/agent/v3/raster-text-preview-status.schema.json": b"{}\n",
        "src/customs_backend/contracts/evidence/v3/guidance_registry_v1.manifest.json": b"{}\n",
        "src/customs_backend/contracts/evidence/v3/guidance_registry_v2.manifest.json": b"{}\n",
    }
    for relative, content in files.items():
        path = tmp_path / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)
    _git(tmp_path, "init", "--initial-branch=main")
    _git(tmp_path, "config", "user.email", "contract-gate@example.invalid")
    _git(tmp_path, "config", "user.name", "Contract Gate")
    _git(tmp_path, "add", ".")
    _git(tmp_path, "commit", "-m", "seed")
    return tmp_path, openapi


def test_contract_snapshot_gate_accepts_clean_exact_bytes(tmp_path: Path) -> None:
    root, openapi = _repository(tmp_path)

    verifier.verify_contract_snapshots(
        root,
        openapi_renderer=lambda: openapi,
        contract_validator=lambda: None,
    )


@pytest.mark.parametrize("failure", ["untracked", "dirty", "generated-drift"])
def test_contract_snapshot_gate_fails_closed(tmp_path: Path, failure: str) -> None:
    root, openapi = _repository(tmp_path)

    def renderer() -> bytes:
        return openapi

    if failure == "untracked":
        _git(root, "rm", "--cached", "docs/openapi-v1.json")
    elif failure == "dirty":
        (root / "src/customs_backend/contracts/agent/v3/review-request.schema.json").write_bytes(
            b'{"dirty":true}\n'
        )
    else:

        def renderer() -> bytes:
            return b'{"openapi":"different"}\n'

    with pytest.raises(verifier.ContractSnapshotVerificationError):
        verifier.verify_contract_snapshots(
            root,
            openapi_renderer=renderer,
            contract_validator=lambda: None,
        )
