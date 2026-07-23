from __future__ import annotations

from functools import lru_cache
from typing import Literal

from pydantic import Field, SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict
from sqlalchemy.engine import make_url


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_prefix="CUSTOMS_BACKEND_",
        extra="ignore",
    )

    environment: Literal["development", "test", "local_uat", "production"] = "development"
    process_role: Literal["runtime", "migration", "outbox"] = "runtime"
    public_base_url: str = "http://127.0.0.1:8080"
    database_url: SecretStr | None = Field(default=None, repr=False)
    oidc_issuer: str | None = None
    oidc_audience: str | None = None
    oidc_jwks_url: str | None = None
    oidc_client_id: str | None = None
    oidc_authorization_endpoint: str | None = None
    oidc_token_endpoint: str | None = None
    oidc_end_session_endpoint: str | None = None
    oidc_org_claim: str = "org_id"
    oidc_roles_claim: str = "roles"
    agent_base_url: str = "http://127.0.0.1:8080"
    agent_service_token: SecretStr | None = Field(default=None, repr=False)
    agent_expected_build_sha: str | None = Field(
        default=None, pattern=r"^[a-f0-9]{40}$|^[a-f0-9]{64}$"
    )
    agent_expected_wheel_sha256: str | None = Field(default=None, pattern=r"^[a-f0-9]{64}$")
    agent_expected_source_snapshot_sha256: str | None = Field(
        default=None, pattern=r"^[a-f0-9]{64}$"
    )
    agent_expected_package_version: str | None = None
    agent_expected_code_snapshot_sha256: str | None = Field(default=None, pattern=r"^[a-f0-9]{64}$")
    agent_expected_model_id: str | None = None
    agent_expected_model_revision: str | None = None
    agent_expected_extraction_prompt_sha256: str | None = Field(
        default=None, pattern=r"^[a-f0-9]{64}$"
    )
    agent_expected_extraction_schema_sha256: str | None = Field(
        default=None, pattern=r"^[a-f0-9]{64}$"
    )
    agent_expected_coordinator_prompt_sha256: str | None = Field(
        default=None, pattern=r"^[a-f0-9]{64}$"
    )
    agent_expected_narrative_prompt_sha256: str | None = Field(
        default=None, pattern=r"^[a-f0-9]{64}$"
    )
    agent_expected_chat_prompt_sha256: str | None = Field(default=None, pattern=r"^[a-f0-9]{64}$")
    agent_expected_coordinator_schema_sha256: str | None = Field(
        default=None, pattern=r"^[a-f0-9]{64}$"
    )
    agent_expected_narrative_schema_sha256: str | None = Field(
        default=None, pattern=r"^[a-f0-9]{64}$"
    )
    agent_expected_chat_schema_sha256: str | None = Field(default=None, pattern=r"^[a-f0-9]{64}$")
    agent_expected_policy_id: str | None = None
    agent_expected_tool_registry_id: str | None = None
    agent_expected_assertion_trust_store_release_state: Literal["unreleased", "active"] | None = (
        None
    )
    agent_expected_assertion_trust_store_sha256: str | None = Field(
        default=None,
        pattern=r"^[a-f0-9]{64}$",
    )
    agent_expected_evidence_provider_release: Literal["evidence-api-v1.2.0"] | None = None
    agent_expected_evidence_provider_revision: str | None = Field(
        default=None, pattern=r"^[a-f0-9]{40}$"
    )
    agent_expected_evidence_provider_build_sha256: str | None = Field(
        default=None, pattern=r"^[a-f0-9]{64}$"
    )
    agent_expected_evidence_provider_source_manifest_sha256: str | None = Field(
        default=None, pattern=r"^[a-f0-9]{64}$"
    )
    agent_expected_evidence_provider_retrieval_schema_sha256: str | None = Field(
        default=None, pattern=r"^[a-f0-9]{64}$"
    )
    agent_expected_evidence_provider_readiness_schema_sha256: str | None = Field(
        default=None, pattern=r"^[a-f0-9]{64}$"
    )
    agent_expected_evidence_snapshot_id: str | None = None
    agent_expected_evidence_runtime_release_sha256: str | None = Field(
        default=None, pattern=r"^[a-f0-9]{64}$"
    )
    agent_expected_evidence_runtime_v3_sha256: str | None = Field(
        default=None, pattern=r"^[a-f0-9]{64}$"
    )
    agent_expected_release_profile_id: str | None = None
    agent_expected_release_profile_sha256: str | None = Field(
        default=None, pattern=r"^[a-f0-9]{64}$"
    )
    agent_expected_release_profile_subject_sha256: str | None = Field(
        default=None, pattern=r"^[a-f0-9]{64}$"
    )
    agent_expected_release_profile_evaluation_report_sha256: str | None = Field(
        default=None, pattern=r"^[a-f0-9]{64}$"
    )
    agent_expected_reviewer_trust_store_sha256: str | None = Field(
        default=None, pattern=r"^[a-f0-9]{64}$"
    )
    redis_url: SecretStr | None = Field(default=None, repr=False)
    minio_endpoint: str | None = None
    minio_access_key: SecretStr | None = Field(default=None, repr=False)
    minio_secret_key: SecretStr | None = Field(default=None, repr=False)
    minio_bucket: str = "customs-documents"
    minio_secure: bool = False
    auto_completion_enabled: bool = False
    auto_completion_profile_id: str | None = None
    auto_completion_approval_digest: str | None = Field(default=None, pattern=r"^[a-f0-9]{64}$")
    auto_completion_approval_manifest_path: str | None = None
    human_decision_policy_release: Literal["backend-human-decision-policy/v1"] = Field(
        default="backend-human-decision-policy/v1",
    )
    assertion_signing_key_registry_path: str | None = None
    requirement_policy_release_path: str | None = None
    requirement_policy_trust_store_path: str | None = None
    evidence_runtime_v3_release_path: str | None = None
    job_lease_seconds: int = Field(default=300, ge=60, le=1800)
    job_max_attempts: int = Field(default=3, ge=1, le=3)
    outbox_lease_seconds: int = Field(default=30, ge=5, le=300)
    uat_evidence_profile: Literal[
        "synthetic-v3",
        "preserved-3law-v3-diagnostic",
        "unknown",
    ] = "unknown"
    uat_evidence_data_source: Literal[
        "SYNTHETIC_V3_NO_RAG_QUALITY",
        "ACTUAL_POSTGRESQL_QDRANT_NEO4J_READ_ONLY",
        "UNKNOWN",
    ] = "UNKNOWN"
    uat_evidence_expected_production_law_count: int = Field(default=58, ge=1, le=10_000)
    uat_evidence_observed_law_count: int = Field(default=0, ge=0, le=10_000)
    uat_evidence_observed_article_count: int = Field(default=0, ge=0, le=10_000_000)
    uat_evidence_qdrant_point_count: int = Field(default=0, ge=0, le=10_000_000)
    uat_evidence_neo4j_article_count: int = Field(default=0, ge=0, le=10_000_000)
    uat_evidence_rag_quality_evaluated: Literal[False] = False
    uat_planner_mode: Literal["shadow", "diagnostic_active"] = "shadow"
    uat_claude_mode: Literal["actual"] = "actual"

    @field_validator("agent_base_url", "public_base_url")
    @classmethod
    def strip_url(cls, value: str) -> str:
        return value.rstrip("/")

    @field_validator("uat_evidence_rag_quality_evaluated", mode="before")
    @classmethod
    def parse_false_only_rag_quality_claim(cls, value: object) -> Literal[False]:
        # Environment variables arrive as strings. Accept only the explicit
        # spelling used by the harness and keep the all-environment false-only
        # type invariant; permissive boolean spellings such as 0/no/off could
        # otherwise conceal a configuration error.
        if value is False or (isinstance(value, str) and value.strip().casefold() == "false"):
            return False
        raise ValueError("uat_evidence_rag_quality_evaluated must be explicit false")

    @model_validator(mode="after")
    def production_is_fail_closed(self) -> "Settings":
        if self.environment == "local_uat":
            release_only = {
                "agent_expected_model_revision": self.agent_expected_model_revision,
                "agent_expected_evidence_snapshot_id": self.agent_expected_evidence_snapshot_id,
                "agent_expected_evidence_runtime_release_sha256": (
                    self.agent_expected_evidence_runtime_release_sha256
                ),
                "agent_expected_evidence_runtime_v3_sha256": (
                    self.agent_expected_evidence_runtime_v3_sha256
                ),
                "agent_expected_release_profile_id": self.agent_expected_release_profile_id,
                "agent_expected_release_profile_sha256": (
                    self.agent_expected_release_profile_sha256
                ),
                "agent_expected_release_profile_subject_sha256": (
                    self.agent_expected_release_profile_subject_sha256
                ),
                "agent_expected_release_profile_evaluation_report_sha256": (
                    self.agent_expected_release_profile_evaluation_report_sha256
                ),
                "agent_expected_reviewer_trust_store_sha256": (
                    self.agent_expected_reviewer_trust_store_sha256
                ),
                "assertion_signing_key_registry_path": self.assertion_signing_key_registry_path,
                "requirement_policy_release_path": self.requirement_policy_release_path,
                "requirement_policy_trust_store_path": self.requirement_policy_trust_store_path,
                "evidence_runtime_v3_release_path": self.evidence_runtime_v3_release_path,
                "auto_completion_profile_id": self.auto_completion_profile_id,
                "auto_completion_approval_digest": self.auto_completion_approval_digest,
                "auto_completion_approval_manifest_path": (
                    self.auto_completion_approval_manifest_path
                ),
            }
            configured_release_only = sorted(
                name for name, value in release_only.items() if value is not None
            )
            if configured_release_only:
                raise ValueError(
                    "local_uat rejects Production release-only configuration: "
                    + ", ".join(configured_release_only)
                )
            if self.auto_completion_enabled:
                raise ValueError("local_uat requires auto_completion_enabled=false")
            if self.agent_expected_assertion_trust_store_release_state not in (None, "unreleased"):
                raise ValueError("local_uat accepts only an unreleased Agent trust-store identity")
            if self.database_url is None:
                raise ValueError("local_uat configuration requires database_url")
            database_url = self.database_url.get_secret_value()
            if not database_url.startswith(("postgresql://", "postgresql+psycopg://")):
                raise ValueError("local_uat database_url must be PostgreSQL")
            if self.process_role == "migration":
                return self
            if self.process_role == "outbox":
                if self.redis_url is None:
                    raise ValueError("local_uat outbox configuration requires redis_url")
                return self
            observed_counts = (
                self.uat_evidence_observed_law_count,
                self.uat_evidence_observed_article_count,
                self.uat_evidence_qdrant_point_count,
                self.uat_evidence_neo4j_article_count,
            )
            if self.uat_evidence_expected_production_law_count != 58:
                raise ValueError("local_uat Evidence scope requires expected Production count 58")
            if self.uat_evidence_profile == "preserved-3law-v3-diagnostic":
                if (
                    self.uat_evidence_data_source != "ACTUAL_POSTGRESQL_QDRANT_NEO4J_READ_ONLY"
                    or self.uat_evidence_observed_law_count != 3
                    or self.uat_evidence_observed_article_count <= 0
                    or self.uat_evidence_qdrant_point_count
                    != self.uat_evidence_observed_article_count
                    or self.uat_evidence_neo4j_article_count
                    != self.uat_evidence_observed_article_count
                ):
                    raise ValueError(
                        "preserved 3-law UAT requires corroborated 3/58 "
                        "PostgreSQL/Qdrant/Neo4j counts"
                    )
            elif self.uat_evidence_profile == "synthetic-v3":
                if self.uat_evidence_data_source != "SYNTHETIC_V3_NO_RAG_QUALITY" or any(
                    observed_counts
                ):
                    raise ValueError("synthetic UAT requires zero actual-store observations")
            elif self.uat_evidence_data_source != "UNKNOWN" or any(observed_counts):
                raise ValueError("unknown UAT Evidence profile cannot assert Data observations")
            required = {
                "oidc_issuer": self.oidc_issuer,
                "oidc_audience": self.oidc_audience,
                "oidc_jwks_url": self.oidc_jwks_url,
                "oidc_client_id": self.oidc_client_id,
                "oidc_authorization_endpoint": self.oidc_authorization_endpoint,
                "oidc_token_endpoint": self.oidc_token_endpoint,
                "agent_service_token": self.agent_service_token,
                "redis_url": self.redis_url,
                "minio_endpoint": self.minio_endpoint,
                "minio_access_key": self.minio_access_key,
                "minio_secret_key": self.minio_secret_key,
            }
            missing = sorted(name for name, value in required.items() if value is None)
            if missing:
                raise ValueError("local_uat configuration is incomplete: " + ", ".join(missing))
            return self
        if self.environment != "production":
            return self
        if self.process_role == "migration":
            if self.database_url is None:
                raise ValueError("production migration configuration requires database_url")
            migration_url = self.database_url.get_secret_value()
            if not migration_url.startswith(("postgresql://", "postgresql+psycopg://")):
                raise ValueError("production database_url must be PostgreSQL")
            if make_url(migration_url).username != "customs_migrator":
                raise ValueError("production migration database_url must use customs_migrator")
            # Alembic is a one-shot database principal. It must not receive the
            # OIDC, Agent, MinIO, Redis, or automatic-completion credentials that
            # are mandatory for long-lived runtime processes.
            return self
        if self.process_role == "outbox":
            if self.database_url is None or self.redis_url is None:
                raise ValueError(
                    "production outbox configuration requires database_url and redis_url"
                )
            outbox_url = self.database_url.get_secret_value()
            if not outbox_url.startswith(("postgresql://", "postgresql+psycopg://")):
                raise ValueError("production database_url must be PostgreSQL")
            if make_url(outbox_url).username != "customs_runtime":
                raise ValueError("production outbox database_url must use customs_runtime")
            return self
        required = {
            "database_url": self.database_url,
            "oidc_issuer": self.oidc_issuer,
            "oidc_audience": self.oidc_audience,
            "oidc_jwks_url": self.oidc_jwks_url,
            "oidc_client_id": self.oidc_client_id,
            "oidc_authorization_endpoint": self.oidc_authorization_endpoint,
            "oidc_token_endpoint": self.oidc_token_endpoint,
            "agent_service_token": self.agent_service_token,
            "agent_expected_build_sha": self.agent_expected_build_sha,
            "agent_expected_wheel_sha256": self.agent_expected_wheel_sha256,
            "agent_expected_source_snapshot_sha256": (self.agent_expected_source_snapshot_sha256),
            "agent_expected_package_version": self.agent_expected_package_version,
            "agent_expected_code_snapshot_sha256": self.agent_expected_code_snapshot_sha256,
            "agent_expected_model_id": self.agent_expected_model_id,
            "agent_expected_extraction_prompt_sha256": (
                self.agent_expected_extraction_prompt_sha256
            ),
            "agent_expected_extraction_schema_sha256": (
                self.agent_expected_extraction_schema_sha256
            ),
            "agent_expected_coordinator_prompt_sha256": (
                self.agent_expected_coordinator_prompt_sha256
            ),
            "agent_expected_narrative_prompt_sha256": (self.agent_expected_narrative_prompt_sha256),
            "agent_expected_chat_prompt_sha256": self.agent_expected_chat_prompt_sha256,
            "agent_expected_coordinator_schema_sha256": (
                self.agent_expected_coordinator_schema_sha256
            ),
            "agent_expected_narrative_schema_sha256": (self.agent_expected_narrative_schema_sha256),
            "agent_expected_chat_schema_sha256": self.agent_expected_chat_schema_sha256,
            "agent_expected_policy_id": self.agent_expected_policy_id,
            "agent_expected_tool_registry_id": self.agent_expected_tool_registry_id,
            "agent_expected_assertion_trust_store_release_state": (
                self.agent_expected_assertion_trust_store_release_state
            ),
            "agent_expected_assertion_trust_store_sha256": (
                self.agent_expected_assertion_trust_store_sha256
            ),
            "agent_expected_evidence_provider_release": (
                self.agent_expected_evidence_provider_release
            ),
            "agent_expected_evidence_provider_revision": (
                self.agent_expected_evidence_provider_revision
            ),
            "agent_expected_evidence_provider_build_sha256": (
                self.agent_expected_evidence_provider_build_sha256
            ),
            "agent_expected_evidence_provider_source_manifest_sha256": (
                self.agent_expected_evidence_provider_source_manifest_sha256
            ),
            "agent_expected_evidence_provider_retrieval_schema_sha256": (
                self.agent_expected_evidence_provider_retrieval_schema_sha256
            ),
            "agent_expected_evidence_provider_readiness_schema_sha256": (
                self.agent_expected_evidence_provider_readiness_schema_sha256
            ),
            "redis_url": self.redis_url,
            "minio_endpoint": self.minio_endpoint,
            "minio_access_key": self.minio_access_key,
            "minio_secret_key": self.minio_secret_key,
        }
        missing = sorted(name for name, value in required.items() if value is None)
        if missing:
            raise ValueError(f"production configuration is incomplete: {', '.join(missing)}")
        release_values = {
            "agent_expected_model_revision": self.agent_expected_model_revision,
            "agent_expected_evidence_snapshot_id": self.agent_expected_evidence_snapshot_id,
            "agent_expected_evidence_runtime_release_sha256": (
                self.agent_expected_evidence_runtime_release_sha256
            ),
            "agent_expected_evidence_runtime_v3_sha256": (
                self.agent_expected_evidence_runtime_v3_sha256
            ),
            "agent_expected_release_profile_id": self.agent_expected_release_profile_id,
            "agent_expected_release_profile_sha256": self.agent_expected_release_profile_sha256,
            "agent_expected_release_profile_subject_sha256": (
                self.agent_expected_release_profile_subject_sha256
            ),
            "agent_expected_release_profile_evaluation_report_sha256": (
                self.agent_expected_release_profile_evaluation_report_sha256
            ),
            "agent_expected_reviewer_trust_store_sha256": (
                self.agent_expected_reviewer_trust_store_sha256
            ),
        }
        if any(value is not None for value in release_values.values()) and not all(
            value is not None for value in release_values.values()
        ):
            raise ValueError(
                "automatic-completion release configuration must be complete or absent"
            )
        policy_release_paths = {
            "requirement_policy_release_path": self.requirement_policy_release_path,
            "requirement_policy_trust_store_path": self.requirement_policy_trust_store_path,
            "evidence_runtime_v3_release_path": self.evidence_runtime_v3_release_path,
        }
        if any(value is not None for value in release_values.values()) and not all(
            value is not None for value in policy_release_paths.values()
        ):
            raise ValueError(
                "automatic-completion release configuration requires all signed Data policy "
                "artifacts"
            )
        database_url = self.database_url.get_secret_value() if self.database_url else ""
        if not database_url.startswith(("postgresql://", "postgresql+psycopg://")):
            raise ValueError("production database_url must be PostgreSQL")
        if make_url(database_url).username != "customs_runtime":
            raise ValueError("production runtime database_url must use customs_runtime")
        public_https_values = {
            "public_base_url": self.public_base_url,
            "oidc_issuer": self.oidc_issuer,
            "oidc_authorization_endpoint": self.oidc_authorization_endpoint,
            "oidc_token_endpoint": self.oidc_token_endpoint,
        }
        insecure = sorted(
            name
            for name, value in public_https_values.items()
            if value is None or not value.startswith("https://")
        )
        if insecure:
            raise ValueError(
                "production browser and OIDC endpoints must use HTTPS: " + ", ".join(insecure)
            )
        if self.auto_completion_enabled and (
            not all(value is not None for value in release_values.values())
            or not self.auto_completion_profile_id
            or not self.auto_completion_approval_digest
            or not self.auto_completion_approval_manifest_path
        ):
            raise ValueError(
                "auto completion requires an approved profile and two-reviewer approval digest"
            )
        return self


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()
