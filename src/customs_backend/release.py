from __future__ import annotations

import base64
import binascii
import hashlib
import json
from datetime import UTC, date, datetime, timedelta
from functools import lru_cache
from importlib import resources
from pathlib import Path
from typing import Any, Literal

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
from jsonschema import Draft202012Validator, FormatChecker
from jsonschema.exceptions import SchemaError, ValidationError
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from customs_backend.domain import ReleaseIdentityV3, canonical_digest


class ReleaseConfigurationError(RuntimeError):
    pass


class AgentContractPayloadError(ValueError):
    """A request/result does not satisfy the exact provider-owned V3 Schema."""


def _strict_json_object(raw: bytes, *, label: str, maximum_bytes: int) -> dict[str, Any]:
    if not raw or len(raw) > maximum_bytes:
        raise ReleaseConfigurationError(f"{label} exceeds its canonical JSON size limit")

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
        raise ReleaseConfigurationError(f"{label} is not strict canonical JSON") from exc
    if not isinstance(value, dict):
        raise ReleaseConfigurationError(f"{label} must be a JSON object")
    return value


def _canonical_bytes(value: dict[str, Any]) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")


def _timestamp(value: datetime) -> str:
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")


def _decode_base64(value: str, *, expected_length: int, label: str) -> bytes:
    try:
        decoded = base64.b64decode(value, validate=True)
    except (binascii.Error, ValueError) as exc:
        raise ValueError(f"{label} must be canonical base64") from exc
    if len(decoded) != expected_length:
        raise ValueError(f"{label} has an invalid byte length")
    return decoded


class AgentContractLock(BaseModel):
    model_config = ConfigDict(extra="forbid")

    contract: Literal["customs-ai-agent-v3-consumer-lock/v2"]
    review_request_v3_path: Literal["contracts/agent/v3/review-request.schema.json"]
    review_request_v3_bytes: int = Field(ge=1, le=1_000_000)
    review_request_v3_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    review_result_v3_path: Literal["contracts/agent/v3/review-result.schema.json"]
    review_result_v3_bytes: int = Field(ge=1, le=1_000_000)
    review_result_v3_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    service_openapi_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    service_api_version: Literal["internal-agent-service/v2"]


@lru_cache(maxsize=1)
def load_agent_contract_lock() -> AgentContractLock:
    try:
        raw = resources.files("customs_backend").joinpath("agent_contract.lock.json").read_bytes()
        return AgentContractLock.model_validate(
            _strict_json_object(
                raw,
                label="Agent V3 consumer lock",
                maximum_bytes=32_768,
            )
        )
    except (OSError, ValueError) as exc:
        if isinstance(exc, ReleaseConfigurationError):
            raise
        raise ReleaseConfigurationError("Agent V3 consumer lock is missing or invalid") from exc


AgentContractKind = Literal["review_request", "review_result"]


@lru_cache(maxsize=2)
def load_agent_contract_schema(kind: AgentContractKind) -> dict[str, Any]:
    lock = load_agent_contract_lock()
    path: str
    if kind == "review_request":
        path = lock.review_request_v3_path
        expected_bytes = lock.review_request_v3_bytes
        expected_sha256 = lock.review_request_v3_sha256
    else:
        path = lock.review_result_v3_path
        expected_bytes = lock.review_result_v3_bytes
        expected_sha256 = lock.review_result_v3_sha256
    try:
        raw = resources.files("customs_backend").joinpath(*path.split("/")).read_bytes()
    except OSError as exc:
        raise ReleaseConfigurationError("Agent V3 provider Schema is missing") from exc
    if len(raw) != expected_bytes or hashlib.sha256(raw).hexdigest() != expected_sha256:
        raise ReleaseConfigurationError("Agent V3 provider Schema bytes differ from the lock")
    schema = _strict_json_object(
        raw,
        label=f"Agent V3 {kind} Schema",
        maximum_bytes=1_000_000,
    )
    try:
        Draft202012Validator.check_schema(schema)
    except SchemaError as exc:
        raise ReleaseConfigurationError("Agent V3 provider Schema is invalid") from exc
    return schema


def validate_agent_contract_bundle() -> AgentContractLock:
    lock = load_agent_contract_lock()
    load_agent_contract_schema("review_request")
    load_agent_contract_schema("review_result")
    return lock


def validate_agent_v3_payload(kind: AgentContractKind, payload: object) -> None:
    try:
        Draft202012Validator(
            load_agent_contract_schema(kind),
            format_checker=FormatChecker(),
        ).validate(payload)
    except ValidationError:
        raise AgentContractPayloadError(
            f"Agent V3 {kind} does not satisfy the packaged provider Schema"
        ) from None


class DomainReviewerKey(BaseModel):
    model_config = ConfigDict(extra="forbid")

    reviewer_id: str = Field(pattern=r"^[a-z0-9][a-z0-9._:-]{2,127}$")
    public_key_ed25519_base64: str = Field(min_length=44, max_length=44)
    valid_from: datetime
    valid_until: datetime | None = None
    status: Literal["active", "revoked"] = "active"

    @field_validator("public_key_ed25519_base64")
    @classmethod
    def valid_public_key(cls, value: str) -> str:
        _decode_base64(value, expected_length=32, label="reviewer public key")
        return value

    @field_validator("valid_from", "valid_until")
    @classmethod
    def timezone_aware(cls, value: datetime | None) -> datetime | None:
        if value is not None and value.tzinfo is None:
            raise ValueError("reviewer key timestamps must include a timezone")
        return value

    @model_validator(mode="after")
    def valid_window(self) -> "DomainReviewerKey":
        if self.valid_until is not None and self.valid_until <= self.valid_from:
            raise ValueError("reviewer key validity window is invalid")
        return self


class DomainReviewerTrustStore(BaseModel):
    """Immutable trust root packaged in the Backend release image.

    The initial store is intentionally empty.  Reviewer keys may only be added by
    a protected source PR and a new exact Backend image release; runtime env files
    cannot introduce a new trust root.
    """

    model_config = ConfigDict(extra="forbid")

    schema_version: Literal["backend-domain-reviewer-trust-store/v1"]
    reviewers: list[DomainReviewerKey] = Field(default_factory=list, max_length=100)
    trust_store_digest: str = Field(pattern=r"^[a-f0-9]{64}$")

    @model_validator(mode="after")
    def verify_store(self) -> "DomainReviewerTrustStore":
        reviewer_ids = [item.reviewer_id for item in self.reviewers]
        public_keys = [item.public_key_ed25519_base64 for item in self.reviewers]
        if reviewer_ids != sorted(reviewer_ids) or len(reviewer_ids) != len(set(reviewer_ids)):
            raise ValueError("trusted reviewer IDs must be unique and sorted")
        if len(public_keys) != len(set(public_keys)):
            raise ValueError("one Ed25519 key cannot represent multiple reviewers")
        payload = self.model_dump(mode="json")
        payload["trust_store_digest"] = "0" * 64
        if self.trust_store_digest != canonical_digest(payload):
            raise ValueError("domain reviewer trust-store digest is invalid")
        return self


