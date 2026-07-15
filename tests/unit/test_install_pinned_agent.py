from __future__ import annotations

import hashlib
import json
import sys
import tomllib
import zipfile
from pathlib import Path

import pytest

import tools.install_pinned_agent as installer
from integration.ai_distribution import AI_DISTRIBUTION, AI_VERSION


ROOT = Path(__file__).resolve().parents[2]


def make_wheel(tmp_path, name="customs-ai-agent", version="0.1.0"):
    wheel = tmp_path / "agent.whl"
    with zipfile.ZipFile(wheel, "w") as archive:
        archive.writestr(
            "customs_ai_agent-0.1.0.dist-info/METADATA",
            f"Metadata-Version: 2.1\nName: {name}\nVersion: {version}\n",
        )
    return wheel


def config_for(wheel, **overrides):
    config = {
        "distribution": "customs-ai-agent",
        "version": "0.1.0",
        "wheel_sha256": hashlib.sha256(wheel.read_bytes()).hexdigest(),
    }
    config.update(overrides)
    return config


def test_distribution_normalization_follows_python_name_equivalence() -> None:
    assert installer.normalize_distribution("customs_ai.agent") == "customs-ai-agent"


def test_wheel_identity_reads_canonical_metadata(tmp_path) -> None:
    wheel = make_wheel(tmp_path)
    assert installer.wheel_identity(wheel) == ("customs-ai-agent", "0.1.0")
    assert installer.verify_wheel(wheel, config_for(wheel))[1:] == (
        "0.1.0",
        hashlib.sha256(wheel.read_bytes()).hexdigest(),
    )


def test_wheel_identity_rejects_ambiguous_metadata(tmp_path) -> None:
    wheel = tmp_path / "ambiguous.whl"
    with zipfile.ZipFile(wheel, "w") as archive:
        archive.writestr("one.dist-info/METADATA", "Name: one\nVersion: 1\n")
        archive.writestr("two.dist-info/METADATA", "Name: two\nVersion: 2\n")

    with pytest.raises(ValueError, match="exactly one"):
        installer.wheel_identity(wheel)


def test_verifier_rejects_hash_distribution_and_version_drift(tmp_path) -> None:
    wheel = make_wheel(tmp_path)
    with pytest.raises(ValueError, match="SHA-256 mismatch"):
        installer.verify_wheel(wheel, config_for(wheel, wheel_sha256="0" * 64))
    with pytest.raises(ValueError, match="distribution mismatch"):
        installer.verify_wheel(wheel, config_for(wheel, distribution="other-agent"))
    with pytest.raises(ValueError, match="version mismatch"):
        installer.verify_wheel(wheel, config_for(wheel, version="9.9.9"))


def test_verifier_rejects_corrupt_wheel_after_hash_check(tmp_path) -> None:
    wheel = tmp_path / "corrupt.whl"
    wheel.write_bytes(b"not-a-wheel")
    with pytest.raises(zipfile.BadZipFile):
        installer.verify_wheel(wheel, config_for(wheel))


def test_uv_environment_fallback(monkeypatch, tmp_path) -> None:
    executable = tmp_path / "uv.exe"
    executable.write_text("placeholder", encoding="utf-8")
    monkeypatch.setattr(installer.shutil, "which", lambda _: None)
    monkeypatch.setenv("UV", str(executable))
    assert installer.resolve_uv() == str(executable)


def test_main_verify_only_accepts_external_wheel_path(
    monkeypatch, tmp_path, capsys
) -> None:
    external_store = tmp_path / "external artifact store"
    external_store.mkdir()
    wheel = make_wheel(external_store)
    contract = config_for(wheel, source_commit="a" * 40)
    contract_path = tmp_path / "agent_contract.json"
    contract_path.write_text(json.dumps(contract), encoding="utf-8")
    monkeypatch.setattr(installer, "CONTRACT_PATH", contract_path)
    monkeypatch.setattr(
        sys, "argv", ["install_pinned_agent.py", str(wheel), "--verify-only"]
    )

    assert installer.main() == 0
    assert "verified AI Agent wheel" in capsys.readouterr().out


def test_agent_contract_is_single_runtime_source_and_matches_system_extra() -> None:
    contract = json.loads(
        (ROOT / "src" / "integration" / "agent_contract.json").read_text(encoding="utf-8")
    )
    with (ROOT / "pyproject.toml").open("rb") as handle:
        project = tomllib.load(handle)
    system = set(project["project"]["optional-dependencies"]["system"])

    assert AI_DISTRIBUTION == contract["distribution"]
    assert AI_VERSION == contract["version"]
    for name, version in contract["runtime_direct"].items():
        assert f"{name}=={version}" in system
