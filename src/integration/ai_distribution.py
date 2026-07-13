"""Exact distribution guard for the separately built AI Agent package."""

from __future__ import annotations

from importlib.metadata import PackageNotFoundError, version


AI_DISTRIBUTION = "customs-ai-agent"
AI_VERSION = "0.1.0"


def require_compatible_ai_agent() -> None:
    """Fail before a system run when the pinned Agent wheel is absent or wrong."""

    try:
        installed = version(AI_DISTRIBUTION)
    except PackageNotFoundError as exc:
        raise RuntimeError(
            "customs-ai-agent==0.1.0 is required for system/demo commands; "
            "install the wheel built from the pinned Agent commit"
        ) from exc
    if installed != AI_VERSION:
        raise RuntimeError(
            f"{AI_DISTRIBUTION}=={AI_VERSION} is required; installed version is {installed}"
        )
