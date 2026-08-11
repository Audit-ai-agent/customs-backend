"""Backend consumer validation for the packaged Agent V3.2 schemas."""

from __future__ import annotations

import json
from functools import lru_cache
from importlib import resources
from typing import Any, Literal

from jsonschema import Draft202012Validator, FormatChecker
from jsonschema.exceptions import SchemaError, ValidationError
from referencing import Registry, Resource
from referencing.jsonschema import DRAFT202012


class BackendConfigurationError(RuntimeError):
    """A packaged contract schema is absent or malformed."""


class AgentContractPayloadError(ValueError):
    """A request or response does not match the packaged Agent schema."""


AgentContractKind = Literal[
    "review_request",
    "review_result",
    "assistant_answer_request",
    "assistant_answer_response",
    "knowledge_answer_request",
    "knowledge_answer_response",
    "document_extraction_request",
    "document_extraction_result",
]
_SCHEMA_PATHS: dict[AgentContractKind, tuple[str, ...]] = {
    "review_request": ("contracts", "agent", "v3", "review-request.schema.json"),
    "review_result": ("contracts", "agent", "v3", "review-result.schema.json"),
    "assistant_answer_request": (
        "contracts",
        "agent",
        "v3",
        "assistant-answer-request.schema.json",
    ),
    "assistant_answer_response": (
        "contracts",
        "agent",
        "v3",
        "assistant-answer-response.schema.json",
    ),
    "knowledge_answer_request": (
        "contracts",
        "agent",
        "v3",
        "knowledge-answer-request.schema.json",
    ),
    "knowledge_answer_response": (
        "contracts",
        "agent",
        "v3",
        "knowledge-answer-response.schema.json",
    ),
    "document_extraction_request": (
        "contracts",
        "agent",
        "v3",
        "document-extraction-request.schema.json",
    ),
    "document_extraction_result": (
        "contracts",
        "agent",
        "v3",
        "document-extraction-result.schema.json",
    ),
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


@lru_cache(maxsize=8)
def load_agent_contract_schema(kind: AgentContractKind) -> dict[str, Any]:
    try:
        raw = resources.files("customs_backend").joinpath(*_SCHEMA_PATHS[kind]).read_bytes()
    except OSError as exc:
        raise BackendConfigurationError("Agent V3 contract schema is missing") from exc
    schema = _strict_json_object(raw, label=f"Agent V3 {kind} schema")
    try:
        Draft202012Validator.check_schema(schema)
    except SchemaError as exc:
        raise BackendConfigurationError("Agent V3 contract schema is invalid") from exc
    return schema


def validate_agent_contract_bundle() -> None:
    load_agent_contract_schema("review_request")
    load_agent_contract_schema("review_result")
    load_agent_contract_schema("assistant_answer_request")
    load_agent_contract_schema("assistant_answer_response")
    load_agent_contract_schema("knowledge_answer_request")
    load_agent_contract_schema("knowledge_answer_response")
    load_agent_contract_schema("document_extraction_request")
    load_agent_contract_schema("document_extraction_result")


@lru_cache(maxsize=10)
def _agent_contract_validator(kind: AgentContractKind) -> Draft202012Validator:
    """Build a local-only registry for the assistant request's relative ref."""

    registry = Registry()
    for candidate in _SCHEMA_PATHS:
        schema = load_agent_contract_schema(candidate)
        schema_id = schema.get("$id")
        if not isinstance(schema_id, str) or not schema_id:
            raise BackendConfigurationError("Agent V3 contract schema has no canonical identifier")
        registry = registry.with_resource(
            schema_id,
            Resource.from_contents(schema, default_specification=DRAFT202012),
        )
    return Draft202012Validator(
        load_agent_contract_schema(kind), registry=registry, format_checker=FormatChecker()
    )


def validate_agent_v3_payload(kind: AgentContractKind, payload: object) -> None:
    try:
        _agent_contract_validator(kind).validate(payload)
    except ValidationError:
        raise AgentContractPayloadError(
            f"Agent V3 {kind} does not satisfy the packaged schema"
        ) from None