@lru_cache(maxsize=1)
def load_domain_reviewer_trust_store() -> DomainReviewerTrustStore:
    try:
        raw = (
            resources.files("customs_backend")
            .joinpath("domain_reviewer_trust_store.json")
            .read_bytes()
        )
        return DomainReviewerTrustStore.model_validate(
            _strict_json_object(
                raw,
                label="domain reviewer trust store",
                maximum_bytes=262_144,
            )
        )
    except (OSError, ValueError) as exc:
        if isinstance(exc, ReleaseConfigurationError):
            raise
        raise ReleaseConfigurationError(
            "domain reviewer trust store is missing or invalid"
        ) from exc


class EvidenceProviderContractPin(BaseModel):
    """Exact Data provider code/contract identity advertised by the Agent."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    contract: Literal["evidence-runtime-release-provider-lock"] = (
        "evidence-runtime-release-provider-lock"
    )
    schema_version: Literal["1.0"] = "1.0"
    provider_release: Literal["evidence-api-v1.2.0"]
    provider_revision: str = Field(pattern=r"^[a-f0-9]{40}$")
    provider_build_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    provider_source_manifest_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    retrieval_schema_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    readiness_schema_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")

    @model_validator(mode="after")
    def build_digest_matches(self) -> "EvidenceProviderContractPin":
        build_payload = {
            "contract": "evidence-retrieval",
            "schema_version": self.schema_version,
            "provider_release": self.provider_release,
            "provider_revision": self.provider_revision,
            "schema_sha256": self.retrieval_schema_sha256,
            "source_manifest_sha256": self.provider_source_manifest_sha256,
        }
        if hashlib.sha256(_canonical_bytes(build_payload)).hexdigest() != (
            self.provider_build_sha256
        ):
            raise ValueError("Evidence provider build digest is invalid")
        return self


class EvidenceRuntimeReleaseIdentity(BaseModel):
    """Full Data V2 identity with an independently verified canonical digest."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    contract: Literal["evidence-runtime-release"]
    schema_version: Literal["2.0"]
    retrieval_schema_version: Literal["1.0"]
    provider_release: Literal["evidence-api-v1.2.0"]
    provider_revision: str = Field(pattern=r"^[a-f0-9]{40}$")
    provider_build_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    retrieval_schema_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    provider_source_manifest_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    knowledge_generation_id: str = Field(pattern=r"^[a-f0-9]{64}$")
    knowledge_generation_attestation_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    knowledge_source_manifest_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    model_manifest_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    index_config_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    evaluation_report_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    seal_attestation_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    knowledge_snapshot_id: str = Field(pattern=r"^[A-Za-z0-9._:-]{1,200}$")
    supported_review_as_of: date
    runtime_release_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")

    @field_validator("supported_review_as_of", mode="before")
    @classmethod
    def supported_date_is_exact_iso_date(cls, value: object) -> object:
        if isinstance(value, datetime):
            raise ValueError("supported_review_as_of must be an exact calendar date")
        if isinstance(value, date):
            return value
        if not isinstance(value, str):
            raise ValueError("supported_review_as_of must be an exact calendar date")
        try:
            parsed = date.fromisoformat(value)
        except ValueError:
            raise ValueError("supported_review_as_of must be an exact calendar date") from None
        if parsed.isoformat() != value:
            raise ValueError("supported_review_as_of must be an exact calendar date")
        return value

    @model_validator(mode="after")
    def canonical_digest_matches(self) -> "EvidenceRuntimeReleaseIdentity":
        value = self.model_dump(mode="json", exclude={"runtime_release_sha256"})
        if hashlib.sha256(_canonical_bytes(value)).hexdigest() != self.runtime_release_sha256:
            raise ValueError("Evidence runtime release digest is invalid")
        return self


class EvidenceRuntimeReleaseIdentityV3(BaseModel):
    """Full policy-bound Data V3 identity with a verified canonical digest."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    contract: Literal["evidence-runtime-release"]
    schema_version: Literal["3.0"]
    retrieval_schema_version: Literal["3.0"]
    base_runtime_release: EvidenceRuntimeReleaseIdentity
    retrieval_schema_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    readiness_schema_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    requirement_policy_schema_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    requirement_policy_release: str = Field(min_length=1, max_length=128)
    requirement_policy_manifest_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    coverage_matrix_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    requirement_policy_approval_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    reviewer_trust_store_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    runtime_release_v3_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")

    @model_validator(mode="after")
    def canonical_digest_matches(self) -> "EvidenceRuntimeReleaseIdentityV3":
        value = self.model_dump(mode="json", exclude={"runtime_release_v3_sha256"})
        if hashlib.sha256(_canonical_bytes(value)).hexdigest() != self.runtime_release_v3_sha256:
            raise ValueError("Evidence runtime v3 release digest is invalid")
        return self


class LocalUatAgentRuntimeBindingV1(BaseModel):
    """Exact, short-lived Agent identity selected by the local UAT harness."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    commit: str = Field(pattern=r"^[a-f0-9]{40}$")
    working_tree_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    wheel_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    code_snapshot_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    model_id: str = Field(min_length=1, max_length=128)
    prompt_bundle_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    schema_bundle_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    tool_registry_id: str = Field(min_length=1, max_length=128)
    tool_registry_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    meaning_catalog_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")


