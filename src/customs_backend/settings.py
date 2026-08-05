"""Settings for the two supported local Backend modes."""

from __future__ import annotations

from datetime import date
from functools import lru_cache
from typing import Literal
from urllib.parse import urlsplit

from pydantic import Field, SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


_FUNCTIONAL_UAT_AS_OF = date(2026, 8, 5)
SupportedMode = Literal["local-development", "functional-uat"]


class Settings(BaseSettings):
    # Local profiles are imported explicitly. A repository `.env` must never
    # silently override or mix with a generated `_local` profile.
    model_config = SettingsConfigDict(env_file=None, env_prefix="CUSTOMS_BACKEND_", extra="ignore")

    # `test` exists solely for explicit unit-test containers and OpenAPI export.
    # A launched process can start only one of the two local modes.
    environment: Literal["local-development", "functional-uat", "test"] = "local-development"
    process_role: Literal["runtime"] = "runtime"
    public_base_url: str = "http://127.0.0.1:8080"
    agent_base_url: str = "http://127.0.0.1:8090"
    agent_service_token: SecretStr | None = Field(default=None, repr=False)
    agent_timeout_seconds: int = Field(default=180, ge=1, le=900)

    dev_session_token: SecretStr | None = Field(default=None, repr=False)
    dev_session_subject: str = "local-developer"
    dev_session_org_id: str = "local-dev"
    dev_session_ttl_seconds: int = Field(default=28_800, ge=60, le=86_400)

    functional_uat_as_of: date = _FUNCTIONAL_UAT_AS_OF
    database_url: SecretStr | None = Field(default=None, repr=False)
    minio_endpoint: str | None = None
    minio_access_key: SecretStr | None = Field(default=None, repr=False)
    minio_secret_key: SecretStr | None = Field(default=None, repr=False)
    minio_bucket: str = "customs-documents"
    minio_secure: bool = False
    oidc_issuer: str | None = None
    oidc_audience: str | None = None
    oidc_jwks_url: str | None = None
    oidc_client_id: str | None = None
    oidc_authorization_endpoint: str | None = None
    oidc_token_endpoint: str | None = None
    oidc_end_session_endpoint: str | None = None
    oidc_org_claim: str = "org_id"
    oidc_roles_claim: str = "roles"

    @field_validator("agent_base_url", "public_base_url")
    @classmethod
    def strip_url(cls, value: str) -> str:
        return value.rstrip("/")

    @property
    def service_mode(self) -> SupportedMode | None:
        if self.environment == "local-development":
            return "local-development"
        if self.environment == "functional-uat":
            return "functional-uat"
        return None

    @staticmethod
    def _literal_loopback_url(value: str, *, name: str) -> None:
        parsed = urlsplit(value)
        if (
            parsed.scheme != "http"
            or parsed.hostname not in {"127.0.0.1", "::1"}
            or parsed.username is not None
            or parsed.password is not None
            or parsed.query
            or parsed.fragment
        ):
            raise ValueError(f"{name} must use a literal HTTP loopback endpoint")

    @model_validator(mode="after")
    def validate_supported_profiles(self) -> "Settings":
        if self.environment == "test":
            return self
        if self.process_role != "runtime":
            raise ValueError("local Backend profiles support only the runtime role")
        self._literal_loopback_url(self.public_base_url, name="public_base_url")
        self._literal_loopback_url(self.agent_base_url, name="agent_base_url")
        if self.agent_service_token is None:
            raise ValueError("local Backend profiles require agent_service_token")
        if self.environment == "local-development":
            if self.dev_session_token is None:
                raise ValueError("local-development requires dev_session_token")
            unused = {
                "database_url": self.database_url,
                "minio_endpoint": self.minio_endpoint,
                "minio_access_key": self.minio_access_key,
                "minio_secret_key": self.minio_secret_key,
                "oidc_issuer": self.oidc_issuer,
                "oidc_audience": self.oidc_audience,
                "oidc_jwks_url": self.oidc_jwks_url,
                "oidc_client_id": self.oidc_client_id,
                "oidc_authorization_endpoint": self.oidc_authorization_endpoint,
                "oidc_token_endpoint": self.oidc_token_endpoint,
                "oidc_end_session_endpoint": self.oidc_end_session_endpoint,
            }
            configured = sorted(name for name, value in unused.items() if value is not None)
            if configured:
                raise ValueError(
                    "local-development uses in-memory state and no OIDC/store configuration: "
                    + ", ".join(configured)
                )
            return self

        if self.functional_uat_as_of != _FUNCTIONAL_UAT_AS_OF:
            raise ValueError("functional-uat is fixed to 2026-08-05")
        required = {
            "database_url": self.database_url,
            "minio_endpoint": self.minio_endpoint,
            "minio_access_key": self.minio_access_key,
            "minio_secret_key": self.minio_secret_key,
            "oidc_issuer": self.oidc_issuer,
            "oidc_audience": self.oidc_audience,
            "oidc_jwks_url": self.oidc_jwks_url,
            "oidc_client_id": self.oidc_client_id,
            "oidc_authorization_endpoint": self.oidc_authorization_endpoint,
            "oidc_token_endpoint": self.oidc_token_endpoint,
        }
        missing = sorted(name for name, value in required.items() if value is None)
        if missing:
            raise ValueError("functional-uat configuration is incomplete: " + ", ".join(missing))
        database_url = self.database_url.get_secret_value() if self.database_url else ""
        parsed_database = urlsplit(database_url)
        if (
            parsed_database.scheme != "postgresql"
            or parsed_database.hostname not in {"127.0.0.1", "::1"}
        ):
            raise ValueError("functional-uat database_url must use PostgreSQL on literal loopback")
        for name in (
            "oidc_issuer",
            "oidc_jwks_url",
            "oidc_authorization_endpoint",
            "oidc_token_endpoint",
        ):
            value = getattr(self, name)
            assert isinstance(value, str)
            self._literal_loopback_url(value, name=name)
        if self.oidc_end_session_endpoint is not None:
            self._literal_loopback_url(self.oidc_end_session_endpoint, name="oidc_end_session_endpoint")
        minio = urlsplit("http://" + (self.minio_endpoint or ""))
        if (
            minio.hostname not in {"127.0.0.1", "::1"}
            or minio.username is not None
            or minio.password is not None
            or minio.query
            or minio.fragment
            or minio.path not in {"", "/"}
        ):
            raise ValueError("functional-uat minio_endpoint must use literal loopback")
        return self


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()
