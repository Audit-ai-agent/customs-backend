from __future__ import annotations

import os
from pathlib import Path

import pytest

from tools.probe_installed_agent_http import _agent_python_executable


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