class LocalUatRuntimeBindingV1(BaseModel):
    """Fail-closed local-only binding that avoids rewriting release locks per edit.

    The file is generated by the private harness after it has inspected the live
    Agent metadata and the installed wheel.  It never weakens the packaged
    request/result/OpenAPI/meaning-catalog byte checks and is rejected outside a
    literal loopback local-UAT runtime.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal["customs-local-uat-runtime-binding/v1"]
    mode: Literal["development", "candidate"]
    issued_at_utc: datetime
    expires_at_utc: datetime
    loopback_origin: str = Field(min_length=1, max_length=512)
    agent: LocalUatAgentRuntimeBindingV1
    evidence_runtime_v3_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    capability_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    capability_file_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    mock_fallback_allowed: Literal[False]
    auto_completion_allowed: Literal[False]
    binding_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")

    @field_validator("issued_at_utc", "expires_at_utc")
    @classmethod
    def timestamp_is_aware(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("local UAT binding timestamps must include a timezone")
        return value.astimezone(UTC)

    @model_validator(mode="after")
    def binding_is_canonical_and_bounded(self) -> "LocalUatRuntimeBindingV1":
        from urllib.parse import urlsplit

        origin = urlsplit(self.loopback_origin)
        if (
            origin.scheme != "http"
            or origin.hostname not in {"127.0.0.1", "::1"}
            or origin.username is not None
            or origin.password is not None
            or origin.query
            or origin.fragment
            or origin.path not in {"", "/"}
        ):
            raise ValueError("local UAT binding requires a literal HTTP loopback origin")
        if (
            self.expires_at_utc <= self.issued_at_utc
            or self.expires_at_utc - self.issued_at_utc > timedelta(hours=24)
        ):
            raise ValueError("local UAT binding validity must be greater than 0 and at most 24h")
        payload = self.model_dump(mode="json", exclude={"binding_sha256"})
        if self.binding_sha256 != canonical_digest(payload):
            raise ValueError("local UAT binding digest is invalid")
        return self

    def require_active(self, *, now: datetime | None = None) -> None:
        checked_at = (now or datetime.now(UTC)).astimezone(UTC)
        if self.issued_at_utc > checked_at + timedelta(minutes=5):
            raise ReleaseConfigurationError("local UAT binding was issued in the future")
        if self.expires_at_utc <= checked_at:
            raise ReleaseConfigurationError("local UAT binding is expired")

    def matches_result_runtime(self, identity: ReleaseIdentityV3) -> bool:
        return (
            identity.agent_git_sha == self.agent.commit
            and identity.agent_source_snapshot_sha256 == self.agent.working_tree_sha256
            and identity.agent_code_snapshot_sha256 == self.agent.code_snapshot_sha256
            and identity.model_id == self.agent.model_id
            and identity.evidence_runtime_v3_sha256 == self.evidence_runtime_v3_sha256
            and identity.kill_switch_active
        )

    def matches_service_metadata_runtime(self, identity: ReleaseIdentityV3) -> bool:
        """Match the Agent build advertised before a Case-specific Evidence run.

        Base service metadata intentionally advertises the packaged text tool
        registry and no Case Evidence identity.  The UAT capability and the
        separate Evidence V3 identity bind those two dynamic boundaries.
        """

        return (
            identity.agent_git_sha == self.agent.commit
            and identity.agent_source_snapshot_sha256 == self.agent.working_tree_sha256
            and identity.agent_code_snapshot_sha256 == self.agent.code_snapshot_sha256
            and identity.model_id == self.agent.model_id
            and identity.kill_switch_active
        )


def load_local_uat_runtime_binding(
    path: str | Path,
    *,
    expected_file_sha256: str,
    now: datetime | None = None,
) -> LocalUatRuntimeBindingV1:
    """Load one exact binding file without trusting its path or formatting."""

    candidate = Path(path)
    if not candidate.is_absolute():
        raise ReleaseConfigurationError("local UAT binding path must be absolute")
    try:
        raw = candidate.read_bytes()
    except OSError as exc:
        raise ReleaseConfigurationError("local UAT binding file is unavailable") from exc
    if hashlib.sha256(raw).hexdigest() != expected_file_sha256:
        raise ReleaseConfigurationError("local UAT binding file SHA-256 differs")
    try:
        binding = LocalUatRuntimeBindingV1.model_validate(
            _strict_json_object(raw, label="local UAT runtime binding", maximum_bytes=65_536)
        )
    except (TypeError, ValueError) as exc:
        if isinstance(exc, ReleaseConfigurationError):
            raise
        raise ReleaseConfigurationError("local UAT runtime binding is invalid") from exc
    binding.require_active(now=now)
    return binding


class AgentServicePin(BaseModel):
    """Exact Agent service/code pin usable before an automatic-completion release.

    This pin deliberately excludes approved model/Data/profile values.  It lets a
    Production-candidate process prove which code, contracts and Data provider
    shell it is talking to while requiring all release-bearing metadata to remain
    honestly unreleased and fail closed.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    build_sha: str = Field(pattern=r"^[a-f0-9]{40}$|^[a-f0-9]{64}$")
    wheel_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    source_snapshot_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    package_version: str = Field(min_length=1, max_length=64)
    agent_code_snapshot_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    model_id: str = Field(min_length=1, max_length=128)
    extraction_prompt_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    extraction_schema_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    coordinator_prompt_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    narrative_prompt_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    chat_prompt_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    coordinator_schema_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    narrative_schema_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    chat_schema_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    policy_id: str = Field(min_length=1, max_length=128)
    tool_registry_id: str = Field(min_length=1, max_length=128)
    assertion_trust_store_release_state: Literal["unreleased", "active"]
    assertion_trust_store_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    evidence_provider_contract: EvidenceProviderContractPin

    def matches_common_runtime(self, identity: ReleaseIdentityV3) -> bool:
        lock = load_agent_contract_lock()
        return (
            identity.agent_package_version == self.package_version
            and identity.agent_git_sha == self.build_sha
            and identity.agent_source_snapshot_sha256 == self.source_snapshot_sha256
            and identity.agent_code_snapshot_sha256 == self.agent_code_snapshot_sha256
            and identity.model_id == self.model_id
            and identity.extraction_prompt_sha256 == self.extraction_prompt_sha256
            and identity.extraction_schema_sha256 == self.extraction_schema_sha256
            and identity.coordinator_prompt_sha256 == self.coordinator_prompt_sha256
            and identity.narrative_prompt_sha256 == self.narrative_prompt_sha256
            and identity.chat_prompt_sha256 == self.chat_prompt_sha256
            and identity.coordinator_schema_sha256 == self.coordinator_schema_sha256
            and identity.narrative_schema_sha256 == self.narrative_schema_sha256
            and identity.chat_schema_sha256 == self.chat_schema_sha256
            and identity.result_schema_sha256 == lock.review_result_v3_sha256
            and identity.policy_id == self.policy_id
            and identity.tool_registry_id == self.tool_registry_id
            and identity.assertion_trust_store_sha256 == self.assertion_trust_store_sha256
            and identity.assertion_trust_store_state == self.assertion_trust_store_release_state
        )

    def matches_unreleased_runtime(self, identity: ReleaseIdentityV3) -> bool:
        return self.matches_common_runtime(identity) and (
            identity.model_revision is None
            and identity.evidence_snapshot_id is None
            and identity.evidence_runtime_v3_sha256 is None
            and identity.release_profile_id == "unreleased"
            and identity.release_profile_state == "unreleased"
            and identity.release_profile_sha256 is None
            and identity.release_profile_subject_sha256 is None
            and identity.release_profile_evaluation_report_sha256 is None
            and identity.reviewer_trust_store_sha256 is None
            and identity.kill_switch_active
        )

    def validate_unreleased_metadata(self, value: object) -> dict[str, Any]:
        lock = load_agent_contract_lock()
        if not isinstance(value, dict):
            raise ReleaseConfigurationError("Agent metadata must be an object")
        try:
            if set(value) != {
                "agent",
                "agentic_limits",
                "automatic_completion_release",
                "capabilities",
                "evidence_provider_contract",
                "evidence_runtime_release",
                "evidence_runtime_release_v3",
                "model",
                "runtime_release",
                "schemas",
                "service_api_version",
                "transport_limits",
            }:
                raise ReleaseConfigurationError("Agent metadata shape is invalid")
            agent = value["agent"]
            model = value["model"]
            schemas = value["schemas"]
            if not isinstance(agent, dict) or set(agent) != {
                "build_sha",
                "package_version",
                "source_snapshot_sha256",
                "wheel_sha256",
            }:
                raise ReleaseConfigurationError("Agent metadata shape is invalid")
            if not isinstance(model, dict) or set(model) != {
                "id",
                "prompt_id",
                "prompt_sha256",
                "prompt_version",
            }:
                raise ReleaseConfigurationError("Agent metadata shape is invalid")
            if not isinstance(schemas, dict) or set(schemas) != {
                "review_request_v2_sha256",
                "review_request_v3_sha256",
                "review_result_v2_sha256",
                "review_result_v3_sha256",
                "service_openapi_sha256",
            }:
                raise ReleaseConfigurationError("Agent metadata shape is invalid")
            runtime_release = ReleaseIdentityV3.model_validate(value["runtime_release"])
            provider_contract = EvidenceProviderContractPin.model_validate(
                value["evidence_provider_contract"]
            )
            automatic = value["automatic_completion_release"]
            if not isinstance(automatic, dict) or set(automatic) != {
                "assertion_trust_store",
                "bounded_adaptive_active_enabled",
                "evaluation_report_sha256",
                "kill_switch_active",
                "model_revision",
                "profile_id",
                "profile_sha256",
                "release_state",
                "review_subject_sha256",
                "reviewer_trust_store",
                "production_admission",
            }:
                raise ReleaseConfigurationError("Agent metadata shape is invalid")
            trust = automatic["reviewer_trust_store"]
            if not isinstance(trust, dict) or set(trust) != {
                "release_state",
                "trust_store_sha256",
                "trusted_reviewer_count",
            }:
                raise ReleaseConfigurationError("Agent metadata shape is invalid")
            assertion_trust = automatic["assertion_trust_store"]
            production_admission = automatic["production_admission"]
            if not isinstance(assertion_trust, dict) or set(assertion_trust) != {
                "active_key_count",
                "release_state",
                "trust_domain",
                "trust_store_sha256",
            }:
                raise ReleaseConfigurationError("Agent metadata shape is invalid")
            if not isinstance(production_admission, dict) or set(production_admission) != {
                "admitted",
                "reason_code",
            }:
                raise ReleaseConfigurationError("Agent metadata shape is invalid")
            trust_sha = trust["trust_store_sha256"]
            if (
                not isinstance(trust_sha, str)
                or len(trust_sha) != 64
                or any(character not in "0123456789abcdef" for character in trust_sha)
            ):
                raise ReleaseConfigurationError("Agent metadata shape is invalid")
            if (
                value["service_api_version"] != lock.service_api_version
                or agent
                != {
                    "build_sha": self.build_sha,
                    "package_version": self.package_version,
                    "source_snapshot_sha256": self.source_snapshot_sha256,
                    "wheel_sha256": self.wheel_sha256,
                }
                or model["id"] != self.model_id
                or model["prompt_id"] != "customs_document_analysis"
                or model["prompt_sha256"] != self.extraction_prompt_sha256
                or model["prompt_version"] != "1.2.0"
                or schemas["review_request_v3_sha256"] != lock.review_request_v3_sha256
                or schemas["review_result_v3_sha256"] != lock.review_result_v3_sha256
                or schemas["service_openapi_sha256"] != lock.service_openapi_sha256
                or provider_contract != self.evidence_provider_contract
                or not self.matches_unreleased_runtime(runtime_release)
                or value["capabilities"]
                != [
                    "text",
                    "bounded_planning_shadow",
                    "bounded_active_executor_injectable",
                    "citation_verified_narrative",
                    "grounded_case_answer",
                    "immutable_revision",
                ]
                or value["agentic_limits"]
                != {
                    "max_cycles": 2,
                    "max_tool_calls": 6,
                    "ocr_enabled": False,
                    "planner_mode": "shadow",
                    "active_execution_authorized_by_release": False,
                    "active_tool_handlers_registered": True,
                    "trusted_assertion_verifier_configured": (
                        self.assertion_trust_store_release_state == "active"
                    ),
                    "effective_execution_mode": "shadow",
                }
                or value["transport_limits"]
                != {
                    "max_body_bytes": 2 * 1024 * 1024,
                    "max_documents": 6,
                    "max_text_characters": 500_000,
                    "text_only": True,
                    "empty_visual_placeholders_accepted": True,
                }
                or {
                    key: automatic[key]
                    for key in automatic
                    if key
                    not in {
                        "assertion_trust_store",
                        "reviewer_trust_store",
                        "production_admission",
                    }
                }
                != {
                    "profile_id": "unreleased",
                    "release_state": "unreleased",
                    "kill_switch_active": True,
                    "bounded_adaptive_active_enabled": False,
                    "profile_sha256": None,
                    "review_subject_sha256": None,
                    "model_revision": None,
                    "evaluation_report_sha256": None,
                }
                or trust["release_state"] != "unreleased"
                or trust["trusted_reviewer_count"] != 0
                or assertion_trust["release_state"] != self.assertion_trust_store_release_state
                or assertion_trust["trust_domain"]
                != "customs-ai-agent/supporting-document-verification/v1"
                or assertion_trust["trust_store_sha256"] != self.assertion_trust_store_sha256
                or not isinstance(assertion_trust["active_key_count"], int)
                or (
                    assertion_trust["active_key_count"] < 1
                    if self.assertion_trust_store_release_state == "active"
                    else assertion_trust["active_key_count"] != 0
                )
                or production_admission["admitted"] is not False
                or not isinstance(production_admission["reason_code"], str)
                or not production_admission["reason_code"]
                or value["evidence_runtime_release"] != {"release_state": "unreleased"}
                or value["evidence_runtime_release_v3"] != {"release_state": "unreleased"}
            ):
                raise ReleaseConfigurationError(
                    "Agent metadata differs from the exact candidate pin"
                )
        except (KeyError, TypeError, ValueError):
            raise ReleaseConfigurationError("Agent metadata shape is invalid") from None
        return value

    def validate_local_uat_metadata(self, value: object) -> dict[str, Any]:
        """Validate the exact candidate code pin without promoting diagnostic Data.

        The local UAT Agent adds an explicit diagnostic envelope and binds a
        non-release Evidence identity.  Strip only those known annotations, then
        reuse the strict candidate validator for every code, contract, prompt,
        model, policy, and tool-registry field.
        """

        if not isinstance(value, dict):
            raise ReleaseConfigurationError("Agent UAT metadata must be an object")
        normalized = dict(value)
        expected_annotations = {
            "status": "ready",
            "service_mode": "local_user_uat_diagnostic",
            "assurance_level": "diagnostic",
        }
        for key, expected in expected_annotations.items():
            if normalized.pop(key, None) != expected:
                raise ReleaseConfigurationError("Agent UAT metadata annotation is invalid")
        uat = normalized.pop("uat", None)
        if not isinstance(uat, dict):
            raise ReleaseConfigurationError("Agent UAT metadata annotation is invalid")
        evidence_v3 = normalized.get("evidence_runtime_release_v3")
        if (
            not isinstance(evidence_v3, dict)
            or set(evidence_v3) != {"release_state", "identity"}
            or evidence_v3.get("release_state") != "diagnostic"
            or not isinstance(evidence_v3.get("identity"), dict)
        ):
            raise ReleaseConfigurationError("Agent UAT Evidence identity is invalid")
        try:
            evidence_identity = EvidenceRuntimeReleaseIdentityV3.model_validate(
                evidence_v3["identity"]
            )
        except (TypeError, ValueError):
            raise ReleaseConfigurationError("Agent UAT Evidence identity is invalid") from None
        if uat.get("evidence_runtime_identity") != evidence_identity.model_dump(mode="json"):
            raise ReleaseConfigurationError("Agent UAT Evidence identity is not consistently bound")
        automatic = normalized.get("automatic_completion_release")
        if not isinstance(automatic, dict) or automatic.get("uat_forced_disabled") is not True:
            raise ReleaseConfigurationError("Agent UAT completion Gate is invalid")
        normalized["automatic_completion_release"] = {
            key: item for key, item in automatic.items() if key != "uat_forced_disabled"
        }
        normalized["evidence_runtime_release_v3"] = {"release_state": "unreleased"}
        self.validate_unreleased_metadata(normalized)
        return value

    def validate_local_uat_development_metadata(
        self,
        value: object,
        *,
        binding: LocalUatRuntimeBindingV1,
    ) -> dict[str, Any]:
        """Validate a live development build through the short-lived binding.

        Only code/model/prompt/schema/tool identities that legitimately change
        while iterating are selected from the runtime.  Provider contracts,
        policies, trust state, package version and all metadata invariants remain
        governed by the packaged Backend pin and validators.
        """

        if binding.mode != "development" or not isinstance(value, dict):
            raise ReleaseConfigurationError("development Agent binding is invalid")
        try:
            agent = value["agent"]
            runtime = ReleaseIdentityV3.model_validate(value["runtime_release"])
            evidence = value["evidence_runtime_release_v3"]
            evidence_identity = EvidenceRuntimeReleaseIdentityV3.model_validate(
                evidence["identity"]
            )
            if (
                not isinstance(agent, dict)
                or agent.get("build_sha") != binding.agent.commit
                or agent.get("source_snapshot_sha256") != binding.agent.working_tree_sha256
                or agent.get("wheel_sha256") != binding.agent.wheel_sha256
                or not binding.matches_service_metadata_runtime(runtime)
                or evidence_identity.runtime_release_v3_sha256 != binding.evidence_runtime_v3_sha256
            ):
                raise ReleaseConfigurationError(
                    "Agent metadata differs from the local UAT runtime binding"
                )
            dynamic_pin = self.model_copy(
                update={
                    "build_sha": binding.agent.commit,
                    "wheel_sha256": binding.agent.wheel_sha256,
                    "source_snapshot_sha256": binding.agent.working_tree_sha256,
                    "agent_code_snapshot_sha256": binding.agent.code_snapshot_sha256,
                    "model_id": binding.agent.model_id,
                    "extraction_prompt_sha256": runtime.extraction_prompt_sha256,
                    "extraction_schema_sha256": runtime.extraction_schema_sha256,
                    "coordinator_prompt_sha256": runtime.coordinator_prompt_sha256,
                    "narrative_prompt_sha256": runtime.narrative_prompt_sha256,
                    "chat_prompt_sha256": runtime.chat_prompt_sha256,
                    "coordinator_schema_sha256": runtime.coordinator_schema_sha256,
                    "narrative_schema_sha256": runtime.narrative_schema_sha256,
                    "chat_schema_sha256": runtime.chat_schema_sha256,
                    "tool_registry_id": runtime.tool_registry_id,
                }
            )
            dynamic_pin.validate_local_uat_metadata(value)
        except (KeyError, TypeError, ValueError):
            raise ReleaseConfigurationError("Agent development metadata is invalid") from None
        return value


