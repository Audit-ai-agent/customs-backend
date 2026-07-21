from __future__ import annotations

import base64
import hashlib
import json
from datetime import UTC, date, datetime, time, timedelta
from pathlib import Path
from typing import Any, Literal, Protocol, cast

from cryptography.hazmat.primitives import serialization
from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import (
    Ed25519PrivateKey,
    Ed25519PublicKey,
)
from pydantic import BaseModel, ConfigDict, Field, SecretStr, model_validator

from customs_backend.domain import (
    SupportingDocumentRecord,
    SupportingVerificationDecisionRecord,
    VerificationReceiptRecord,
    canonical_json_bytes,
)
from customs_backend.release import EvidenceRuntimeReleaseIdentityV3


ASSERTION_TRUST_DOMAIN = "customs-ai-agent/supporting-document-verification/v1"
_SIGNATURE_PREFIX = ASSERTION_TRUST_DOMAIN.encode("ascii") + b"\x00"
_MAX_REGISTRY_BYTES = 1024 * 1024
_POLICY_SIGNATURE_PREFIX = b"customs-evidence/requirement-policy-approval/v1\x00"


class AssertionSigningConfigurationError(RuntimeError):
    pass


class RequirementPolicyIdentity(BaseModel):
    """Identity resolved only from a release-verified server-side Data policy source."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    policy_source_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    policy_release: str = Field(min_length=1, max_length=128)
    evidence_runtime_v3_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")


class RequirementPolicyIdentityResolver(Protocol):
    @property
    def ready(self) -> bool: ...

    def resolve(self, document: SupportingDocumentRecord) -> RequirementPolicyIdentity | None: ...


class UnavailableRequirementPolicyIdentityResolver:
    @property
    def ready(self) -> bool:
        return False

    def resolve(self, document: SupportingDocumentRecord) -> None:
        return None


class _PolicyEntry(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    required_document_code: str = Field(min_length=1, max_length=128)
    verification_policy: Literal[
        "presence_only",
        "content_match_required",
        "external_verification_required",
        "human_only",
    ]
    policy_source_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")


class _ReviewerAttestation(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    reviewer_id: str = Field(min_length=1, max_length=128)
    key_id: str = Field(min_length=1, max_length=128)
    review_reference_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    signature_hex: str = Field(pattern=r"^[a-f0-9]{128}$")


class _PolicyApproval(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    reviewer_attestations: list[_ReviewerAttestation] = Field(min_length=2, max_length=10)
    adjudication_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    subject_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    trust_store_release: str = Field(min_length=1, max_length=128)
    reviewer_trust_store_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    approved_at: date
    valid_until: date

    @model_validator(mode="after")
    def independent_reviewers(self) -> "_PolicyApproval":
        dimensions = (
            [item.reviewer_id for item in self.reviewer_attestations],
            [item.key_id for item in self.reviewer_attestations],
            [item.review_reference_sha256 for item in self.reviewer_attestations],
            [item.signature_hex for item in self.reviewer_attestations],
        )
        if any(len(values) != len(set(values)) for values in dimensions):
            raise ValueError("policy approval reviewers must be independent")
        if self.valid_until < self.approved_at:
            raise ValueError("policy approval validity window is invalid")
        return self


class _PolicyRelease(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    contract: Literal["requirement-verification-policy"]
    schema_version: Literal["1.0"]
    release_state: Literal["approved"]
    policy_release: str = Field(min_length=1, max_length=128)
    knowledge_source_manifest_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    knowledge_snapshot_id: str = Field(min_length=1, max_length=200)
    coverage_matrix_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    policy_manifest_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    approval_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    approval: _PolicyApproval
    policies: list[_PolicyEntry] = Field(min_length=1, max_length=10_000)


class _TrustedReviewerKey(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    reviewer_id: str = Field(min_length=1, max_length=128)
    key_id: str = Field(min_length=1, max_length=128)
    role: Literal["customs_domain_reviewer"]
    algorithm: Literal["ed25519"]
    public_key_hex: str = Field(pattern=r"^[a-f0-9]{64}$")
    valid_from: date
    valid_until: date
    revoked: bool = False


class _TrustedReviewerStore(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    contract: Literal["evidence-domain-reviewer-trust-store"]
    schema_version: Literal["1.0"]
    trust_store_release: str = Field(min_length=1, max_length=128)
    valid_from: date
    valid_until: date
    keys: list[_TrustedReviewerKey] = Field(min_length=2, max_length=100)

    @model_validator(mode="after")
    def unique_keys(self) -> "_TrustedReviewerStore":
        if self.valid_until < self.valid_from:
            raise ValueError("reviewer trust-store validity window is invalid")
        key_ids = [item.key_id for item in self.keys]
        public_keys = [item.public_key_hex for item in self.keys]
        if len(key_ids) != len(set(key_ids)) or len(public_keys) != len(set(public_keys)):
            raise ValueError("reviewer trust-store keys must be unique")
        return self


def _canonical_sha256(value: Any) -> str:
    return hashlib.sha256(canonical_json_bytes(value)).hexdigest()


def _strict_json(path: str | Path, model: type[BaseModel]) -> BaseModel:
    unresolved = Path(path).expanduser()
    if unresolved.is_symlink():
        raise AssertionSigningConfigurationError("release artifact must be a regular file")
    resolved = unresolved.resolve(strict=True)
    if not resolved.is_file():
        raise AssertionSigningConfigurationError("release artifact must be a regular file")
    raw = resolved.read_bytes()
    if not raw or len(raw) > _MAX_REGISTRY_BYTES:
        raise AssertionSigningConfigurationError("release artifact size is invalid")

    def unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, value in pairs:
            if key in result:
                raise ValueError("release artifact contains a duplicate key")
            result[key] = value
        return result

    try:
        value = json.loads(
            raw.decode("utf-8"),
            object_pairs_hook=unique_object,
            parse_constant=lambda _value: (_ for _ in ()).throw(ValueError("non-finite JSON")),
        )
        return model.model_validate(value)
    except (UnicodeError, json.JSONDecodeError, ValueError) as exc:
        raise AssertionSigningConfigurationError("release artifact is invalid") from exc


def _approval_subject(release: _PolicyRelease) -> str:
    approval = release.approval
    claims = sorted(
        [
            {
                "reviewer_id": item.reviewer_id,
                "key_id": item.key_id,
                "review_reference_sha256": item.review_reference_sha256,
            }
            for item in approval.reviewer_attestations
        ],
        key=lambda item: (item["reviewer_id"], item["key_id"]),
    )
    return _canonical_sha256(
        {
            "domain": "customs-evidence/requirement-policy-approval/v1",
            "contract": release.contract,
            "schema_version": release.schema_version,
            "policy_release": release.policy_release,
            "knowledge_source_manifest_sha256": release.knowledge_source_manifest_sha256,
            "knowledge_snapshot_id": release.knowledge_snapshot_id,
            "coverage_matrix_sha256": release.coverage_matrix_sha256,
            "policy_manifest_sha256": release.policy_manifest_sha256,
            "adjudication_sha256": approval.adjudication_sha256,
            "reviewer_claims": claims,
            "trust_store_release": approval.trust_store_release,
            "reviewer_trust_store_sha256": approval.reviewer_trust_store_sha256,
            "approved_at": approval.approved_at.isoformat(),
            "valid_until": approval.valid_until.isoformat(),
        }
    )


class ReleasedRequirementPolicyIdentityResolver:
    """Resolve policy identity only after Data's two-reviewer release is verified."""

    def __init__(
        self,
        *,
        policy: _PolicyRelease,
        trust_store: _TrustedReviewerStore,
        runtime: EvidenceRuntimeReleaseIdentityV3,
        today: date | None = None,
    ) -> None:
        effective = today or date.today()
        if not (
            trust_store.valid_from <= effective <= trust_store.valid_until
            and policy.approval.approved_at <= effective <= policy.approval.valid_until
        ):
            raise AssertionSigningConfigurationError("policy approval is not currently valid")
        if len({item.required_document_code for item in policy.policies}) != len(policy.policies):
            raise AssertionSigningConfigurationError("policy document codes are not unique")
        policy_manifest = _canonical_sha256(
            [item.model_dump(mode="json") for item in policy.policies]
        )
        approval_sha = _canonical_sha256(policy.approval.model_dump(mode="json"))
        trust_sha = _canonical_sha256(trust_store.model_dump(mode="json"))
        verified_approval_sha = _canonical_sha256(
            {
                "domain": "customs-evidence/verified-policy-approval/v1",
                "policy_approval_sha256": policy.approval_sha256,
                "reviewer_trust_store_sha256": trust_sha,
            }
        )
        if (
            policy_manifest != policy.policy_manifest_sha256
            or approval_sha != policy.approval_sha256
            or _approval_subject(policy) != policy.approval.subject_sha256
            or policy.approval.reviewer_trust_store_sha256 != trust_sha
            or policy.approval.trust_store_release != trust_store.trust_store_release
            or runtime.requirement_policy_release != policy.policy_release
            or runtime.requirement_policy_manifest_sha256 != policy_manifest
            or runtime.coverage_matrix_sha256 != policy.coverage_matrix_sha256
            or runtime.requirement_policy_approval_sha256 != verified_approval_sha
            or runtime.reviewer_trust_store_sha256 != trust_sha
            or runtime.base_runtime_release.knowledge_snapshot_id != policy.knowledge_snapshot_id
            or runtime.base_runtime_release.knowledge_source_manifest_sha256
            != policy.knowledge_source_manifest_sha256
        ):
            raise AssertionSigningConfigurationError("policy is not bound to the Data runtime")
        keys = {item.key_id: item for item in trust_store.keys}
        reviewers: set[str] = set()
        public_keys: set[str] = set()
        for attestation in policy.approval.reviewer_attestations:
            key = keys.get(attestation.key_id)
            if (
                key is None
                or key.reviewer_id != attestation.reviewer_id
                or key.revoked
                or not key.valid_from <= policy.approval.approved_at <= key.valid_until
                or not key.valid_from <= effective <= key.valid_until
            ):
                raise AssertionSigningConfigurationError("policy reviewer key is invalid")
            try:
                Ed25519PublicKey.from_public_bytes(bytes.fromhex(key.public_key_hex)).verify(
                    bytes.fromhex(attestation.signature_hex),
                    _POLICY_SIGNATURE_PREFIX + bytes.fromhex(policy.approval.subject_sha256),
                )
            except (InvalidSignature, ValueError) as exc:
                raise AssertionSigningConfigurationError("policy signature is invalid") from exc
            reviewers.add(key.reviewer_id)
            public_keys.add(key.public_key_hex)
        if len(reviewers) < 2 or len(public_keys) < 2:
            raise AssertionSigningConfigurationError("policy requires two independent reviewers")
        self._runtime_sha256 = runtime.runtime_release_v3_sha256
        self._policy_release = policy.policy_release
        self._entries = {item.required_document_code: item for item in policy.policies}

    @property
    def ready(self) -> bool:
        return True

    def resolve(self, document: SupportingDocumentRecord) -> RequirementPolicyIdentity | None:
        entry = self._entries.get(document.document_code)
        if entry is None:
            return None
        return RequirementPolicyIdentity(
            policy_source_sha256=entry.policy_source_sha256,
            policy_release=self._policy_release,
            evidence_runtime_v3_sha256=self._runtime_sha256,
        )


