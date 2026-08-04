"""Backend consumer validation for the packaged Agent V3 schemas."""

from __future__ import annotations

import json
from functools import lru_cache
from importlib import resources
from typing import Any, Literal

from jsonschema import Draft202012Validator, FormatChecker
from jsonschema.exceptions import SchemaError, ValidationError


class BackendConfigurationError(RuntimeError):
    """A packaged contract schema is absent or malformed."""


class AgentContractPayloadError(ValueError):
    """A request or response does not match the Agent V3 schema."""


AgentContractKind = Literal["review_request", "review_result"]
_SCHEMA_PATHS: dict[AgentContractKind, tuple[str, ...]] = {
    "review_request": ("contracts", "agent", "v3", "review-request.schema.json"),
    "review_result": ("contracts", "agent", "v3", "review-result.schema.json"),
}


def _strict_json_object(raw: bytes, *, label: str) -> dict[str, Any]:
    if not raw or len(raw) > 1_000_000:
        raise BackendConfigurationError(f"{label} exceeds its JSON size limit")

    def pairs(values: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, value in values:
            if key in result:
                raise ValueError(f"duplicate JSON key: {key}")
            result[key] = value
        return result

    try:
        value = json.loads(
            raw,
            object_pairs_hook=pairs,
            parse_constant=lambda value: (_ for _ in ()).throw(
                ValueError(f"non-finite JSON number: {value}")
            ),
        )
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
        raise BackendConfigurationError(f"{label} is not strict JSON") from exc
    if not isinstance(value, dict):
        raise BackendConfigurationError(f"{label} must be a JSON object")
    return value


@lru_cache(maxsize=2)
def load_agent_contract_schema(kind: AgentContractKind) -> dict[str, Any]:
    try:
        raw = resources.files("customs_backend").joinpath(*_SCHEMA_PATHS[kind]).read_bytes()
    except OSError as exc:
        raise BackendConfigurationError("Agent V3 schema is missing") from exc
    schema = _strict_json_object(raw, label=f"Agent V3 {kind} schema")
    try:
        Draft202012Validator.check_schema(schema)
    except SchemaError as exc:
        raise BackendConfigurationError("Agent V3 schema is invalid") from exc
    return schema


def validate_agent_contract_bundle() -> None:
    load_agent_contract_schema("review_request")
    load_agent_contract_schema("review_result")


def validate_agent_v3_payload(kind: AgentContractKind, payload: object) -> None:
    try:
        Draft202012Validator(
            load_agent_contract_schema(kind), format_checker=FormatChecker()
        ).validate(payload)
    except ValidationError:
        raise AgentContractPayloadError(f"Agent V3 {kind} does not satisfy the packaged schema") from None
