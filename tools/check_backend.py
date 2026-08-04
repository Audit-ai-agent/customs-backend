"""Small, explicit Backend check profiles.

The profiles intentionally reuse existing tools and tests.  They do not create
coverage, packages, containers, cross-repository checkouts, or permanent
artifacts.  Run them from the repository root in an already-synchronised
environment.
"""

from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path
from typing import Final


ROOT: Final = Path(__file__).resolve().parents[1]

PROFILES: Final[dict[str, tuple[tuple[str, ...], ...]]] = {
    "quick": (
        ("-m", "ruff", "check", "."),
        ("-m", "mypy"),
        (
            "-m",
            "pytest",
            "packages/operational_db/tests",
            "tests/unit",
            "--no-header",
            "-q",
        ),
    ),
    "contract": (
        ("tools/verify_contract_snapshots.py",),
        (
            "-m",
            "pytest",
            "tests/product/test_contract_snapshot_gate.py",
            "tests/product/test_agent_http_boundary.py",
            "--no-header",
            "-q",
        ),
    ),
    "candidate": (
        (
            "-m",
            "pytest",
            (
                "tests/product/test_commercial_agent_experience.py::"
                "test_v2_review_experience_and_multiturn_assistant_are_end_to_end_bound"
            ),
            (
                "tests/product/test_workbench_api.py::"
                "test_typed_revision_compiles_confirm_correct_support_and_terminal_decisions"
            ),
            "--no-header",
            "-q",
        ),
    ),
}


def _run(command: tuple[str, ...]) -> int:
    completed = subprocess.run((sys.executable, *command), cwd=ROOT, check=False)
    return completed.returncode


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("profile", choices=tuple(PROFILES))
    args = parser.parse_args()

    for command in PROFILES[args.profile]:
        result = _run(command)
        if result != 0:
            return result
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