def load_requirement_policy_resolver(
    *,
    policy_path: str | Path | None,
    trust_store_path: str | Path | None,
    runtime_v3_path: str | Path | None,
    expected_runtime_v3_sha256: str | None,
) -> RequirementPolicyIdentityResolver:
    if not all((policy_path, trust_store_path, runtime_v3_path, expected_runtime_v3_sha256)):
        return UnavailableRequirementPolicyIdentityResolver()
    policy = _strict_json(cast(str | Path, policy_path), _PolicyRelease)
    trust = _strict_json(cast(str | Path, trust_store_path), _TrustedReviewerStore)
    runtime = _strict_json(cast(str | Path, runtime_v3_path), EvidenceRuntimeReleaseIdentityV3)
    if (
        not isinstance(policy, _PolicyRelease)
        or not isinstance(trust, _TrustedReviewerStore)
        or not isinstance(runtime, EvidenceRuntimeReleaseIdentityV3)
    ):
        raise AssertionSigningConfigurationError("release artifact type mismatch")
    if runtime.runtime_release_v3_sha256 != expected_runtime_v3_sha256:
        raise AssertionSigningConfigurationError("Data runtime differs from the Agent release pin")
    return ReleasedRequirementPolicyIdentityResolver(
        policy=policy,
        trust_store=trust,
        runtime=runtime,
    )