class AgentReleasePin(BaseModel):
    model_config = ConfigDict(extra="forbid")

    build_sha: str = Field(pattern=r"^[a-f0-9]{40}$|^[a-f0-9]{64}$")
    wheel_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    source_snapshot_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    package_version: str = Field(min_length=1, max_length=64)
    agent_code_snapshot_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    model_id: str = Field(min_length=1, max_length=128)
    model_revision: str = Field(min_length=1, max_length=128)
    extraction_prompt_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    extraction_schema_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    coordinator_prompt_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    narrative_prompt_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    chat_prompt_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    coordinator_schema_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    narrative_schema_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    chat_schema_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    policy_id: str = Field(min_length=1, max_length=128)
    tool_registry_id: str = Field(min_length=1, max_length=128)
    assertion_trust_store_release_state: Literal["active"] = "active"
    assertion_trust_store_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    evidence_provider_contract: EvidenceProviderContractPin
    evidence_snapshot_id: str = Field(min_length=1, max_length=256)
    evidence_runtime_release_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    evidence_runtime_v3_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    release_profile_id: str = Field(min_length=1, max_length=256)
    release_profile_state: Literal["approved"] = "approved"
    release_profile_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    release_profile_subject_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    release_profile_evaluation_report_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    reviewer_trust_store_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    kill_switch_active: Literal[False] = False

    def service_pin(self) -> AgentServicePin:
        return AgentServicePin(
            build_sha=self.build_sha,
            wheel_sha256=self.wheel_sha256,
            source_snapshot_sha256=self.source_snapshot_sha256,
            package_version=self.package_version,
            agent_code_snapshot_sha256=self.agent_code_snapshot_sha256,
            model_id=self.model_id,
            extraction_prompt_sha256=self.extraction_prompt_sha256,
            extraction_schema_sha256=self.extraction_schema_sha256,
            coordinator_prompt_sha256=self.coordinator_prompt_sha256,
            narrative_prompt_sha256=self.narrative_prompt_sha256,
            chat_prompt_sha256=self.chat_prompt_sha256,
            coordinator_schema_sha256=self.coordinator_schema_sha256,
            narrative_schema_sha256=self.narrative_schema_sha256,
            chat_schema_sha256=self.chat_schema_sha256,
            policy_id=self.policy_id,
            tool_registry_id=self.tool_registry_id,
            assertion_trust_store_release_state=self.assertion_trust_store_release_state,
            assertion_trust_store_sha256=self.assertion_trust_store_sha256,
            evidence_provider_contract=self.evidence_provider_contract,
        )

    def runtime_identity(self) -> ReleaseIdentityV3:
        return ReleaseIdentityV3(
            agent_package_version=self.package_version,
            agent_git_sha=self.build_sha,
            agent_source_snapshot_sha256=self.source_snapshot_sha256,
            agent_code_snapshot_sha256=self.agent_code_snapshot_sha256,
            model_id=self.model_id,
            model_revision=self.model_revision,
            extraction_prompt_sha256=self.extraction_prompt_sha256,
            extraction_schema_sha256=self.extraction_schema_sha256,
            coordinator_prompt_sha256=self.coordinator_prompt_sha256,
            narrative_prompt_sha256=self.narrative_prompt_sha256,
            chat_prompt_sha256=self.chat_prompt_sha256,
            coordinator_schema_sha256=self.coordinator_schema_sha256,
            narrative_schema_sha256=self.narrative_schema_sha256,
            chat_schema_sha256=self.chat_schema_sha256,
            result_schema_sha256=load_agent_contract_lock().review_result_v3_sha256,
            policy_id=self.policy_id,
            tool_registry_id=self.tool_registry_id,
            assertion_trust_store_sha256=self.assertion_trust_store_sha256,
            assertion_trust_store_state=self.assertion_trust_store_release_state,
            evidence_snapshot_id=self.evidence_snapshot_id,
            evidence_runtime_v3_sha256=self.evidence_runtime_v3_sha256,
            release_profile_id=self.release_profile_id,
            release_profile_state=self.release_profile_state,
            release_profile_sha256=self.release_profile_sha256,
            release_profile_subject_sha256=self.release_profile_subject_sha256,
            release_profile_evaluation_report_sha256=(
                self.release_profile_evaluation_report_sha256
            ),
            reviewer_trust_store_sha256=self.reviewer_trust_store_sha256,
            kill_switch_active=self.kill_switch_active,
        )

    def matches_runtime(self, identity: ReleaseIdentityV3) -> bool:
        return identity == self.runtime_identity()

    def validate_metadata(self, value: object) -> dict[str, Any]:
        lock = load_agent_contract_lock()
        if not isinstance(value, dict):
            raise ReleaseConfigurationError("Agent metadata must be an object")
        try:
            if set(value) != {
                "agent",
                "agentic_limits",
                "automatic_completion_release",
                "capabilities",
                "evidence_provider_contract",
                "evidence_runtime_release",
                "evidence_runtime_release_v3",
                "model",
                "runtime_release",
                "schemas",
                "service_api_version",
                "transport_limits",
            }:
                raise ReleaseConfigurationError("Agent metadata shape is invalid")
            agent = value["agent"]
            if not isinstance(agent, dict) or set(agent) != {
                "build_sha",
                "package_version",
                "source_snapshot_sha256",
                "wheel_sha256",
            }:
                raise ReleaseConfigurationError("Agent metadata shape is invalid")
            model = value["model"]
            if not isinstance(model, dict) or set(model) != {
                "id",
                "prompt_id",
                "prompt_sha256",
                "prompt_version",
            }:
                raise ReleaseConfigurationError("Agent metadata shape is invalid")
            schemas = value["schemas"]
            if not isinstance(schemas, dict) or set(schemas) != {
                "review_request_v2_sha256",
                "review_request_v3_sha256",
                "review_result_v2_sha256",
                "review_result_v3_sha256",
                "service_openapi_sha256",
            }:
                raise ReleaseConfigurationError("Agent metadata shape is invalid")
            runtime_release = ReleaseIdentityV3.model_validate(value["runtime_release"])
            capabilities = value["capabilities"]
            limits = value["agentic_limits"]
            automatic = value["automatic_completion_release"]
            provider_contract = EvidenceProviderContractPin.model_validate(
                value["evidence_provider_contract"]
            )
            evidence = value["evidence_runtime_release"]
            evidence_v3 = value["evidence_runtime_release_v3"]
            if capabilities != [
                "text",
                "bounded_planning_shadow",
                "bounded_active_executor_injectable",
                "citation_verified_narrative",
                "grounded_case_answer",
                "immutable_revision",
            ]:
                raise ReleaseConfigurationError("Agent metadata differs from the exact release pin")
            if value["transport_limits"] != {
                "max_body_bytes": 2 * 1024 * 1024,
                "max_documents": 6,
                "max_text_characters": 500_000,
                "text_only": True,
                "empty_visual_placeholders_accepted": True,
            }:
                raise ReleaseConfigurationError("Agent metadata differs from the exact release pin")
            if not isinstance(limits, dict) or limits != {
                "max_cycles": 2,
                "max_tool_calls": 6,
                "ocr_enabled": False,
                "planner_mode": "active",
                "active_execution_authorized_by_release": True,
                "active_tool_handlers_registered": True,
                "trusted_assertion_verifier_configured": True,
                "effective_execution_mode": "active",
            }:
                raise ReleaseConfigurationError("Agent metadata differs from the exact release pin")
            if not isinstance(automatic, dict) or set(automatic) != {
                "assertion_trust_store",
                "bounded_adaptive_active_enabled",
                "evaluation_report_sha256",
                "kill_switch_active",
                "model_revision",
                "profile_id",
                "profile_sha256",
                "release_state",
                "review_subject_sha256",
                "reviewer_trust_store",
                "production_admission",
            }:
                raise ReleaseConfigurationError("Agent metadata shape is invalid")
            trust = automatic["reviewer_trust_store"]
            assertion_trust = automatic["assertion_trust_store"]
            production_admission = automatic["production_admission"]
            if not isinstance(trust, dict) or set(trust) != {
                "release_state",
                "trust_store_sha256",
                "trusted_reviewer_count",
            }:
                raise ReleaseConfigurationError("Agent metadata shape is invalid")
            if not isinstance(assertion_trust, dict) or set(assertion_trust) != {
                "active_key_count",
                "release_state",
                "trust_domain",
                "trust_store_sha256",
            }:
                raise ReleaseConfigurationError("Agent metadata shape is invalid")
            if not isinstance(production_admission, dict) or set(production_admission) != {
                "admitted",
                "reason_code",
            }:
                raise ReleaseConfigurationError("Agent metadata shape is invalid")
            if not isinstance(evidence, dict) or set(evidence) != {"release_state", "identity"}:
                raise ReleaseConfigurationError("Agent metadata shape is invalid")
            if not isinstance(evidence_v3, dict) or set(evidence_v3) != {
                "release_state",
                "identity",
            }:
                raise ReleaseConfigurationError("Agent metadata shape is invalid")
            if evidence["release_state"] != "released" or evidence_v3["release_state"] != (
                "released"
            ):
                raise ReleaseConfigurationError("Agent metadata differs from the exact release pin")
            evidence_identity = EvidenceRuntimeReleaseIdentity.model_validate(evidence["identity"])
            evidence_v3_identity = EvidenceRuntimeReleaseIdentityV3.model_validate(
                evidence_v3["identity"]
            )
            if (
                value["service_api_version"] != lock.service_api_version
                or agent["build_sha"] != self.build_sha
                or agent["wheel_sha256"] != self.wheel_sha256
                or agent["source_snapshot_sha256"] != self.source_snapshot_sha256
                or agent["package_version"] != self.package_version
                or schemas["review_request_v3_sha256"] != lock.review_request_v3_sha256
                or schemas["review_result_v3_sha256"] != lock.review_result_v3_sha256
                or schemas["service_openapi_sha256"] != lock.service_openapi_sha256
                or model["id"] != self.model_id
                or model["prompt_id"] != "customs_document_analysis"
                or model["prompt_sha256"] != self.extraction_prompt_sha256
                or model["prompt_version"] != "1.2.0"
                or runtime_release != self.runtime_identity()
                or provider_contract != self.evidence_provider_contract
                or evidence_identity.provider_release != provider_contract.provider_release
                or evidence_identity.provider_revision != provider_contract.provider_revision
                or evidence_identity.provider_build_sha256
                != provider_contract.provider_build_sha256
                or evidence_identity.provider_source_manifest_sha256
                != provider_contract.provider_source_manifest_sha256
                or evidence_identity.retrieval_schema_sha256
                != provider_contract.retrieval_schema_sha256
                or evidence_identity.runtime_release_sha256 != self.evidence_runtime_release_sha256
                or evidence_identity.knowledge_snapshot_id != self.evidence_snapshot_id
                or evidence_v3_identity.base_runtime_release != evidence_identity
                or evidence_v3_identity.runtime_release_v3_sha256 != self.evidence_runtime_v3_sha256
                or automatic["profile_id"] != self.release_profile_id
                or automatic["release_state"] != self.release_profile_state
                or automatic["kill_switch_active"] is not False
                or automatic["bounded_adaptive_active_enabled"] is not True
                or automatic["profile_sha256"] != self.release_profile_sha256
                or automatic["review_subject_sha256"] != self.release_profile_subject_sha256
                or automatic["model_revision"] != self.model_revision
                or automatic["evaluation_report_sha256"]
                != self.release_profile_evaluation_report_sha256
                or trust["release_state"] != "approved"
                or trust["trust_store_sha256"] != self.reviewer_trust_store_sha256
                or not isinstance(trust["trusted_reviewer_count"], int)
                or trust["trusted_reviewer_count"] < 2
                or assertion_trust["release_state"] != "active"
                or assertion_trust["trust_domain"]
                != "customs-ai-agent/supporting-document-verification/v1"
                or assertion_trust["trust_store_sha256"] != self.assertion_trust_store_sha256
                or not isinstance(assertion_trust["active_key_count"], int)
                or assertion_trust["active_key_count"] < 1
                or production_admission != {"admitted": True, "reason_code": None}
            ):
                raise ReleaseConfigurationError("Agent metadata differs from the exact release pin")
        except (KeyError, TypeError, ValueError):
            raise ReleaseConfigurationError("Agent metadata shape is invalid") from None
        return value


