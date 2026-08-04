from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

from tools.probe_installed_agent_http import _agent_python_executable, _assert_clean_agent_checkout


def test_agent_python_executable_preserves_virtualenv_symlink(tmp_path: Path) -> None:
    base_python = tmp_path / "base-python"
    base_python.write_bytes(b"python")
    virtualenv_python = tmp_path / "venv" / ("Scripts" if os.name == "nt" else "bin") / "python"
    virtualenv_python.parent.mkdir(parents=True)

    if os.name == "nt":
        virtualenv_python.write_bytes(base_python.read_bytes())
    else:
        virtualenv_python.symlink_to(base_python)

    executable = _agent_python_executable(virtualenv_python)

    assert executable == virtualenv_python.absolute()
    if os.name != "nt":
        assert executable != virtualenv_python.resolve(strict=True)


def test_agent_python_executable_rejects_missing_path(tmp_path: Path) -> None:
    with pytest.raises(
        RuntimeError,
        match="installed Agent Python executable is unavailable",
    ):
        _agent_python_executable(tmp_path / "missing-python")


def test_clean_agent_checkout_is_not_pinned_to_a_callers_git_sha(tmp_path: Path) -> None:
    composition = tmp_path / "tools" / "local_uat_agent_service" / "app.py"
    composition.parent.mkdir(parents=True)
    composition.write_text("# tracked composition\n", encoding="utf-8")
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    subprocess.run(["git", "add", "tools/local_uat_agent_service/app.py"], cwd=tmp_path, check=True)
    subprocess.run(
        [
            "git",
            "-c",
            "user.email=test@example.invalid",
            "-c",
            "user.name=Test",
            "commit",
            "-qm",
            "agent checkout",
        ],
        cwd=tmp_path,
        check=True,
    )

    assert _assert_clean_agent_checkout(tmp_path) == tmp_path.resolve()