def _decode_base64url(value: str, *, expected_bytes: int) -> bytes:
    try:
        decoded = base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))
    except (TypeError, ValueError):
        raise ValueError("assertion signing key encoding is invalid") from None
    if (
        len(decoded) != expected_bytes
        or base64.urlsafe_b64encode(decoded).decode("ascii").rstrip("=") != value
    ):
        raise ValueError("assertion signing key encoding is invalid")
    return decoded


def _encode_base64url(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).decode("ascii").rstrip("=")


class AssertionSigningKeyV1(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    authority_id: str = Field(
        min_length=1,
        max_length=128,
        pattern=r"^backend:principal-sha256:[a-f0-9]{64}$",
    )
    authority_release: Literal["backend-human-decision-policy/v1"]
    key_id: str = Field(
        min_length=1,
        max_length=128,
        pattern=r"^[A-Za-z0-9][A-Za-z0-9._:-]*$",
    )
    private_key_base64url: SecretStr = Field(repr=False)
    public_key_base64url: str = Field(pattern=r"^[A-Za-z0-9_-]{43}$")
    valid_from: date
    valid_until: date
    receipt_ttl_seconds: int = Field(default=86_400, ge=60, le=2_592_000)

    @model_validator(mode="after")
    def key_material_and_window_are_valid(self) -> "AssertionSigningKeyV1":
        if self.valid_until < self.valid_from:
            raise ValueError("assertion signing key validity window is invalid")
        private = Ed25519PrivateKey.from_private_bytes(
            _decode_base64url(
                self.private_key_base64url.get_secret_value(),
                expected_bytes=32,
            )
        )
        public = private.public_key().public_bytes(
            encoding=serialization.Encoding.Raw,
            format=serialization.PublicFormat.Raw,
        )
        if _encode_base64url(public) != self.public_key_base64url:
            raise ValueError("assertion signing private/public key pair does not match")
        return self


class AssertionSigningKeyRegistryV1(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal["customs-backend-assertion-signing-keys/v1"]
    trust_domain: Literal["customs-ai-agent/supporting-document-verification/v1"]
    agent_trust_store_release_state: Literal["active"]
    agent_trust_store_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    keys: list[AssertionSigningKeyV1] = Field(min_length=1, max_length=100)

    @model_validator(mode="after")
    def unique_key_identities_and_material(self) -> "AssertionSigningKeyRegistryV1":
        identities = [(item.authority_id, item.key_id) for item in self.keys]
        if len(identities) != len(set(identities)):
            raise ValueError("assertion signing key identities must be unique")
        public_keys = [item.public_key_base64url for item in self.keys]
        if len(public_keys) != len(set(public_keys)):
            raise ValueError("one assertion key cannot represent multiple authorities")
        return self


class SupportingAssertionSigner(Protocol):
    @property
    def ready(self) -> bool: ...

    @property
    def trust_store_sha256(self) -> str | None: ...

    def sign_human_decision(
        self,
        *,
        case_id: str,
        supporting_document: SupportingDocumentRecord,
        decision_record: SupportingVerificationDecisionRecord,
        authority_id: str,
        authority_release: Literal["backend-human-decision-policy/v1"],
        signed_at: datetime,
    ) -> VerificationReceiptRecord | None: ...


class UnavailableSupportingAssertionSigner:
    """Safe default: preserve the human assertion but never claim cryptographic trust."""

    @property
    def ready(self) -> bool:
        return False

    @property
    def trust_store_sha256(self) -> str | None:
        return None

    def sign_human_decision(
        self,
        *,
        case_id: str,
        supporting_document: SupportingDocumentRecord,
        decision_record: SupportingVerificationDecisionRecord,
        authority_id: str,
        authority_release: Literal["backend-human-decision-policy/v1"],
        signed_at: datetime,
    ) -> None:
        return None


class RegistrySupportingAssertionSigner:
    def __init__(self, registry: AssertionSigningKeyRegistryV1) -> None:
        self._registry = AssertionSigningKeyRegistryV1.model_validate(
            registry.model_dump(mode="python")
        )
        self._keys = {
            (item.authority_id, item.authority_release): item for item in self._registry.keys
        }

    @property
    def ready(self) -> bool:
        return True

    @property
    def trust_store_sha256(self) -> str:
        return self._registry.agent_trust_store_sha256

    def sign_human_decision(
        self,
        *,
        case_id: str,
        supporting_document: SupportingDocumentRecord,
        decision_record: SupportingVerificationDecisionRecord,
        authority_id: str,
        authority_release: Literal["backend-human-decision-policy/v1"],
        signed_at: datetime,
    ) -> VerificationReceiptRecord | None:
        key = self._keys.get((authority_id, authority_release))
        if key is None or not key.valid_from <= signed_at.date() <= key.valid_until:
            return None
        maximum_expiry = datetime.combine(key.valid_until, time.max, tzinfo=UTC)
        expires_at = min(
            signed_at.astimezone(UTC) + timedelta(seconds=key.receipt_ttl_seconds),
            maximum_expiry,
        )
        if expires_at <= signed_at:
            return None
        unsigned_values: dict[str, Any] = {
            "trust_domain": ASSERTION_TRUST_DOMAIN,
            "case_id": case_id,
            "method": "human_approver",
            "authority_id": authority_id,
            "authority_release": authority_release,
            "key_id": key.key_id,
            "supporting_document_claims_sha256": supporting_document.claims_sha256,
            "policy_source_sha256": decision_record.policy_source_sha256,
            "policy_release": decision_record.policy_release,
            "decision_record_sha256": decision_record.sha256,
            "connector_result_sha256": None,
            "signed_at": signed_at,
            "expires_at": expires_at,
        }
        unsigned = VerificationReceiptRecord(
            **unsigned_values,
            signature_base64url="A" * 86,
        )
        message = _SIGNATURE_PREFIX + canonical_json_bytes(unsigned.unsigned_payload())
        private = Ed25519PrivateKey.from_private_bytes(
            _decode_base64url(
                key.private_key_base64url.get_secret_value(),
                expected_bytes=32,
            )
        )
        signature = private.sign(message)
        return VerificationReceiptRecord(
            **unsigned_values,
            signature_base64url=_encode_base64url(signature),
        )


def load_assertion_signer(
    path: str | Path | None,
    *,
    expected_trust_store_sha256: str,
) -> SupportingAssertionSigner:
    if path is None:
        return UnavailableSupportingAssertionSigner()
    resolved = Path(path).expanduser().resolve(strict=True)
    raw = resolved.read_bytes()
    if not raw or len(raw) > _MAX_REGISTRY_BYTES:
        raise AssertionSigningConfigurationError("assertion signing registry size is invalid")

    def unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, value in pairs:
            if key in result:
                raise ValueError("assertion signing registry contains a duplicate key")
            result[key] = value
        return result

    try:
        payload = json.loads(
            raw.decode("utf-8"),
            object_pairs_hook=unique_object,
            parse_constant=lambda _value: (_ for _ in ()).throw(
                ValueError("assertion signing registry contains a non-finite number")
            ),
        )
        registry = AssertionSigningKeyRegistryV1.model_validate(payload)
    except (UnicodeError, json.JSONDecodeError, ValueError) as exc:
        raise AssertionSigningConfigurationError("assertion signing registry is invalid") from exc
    if registry.agent_trust_store_sha256 != expected_trust_store_sha256:
        raise AssertionSigningConfigurationError(
            "assertion signing registry is not bound to the pinned Agent trust store"
        )
    return RegistrySupportingAssertionSigner(registry)


__all__ = [
    "ASSERTION_TRUST_DOMAIN",
    "AssertionSigningConfigurationError",
    "AssertionSigningKeyRegistryV1",
    "RegistrySupportingAssertionSigner",
    "ReleasedRequirementPolicyIdentityResolver",
    "RequirementPolicyIdentity",
    "RequirementPolicyIdentityResolver",
    "SupportingAssertionSigner",
    "UnavailableRequirementPolicyIdentityResolver",
    "UnavailableSupportingAssertionSigner",
    "load_assertion_signer",
    "load_requirement_policy_resolver",
]