class DomainReviewerSignature(BaseModel):
    model_config = ConfigDict(extra="forbid")

    reviewer_id: str = Field(pattern=r"^[a-z0-9][a-z0-9._:-]{2,127}$")
    signature_ed25519_base64: str = Field(min_length=88, max_length=88)

    @field_validator("signature_ed25519_base64")
    @classmethod
    def valid_signature(cls, value: str) -> str:
        _decode_base64(value, expected_length=64, label="reviewer signature")
        return value


class AutoCompletionApprovalManifest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: Literal["backend-auto-completion-approval/v2"]
    profile_id: str = Field(min_length=1, max_length=256)
    backend_decision_policy_release: str = Field(min_length=1, max_length=128)
    adjudication_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    locked_holdout_report_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    holdout_case_count: int = Field(ge=500)
    incorrect_or_unsafe_auto_completion_count: Literal[0]
    eligible_clean_completion_recall: float = Field(ge=0.95, le=1.0)
    kill_switch_verification_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    release_pin: AgentReleasePin
    trust_store_digest: str = Field(pattern=r"^[a-f0-9]{64}$")
    issued_at: datetime
    expires_at: datetime
    approval_subject_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    reviewer_signatures: list[DomainReviewerSignature] = Field(min_length=2, max_length=20)
    approval_digest: str = Field(pattern=r"^[a-f0-9]{64}$")

    @field_validator("issued_at", "expires_at")
    @classmethod
    def timezone_aware(cls, value: datetime) -> datetime:
        if value.tzinfo is None:
            raise ValueError("approval timestamps must include a timezone")
        return value

    def subject_payload(self) -> dict[str, Any]:
        return {
            "schema_version": "backend-auto-completion-subject/v1",
            "profile_id": self.profile_id,
            "backend_decision_policy_release": self.backend_decision_policy_release,
            "adjudication_sha256": self.adjudication_sha256,
            "locked_holdout_report_sha256": self.locked_holdout_report_sha256,
            "holdout_case_count": self.holdout_case_count,
            "incorrect_or_unsafe_auto_completion_count": (
                self.incorrect_or_unsafe_auto_completion_count
            ),
            "eligible_clean_completion_recall": self.eligible_clean_completion_recall,
            "kill_switch_verification_sha256": self.kill_switch_verification_sha256,
            "release_pin": self.release_pin.model_dump(mode="json"),
            "agent_contract_lock": load_agent_contract_lock().model_dump(mode="json"),
            "trust_store_digest": self.trust_store_digest,
            "issued_at": _timestamp(self.issued_at),
            "expires_at": _timestamp(self.expires_at),
        }

    @model_validator(mode="after")
    def verify_manifest_shape(self) -> "AutoCompletionApprovalManifest":
        if self.expires_at <= self.issued_at:
            raise ValueError("auto-completion approval validity window is invalid")
        if self.expires_at - self.issued_at > timedelta(days=366):
            raise ValueError("auto-completion approval may not exceed 366 days")
        reviewer_ids = [item.reviewer_id for item in self.reviewer_signatures]
        if reviewer_ids != sorted(reviewer_ids) or len(reviewer_ids) != len(set(reviewer_ids)):
            raise ValueError("reviewer signatures must use unique, sorted reviewer IDs")
        if self.approval_subject_sha256 != canonical_digest(self.subject_payload()):
            raise ValueError("auto-completion approval subject has drifted")
        payload = self.model_dump(mode="json")
        payload["approval_digest"] = "0" * 64
        if self.approval_digest != canonical_digest(payload):
            raise ValueError("auto-completion approval digest is invalid")
        return self

    def verify_with_trust_store(
        self,
        trust_store: DomainReviewerTrustStore,
        *,
        now: datetime | None = None,
    ) -> "AutoCompletionApprovalManifest":
        checked_at = now or datetime.now(UTC)
        if checked_at.tzinfo is None:
            raise ValueError("approval verification time must include a timezone")
        if self.trust_store_digest != trust_store.trust_store_digest:
            raise ReleaseConfigurationError("approval trust-store subject has drifted")
        if self.issued_at > checked_at + timedelta(minutes=5) or checked_at >= self.expires_at:
            raise ReleaseConfigurationError("auto-completion approval is not currently valid")
        trusted = {item.reviewer_id: item for item in trust_store.reviewers}
        verified_keys: set[str] = set()
        subject = _canonical_bytes(self.subject_payload())
        for signature in self.reviewer_signatures:
            reviewer = trusted.get(signature.reviewer_id)
            if reviewer is None:
                raise ReleaseConfigurationError("approval contains an unknown domain reviewer")
            if reviewer.status != "active":
                raise ReleaseConfigurationError("approval contains a revoked domain reviewer")
            if reviewer.valid_from > self.issued_at or reviewer.valid_from > checked_at:
                raise ReleaseConfigurationError("reviewer key was not active for this approval")
            if reviewer.valid_until is not None and (
                reviewer.valid_until <= checked_at or self.expires_at > reviewer.valid_until
            ):
                raise ReleaseConfigurationError("reviewer key is expired or approval outlives it")
            key_bytes = _decode_base64(
                reviewer.public_key_ed25519_base64,
                expected_length=32,
                label="reviewer public key",
            )
            try:
                Ed25519PublicKey.from_public_bytes(key_bytes).verify(
                    _decode_base64(
                        signature.signature_ed25519_base64,
                        expected_length=64,
                        label="reviewer signature",
                    ),
                    subject,
                )
            except InvalidSignature as exc:
                raise ReleaseConfigurationError("domain reviewer signature is invalid") from exc
            verified_keys.add(reviewer.public_key_ed25519_base64)
        if len(verified_keys) < 2:
            raise ReleaseConfigurationError(
                "auto completion requires two independent trusted reviewer keys"
            )
        return self


