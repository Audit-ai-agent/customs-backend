from __future__ import annotations

import json
import re
import tomllib
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
EXPERIENCE_LOCK_PATH = "src/customs_backend/agent_experience_contract.lock.json"
REVIEWED_COMMITS = {
    "79fd7a543af0d203a8f1689a82fe41bcca7ac398",
    "8fbbfd6b66a58bfa9394f4fcdcc3f510ccffe7e7",
    "4dfce1f082e6f74a08da2f2f79d21ff8ca1ee4a1",
    "0efa95649d8218c3836f0244e4a2085b067b5ab7",
}
REVIEWED_LINES = {24, 25}
EXPECTED_EXPERIENCE_LOCK_IGNORES = {
    f"{commit}:{EXPERIENCE_LOCK_PATH}:generic-api-key:{line}"
    for commit in REVIEWED_COMMITS
    for line in REVIEWED_LINES
}
FINGERPRINT_PATTERN = re.compile(r"^[0-9a-f]{40}:[^:*]+:generic-api-key:[1-9][0-9]*$")


def _ignore_entries() -> set[str]:
    return {
        line.strip()
        for line in (ROOT / ".gitleaksignore").read_text(encoding="utf-8").splitlines()
        if line.strip() and not line.lstrip().startswith("#")
    }


def test_secret_scan_has_no_broad_allowlist() -> None:
    config = tomllib.loads((ROOT / ".gitleaks.toml").read_text(encoding="utf-8"))

    assert "allowlists" not in config
    assert all("allowlists" not in rule for rule in config.get("rules", []))


def test_every_ignore_is_bound_to_an_exact_commit_fingerprint() -> None:
    entries = _ignore_entries()

    assert entries
    assert all(FINGERPRINT_PATTERN.fullmatch(entry) for entry in entries)


def test_experience_lock_ignores_are_limited_to_reviewed_findings() -> None:
    entries = _ignore_entries()
    experience_entries = {entry for entry in entries if EXPERIENCE_LOCK_PATH in entry}

    assert experience_entries == EXPECTED_EXPERIENCE_LOCK_IGNORES
    assert len(entries) == 12


def test_current_experience_lock_lines_are_public_sha256_provenance() -> None:
    lock_file = ROOT / EXPERIENCE_LOCK_PATH
    lines = lock_file.read_text(encoding="utf-8").splitlines()
    payload = json.loads(lock_file.read_text(encoding="utf-8"))
    expected_keys = {
        24: "provider_openapi_lock_file_sha256",
        25: "provider_openapi_sha256",
    }

    for line_number, key in expected_keys.items():
        match = re.fullmatch(
            rf'\s*"{key}":\s*"([0-9a-f]{{64}})",?',
            lines[line_number - 1],
        )
        assert match is not None
        assert payload[key] == match.group(1)
