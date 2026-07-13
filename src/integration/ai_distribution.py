"""Exact distribution guard for the separately built AI Agent package."""

from __future__ import annotations

import json
from importlib.metadata import PackageNotFoundError, version
from importlib.resources import files


AGENT_CONTRACT = json.loads(
    files("integration").joinpath("agent_contract.json").read_text(encoding="utf-8")
)
AI_DISTRIBUTION = str(AGENT_CONTRACT["distribution"])
AI_VERSION = str(AGENT_CONTRACT["version"])


def require_compatible_ai_agent() -> None:
    """Fail before a system run when the pinned Agent wheel is absent or wrong."""

    try:
        installed = version(AI_DISTRIBUTION)
    except PackageNotFoundError as exc:
        raise RuntimeError(
            f"{AI_DISTRIBUTION}=={AI_VERSION} is required for system/demo commands; "
            "install the wheel built from the pinned Agent commit"
        ) from exc
    if installed != AI_VERSION:
        raise RuntimeError(
            f"{AI_DISTRIBUTION}=={AI_VERSION} is required; installed version is {installed}"
        )
