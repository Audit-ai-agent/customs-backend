from __future__ import annotations

import hashlib
import json
from functools import lru_cache
from importlib import resources
from typing import Any, Literal

from jsonschema import Draft202012Validator
from jsonschema.exceptions import SchemaError, ValidationError as JsonSchemaValidationError
from pydantic import BaseModel, ConfigDict, Field

from customs_backend.domain import canonical_json_bytes


ExperienceContractName = Literal[
    "agent-uat-capability-v1",
    "assistant-answer-v2",
    "case-conversation-request-v2",
    "review-experience-envelope-v1",
    "review-revision-experience-result-v1",
    "review-result-v3",
    "review-stage-event-v1",
]
_SCHEMA_FILENAMES: dict[ExperienceContractName, str] = {
    "agent-uat-capability-v1": "agent-uat-capability-v1.schema.json",
    "assistant-answer-v2": "assistant-answer-v2.schema.json",
    "case-conversation-request-v2": "case-conversation-request-v2.schema.json",
    "review-experience-envelope-v1": "review-experience-envelope-v1.schema.json",
    "review-revision-experience-result-v1": "review-revision-experience-result-v1.schema.json",
    "review-stage-event-v1": "review-stage-event-v1.schema.json",
}
_RESOURCE_DIRECTORY = "contracts/agent/service/v3"
_PROVIDER_REVIEW_RESULT_V3_RESOURCE = f"{_RESOURCE_DIRECTORY}/review-result-v3.schema.json"
_LOCK_RESOURCE = "agent_experience_contract.lock.json"
_PROVIDER_OPENAPI_LOCK_RESOURCE = f"{_RESOURCE_DIRECTORY}/openapi.lock.json"
_EXPECTED_PROVIDER_PATHS = {
    "/health/live": ["get"],
    "/health/ready": ["get"],
    "/internal/agent/v1/metadata": ["get"],
    "/internal/agent/v1/metrics": ["get"],
    "/internal/agent/v1/reviews": ["post"],
    "/internal/agent/v2/answers": ["post"],
    "/internal/agent/v2/metadata": ["get"],
    "/internal/agent/v2/reviews": ["post"],
    "/internal/agent/v2/revisions": ["post"],
    "/internal/agent/v3/answers": ["post"],
    "/internal/agent/v3/answers/stream": ["post"],
    "/internal/agent/v3/metadata": ["get"],
    "/internal/agent/v3/reviews": ["post"],
    "/internal/agent/v3/reviews/stream": ["post"],
    "/internal/agent/v3/revisions": ["post"],
}
_EXPECTED_PROMPT_IDENTITIES = frozenset(
    {"presentation", "entailment", "chat", "chat_verifier", "scope_classifier"}
)
_EXPECTED_OUTPUT_SCHEMA_IDENTITIES = frozenset(
    {
        "presentation",
        "entailment",
        "chat",
        "chat_verifier",
        "scope_classifier",
        "scope_classifier_semantic",
    }
)