def load_auto_completion_approval(
    path: str | None,
    *,
    expected_digest: str | None,
    expected_profile_id: str | None,
    expected_release_pin: AgentReleasePin | None,
    expected_backend_decision_policy_release: str,
) -> AutoCompletionApprovalManifest | None:
    if path is None:
        return None
    try:
        raw = Path(path).read_bytes()
        approval = AutoCompletionApprovalManifest.model_validate(
            _strict_json_object(
                raw,
                label="auto-completion approval manifest",
                maximum_bytes=262_144,
            )
        )
    except (OSError, ValueError) as exc:
        if isinstance(exc, ReleaseConfigurationError):
            raise
        raise ReleaseConfigurationError("auto-completion approval manifest is invalid") from exc
    if (
        approval.approval_digest != expected_digest
        or approval.profile_id != expected_profile_id
        or expected_release_pin is None
        or approval.release_pin != expected_release_pin
        or approval.backend_decision_policy_release != expected_backend_decision_policy_release
    ):
        raise ReleaseConfigurationError("auto-completion approval does not match configured pin")
    return approval.verify_with_trust_store(load_domain_reviewer_trust_store())


__all__ = [
    "AgentContractLock",
    "AgentReleasePin",
    "AgentServicePin",
    "AutoCompletionApprovalManifest",
    "DomainReviewerKey",
    "DomainReviewerSignature",
    "DomainReviewerTrustStore",
    "EvidenceProviderContractPin",
    "EvidenceRuntimeReleaseIdentity",
    "EvidenceRuntimeReleaseIdentityV3",
    "LocalUatAgentRuntimeBindingV1",
    "LocalUatRuntimeBindingV1",
    "ReleaseConfigurationError",
    "load_agent_contract_lock",
    "load_auto_completion_approval",
    "load_domain_reviewer_trust_store",
    "load_local_uat_runtime_binding",
]
