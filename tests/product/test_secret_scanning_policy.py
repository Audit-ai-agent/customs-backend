from __future__ import annotations

import re
import tomllib
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
FINGERPRINT_PATTERN = re.compile(r"^[0-9a-f]{40}:[^:*]+:[^:*]+:[1-9][0-9]*$")


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

    assert all(FINGERPRINT_PATTERN.fullmatch(entry) for entry in entries)


def test_current_backend_needs_no_secret_scan_exception() -> None:
    assert _ignore_entries() == set()


def test_ignore_file_has_no_entry_for_currently_tracked_runtime_input() -> None:
    entries = _ignore_entries()

    assert all("_local" not in entry and "_artifacts" not in entry for entry in entries)


def test_ci_verifies_the_pinned_gitleaks_archive_before_scanning() -> None:
    workflow = (ROOT / ".github" / "workflows" / "unit.yml").read_text(encoding="utf-8")

    assert "551f6fc83ea457d62a0d98237cbad105af8d557003051f41f3e7ca7b3f2470eb" in workflow
    assert "sha256sum --check --status" in workflow
    assert "git archive --format=tar HEAD" in workflow
    assert './gitleaks dir "$scan_root" --config .gitleaks.toml --redact --no-banner --exit-code 1' in workflow
    assert "GITHUB_TOKEN" not in workflow