class ExperienceSchemaIdentity(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    canonical_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    file_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")


class ExperienceAssetIdentity(ExperienceSchemaIdentity):
    media_type: Literal["application/json"]


class MeaningCatalogEntryV1(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    title: str = Field(min_length=1, max_length=200)
    explanation: str = Field(min_length=1, max_length=2_000)
    impact: str = Field(min_length=1, max_length=2_000)
    next_action: str = Field(min_length=1, max_length=2_000)


class MeaningCatalogV1(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    version: Literal["customs-user-meaning-catalog/v2"]
    issues: dict[str, MeaningCatalogEntryV1]
    findings: dict[str, MeaningCatalogEntryV1]
    actions: dict[str, str]
    general_guidance: dict[str, str]


class ExperienceContractLock(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal["customs-backend-agent-experience-consumer-lock/v1"]
    provider_commit: str = Field(pattern=r"^[a-f0-9]{40}$")
    provider_manifest_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    provider_asset_manifest_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    provider_openapi_lock_file_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    provider_openapi_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    provider_review_result_v3: ExperienceSchemaIdentity
    provider_runtime_tool_registry_id: Literal["customs-agent-tools/text-v1"]
    meaning_catalog_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    meaning_catalog_file_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    experience_prompt_bundle_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    experience_schema_bundle_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    experience_prompt_sha256: dict[str, str]
    experience_output_schema_sha256: dict[str, str]
    uat_tool_registry_id: Literal["customs-agent-tools/text-uat-v2"]
    uat_tool_registry_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    schemas: dict[str, ExperienceSchemaIdentity]

    @property
    def prompt_identities(self) -> dict[str, str]:
        return self.experience_prompt_sha256

    @property
    def output_schema_identities(self) -> dict[str, str]:
        return self.experience_output_schema_sha256


class ExperienceContractBundle(BaseModel):
    model_config = ConfigDict(arbitrary_types_allowed=True, frozen=True)

    lock: ExperienceContractLock
    validators: dict[str, Any]
    meaning_catalog: MeaningCatalogV1


def _resource_bytes(relative_path: str) -> bytes:
    return resources.files("customs_backend").joinpath(relative_path).read_bytes()


def _sha256(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _decode_object(value: bytes, *, name: str) -> dict[str, Any]:
    try:
        parsed = json.loads(
            value.decode("utf-8"),
            parse_constant=lambda token: (_ for _ in ()).throw(
                ValueError(f"non-finite JSON constant {token}")
            ),
            object_pairs_hook=_reject_duplicate_keys,
        )
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
        raise RuntimeError(f"{name} is not canonical JSON") from exc
    if not isinstance(parsed, dict):
        raise RuntimeError(f"{name} must be a JSON object")
    return parsed


def _reject_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    value: dict[str, Any] = {}
    for key, item in pairs:
        if key in value:
            raise ValueError(f"duplicate JSON key: {key}")
        value[key] = item
    return value


@lru_cache(maxsize=1)
def validate_agent_experience_contract_bundle() -> ExperienceContractBundle:
    lock = ExperienceContractLock.model_validate(
        _decode_object(_resource_bytes(_LOCK_RESOURCE), name="experience consumer lock")
    )
    if (
        set(lock.experience_prompt_sha256) != _EXPECTED_PROMPT_IDENTITIES
        or set(lock.experience_output_schema_sha256) != _EXPECTED_OUTPUT_SCHEMA_IDENTITIES
        or any(
            not isinstance(value, str)
            or len(value) != 64
            or any(character not in "0123456789abcdef" for character in value)
            for value in (
                *lock.experience_prompt_sha256.values(),
                *lock.experience_output_schema_sha256.values(),
            )
        )
    ):
        raise RuntimeError("Agent experience task identity inventory drifted")
    openapi_lock_bytes = _resource_bytes(_PROVIDER_OPENAPI_LOCK_RESOURCE)
    if _sha256(openapi_lock_bytes) != lock.provider_openapi_lock_file_sha256:
        raise RuntimeError("Agent service OpenAPI lock bytes drifted")
    provider_openapi_lock = _decode_object(
        openapi_lock_bytes,
        name="Agent service OpenAPI lock",
    )
    if (
        provider_openapi_lock.get("schema_version") != "customs-agent-service-openapi-lock/v1"
        or provider_openapi_lock.get("canonical_sha256") != lock.provider_openapi_sha256
        or provider_openapi_lock.get("paths") != _EXPECTED_PROVIDER_PATHS
    ):
        raise RuntimeError("Agent service OpenAPI identity or route inventory drifted")
    manifest_bytes = _resource_bytes(f"{_RESOURCE_DIRECTORY}/manifest.json")
    if _sha256(manifest_bytes) != lock.provider_manifest_sha256:
        raise RuntimeError("Agent experience provider manifest bytes drifted")
    manifest = _decode_object(manifest_bytes, name="experience provider manifest")
    if manifest.get("schema_version") != "customs-agent-experience-contract-manifest/v1":
        raise RuntimeError("Agent experience provider manifest version drifted")
    raw_identities = manifest.get("schemas")
    if not isinstance(raw_identities, dict):
        raise RuntimeError("Agent experience provider manifest has no Schema identities")
    manifest_identities = {
        name: ExperienceSchemaIdentity.model_validate(identity)
        for name, identity in raw_identities.items()
    }
    if manifest_identities != lock.schemas:
        raise RuntimeError("Agent experience provider manifest differs from the consumer lock")
    if set(manifest_identities) != set(_SCHEMA_FILENAMES.values()):
        raise RuntimeError("Agent experience provider manifest inventory is incomplete")

    asset_manifest_bytes = _resource_bytes(f"{_RESOURCE_DIRECTORY}/asset-manifest.json")
    if _sha256(asset_manifest_bytes) != lock.provider_asset_manifest_sha256:
        raise RuntimeError("Agent experience asset manifest bytes drifted")
    asset_manifest = _decode_object(
        asset_manifest_bytes,
        name="experience provider asset manifest",
    )
    if asset_manifest.get("schema_version") != "customs-agent-experience-asset-manifest/v1":
        raise RuntimeError("Agent experience asset manifest version drifted")
    raw_assets = asset_manifest.get("assets")
    expected_assets = {"meaning-catalog-v1.json", "meaning-catalog-v2.json"}
    if not isinstance(raw_assets, dict) or set(raw_assets) != expected_assets:
        raise RuntimeError("Agent experience provider asset inventory is incomplete")
    asset_identities = {
        name: ExperienceAssetIdentity.model_validate(raw_assets[name])
        for name in sorted(expected_assets)
    }
    for name, asset_identity in asset_identities.items():
        asset_bytes = _resource_bytes(f"{_RESOURCE_DIRECTORY}/{name}")
        if _sha256(asset_bytes) != asset_identity.file_sha256:
            raise RuntimeError(f"Agent experience asset bytes drifted: {name}")
        asset_object = _decode_object(asset_bytes, name=f"Agent experience asset {name}")
        if _sha256(canonical_json_bytes(asset_object)) != asset_identity.canonical_sha256:
            raise RuntimeError(f"Agent canonical experience asset drifted: {name}")
    catalog_identity = asset_identities["meaning-catalog-v2.json"]
    if (
        catalog_identity.canonical_sha256 != lock.meaning_catalog_sha256
        or catalog_identity.file_sha256 != lock.meaning_catalog_file_sha256
    ):
        raise RuntimeError("Agent meaning catalog identity differs from the consumer lock")
    catalog_bytes = _resource_bytes(f"{_RESOURCE_DIRECTORY}/meaning-catalog-v2.json")
    catalog_object = _decode_object(catalog_bytes, name="Agent meaning catalog")
    if _sha256(canonical_json_bytes(catalog_object)) != catalog_identity.canonical_sha256:
        raise RuntimeError("Agent canonical meaning catalog drifted")
    meaning_catalog = MeaningCatalogV1.model_validate(catalog_object)

    validators: dict[str, Draft202012Validator] = {}
    for contract_name, filename in _SCHEMA_FILENAMES.items():
        schema_bytes = _resource_bytes(f"{_RESOURCE_DIRECTORY}/{filename}")
        identity = lock.schemas[filename]
        if _sha256(schema_bytes) != identity.file_sha256:
            raise RuntimeError(f"Agent experience Schema bytes drifted: {filename}")
        schema = _decode_object(schema_bytes, name=filename)
        if _sha256(canonical_json_bytes(schema)) != identity.canonical_sha256:
            raise RuntimeError(f"Agent experience canonical Schema drifted: {filename}")
        try:
            Draft202012Validator.check_schema(schema)
        except SchemaError as exc:
            raise RuntimeError(f"Agent experience Schema is invalid: {filename}") from exc
        validators[contract_name] = Draft202012Validator(schema)
    review_result_bytes = _resource_bytes(_PROVIDER_REVIEW_RESULT_V3_RESOURCE)
    review_result_identity = lock.provider_review_result_v3
    if _sha256(review_result_bytes) != review_result_identity.file_sha256:
        raise RuntimeError("Agent provider V3 review-result Schema bytes drifted")
    review_result_schema = _decode_object(
        review_result_bytes,
        name="Agent provider V3 review-result Schema",
    )
    if (
        _sha256(canonical_json_bytes(review_result_schema))
        != review_result_identity.canonical_sha256
    ):
        raise RuntimeError("Agent canonical provider V3 review-result Schema drifted")
    try:
        Draft202012Validator.check_schema(review_result_schema)
    except SchemaError as exc:
        raise RuntimeError("Agent provider V3 review-result Schema is invalid") from exc
    validators["review-result-v3"] = Draft202012Validator(review_result_schema)
    return ExperienceContractBundle(
        lock=lock,
        validators=validators,
        meaning_catalog=meaning_catalog,
    )


def validate_agent_experience_payload(
    contract_name: ExperienceContractName,
    payload: Any,
) -> None:
    validator = validate_agent_experience_contract_bundle().validators[contract_name]
    try:
        validator.validate(payload)
    except JsonSchemaValidationError as exc:
        raise ValueError(f"Agent experience payload violates {contract_name}") from exc


def expected_meaning_catalog_sha256() -> str:
    return validate_agent_experience_contract_bundle().lock.meaning_catalog_sha256


def provider_meaning_catalog() -> MeaningCatalogV1:
    return validate_agent_experience_contract_bundle().meaning_catalog


__all__ = [
    "ExperienceContractBundle",
    "ExperienceContractLock",
    "MeaningCatalogV1",
    "expected_meaning_catalog_sha256",
    "provider_meaning_catalog",
    "validate_agent_experience_contract_bundle",
    "validate_agent_experience_payload",
]
