"""Approver-only LLM settings proxied to the Agent's internal settings surface.

The Backend never holds an LLM credential and never persists the choice
itself: it forwards an approver's non-secret request (provider, model,
output limit, timeout) to the Agent, which validates it against the
credentials that process actually has, applies it to the next request, and
reports the effective configuration back.  Every value here is therefore the
Agent's view, re-validated at this boundary so a malformed Agent answer can
never reach the Browser as a trusted settings object.
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator, model_validator

from customs_backend.ports import AgentResponseRejectedError

LlmProvider = Literal["openai", "anthropic"]


class LlmSettingsUpdateRequest(BaseModel):
    """Non-secret override; ``PUT`` requires at least one field, ``test`` allows none."""

    model_config = ConfigDict(extra="forbid")

    provider: LlmProvider | None = None
    model: str | None = Field(default=None, min_length=1, max_length=128)
    max_tokens: int | None = Field(default=None, ge=256, le=16_384)
    timeout_seconds: float | None = Field(default=None, ge=1, le=300)

    @field_validator("model")
    @classmethod
    def _strip_model(cls, value: str | None) -> str | None:
        if value is None:
            return None
        stripped = value.strip()
        if not stripped:
            raise ValueError("model must not be blank")
        return stripped

    def is_empty(self) -> bool:
        return (
            self.provider is None
            and self.model is None
            and self.max_tokens is None
            and self.timeout_seconds is None
        )

    def to_agent_wire(self) -> dict[str, Any]:
        return self.model_dump(exclude_none=True)


class LlmSettingsChangeRequest(LlmSettingsUpdateRequest):
    """The ``PUT`` body: an override that changes at least one field."""

    @model_validator(mode="after")
    def _at_least_one_field(self) -> "LlmSettingsChangeRequest":
        if self.is_empty():
            raise ValueError("at least one setting must be supplied")
        return self


class LlmProviderAvailabilityView(BaseModel):
    model_config = ConfigDict(extra="forbid")

    provider: LlmProvider
    credential_present: bool
    default_model: str = Field(min_length=1, max_length=128)


class LlmSettingsView(BaseModel):
    """The Agent's effective, non-secret LLM configuration."""

    model_config = ConfigDict(extra="forbid")

    provider: LlmProvider
    model: str = Field(min_length=1, max_length=128)
    max_tokens: int = Field(ge=1, le=1_000_000)
    timeout_seconds: float = Field(ge=0, le=3600)
    source: Literal["environment", "override"]
    version: int = Field(ge=0)
    updated_at: str | None = Field(default=None, max_length=64)
    error_code: str | None = Field(default=None, max_length=64)
    service_mode: str = Field(min_length=1, max_length=64)
    adjustable: bool
    providers: list[LlmProviderAvailabilityView] = Field(max_length=8)


class LlmConnectionTestView(BaseModel):
    """Outcome of one minimal provider request; never carries provider text."""

    model_config = ConfigDict(extra="forbid")

    ok: bool
    provider: str | None = Field(default=None, max_length=32)
    model: str | None = Field(default=None, max_length=128)
    latency_ms: int | None = Field(default=None, ge=0)
    error_code: str | None = Field(default=None, max_length=64)


def decode_llm_settings_view(raw: dict[str, Any]) -> LlmSettingsView:
    """Validate the Agent's settings payload before it becomes a public view."""

    try:
        return LlmSettingsView.model_validate(raw)
    except ValidationError as exc:
        raise AgentResponseRejectedError("Agent LLM settings payload is invalid") from exc


def decode_llm_connection_test_view(raw: dict[str, Any]) -> LlmConnectionTestView:
    try:
        return LlmConnectionTestView.model_validate(raw)
    except ValidationError as exc:
        raise AgentResponseRejectedError("Agent LLM connection test payload is invalid") from exc


__all__ = [
    "LlmConnectionTestView",
    "LlmProvider",
    "LlmProviderAvailabilityView",
    "LlmSettingsChangeRequest",
    "LlmSettingsUpdateRequest",
    "LlmSettingsView",
    "decode_llm_connection_test_view",
    "decode_llm_settings_view",
]
