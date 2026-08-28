"""Create one ignored, local Backend profile without printing secret values."""

from __future__ import annotations

import argparse
import json
import os
import secrets
from pathlib import Path

from customs_backend.functional_uat_branding import FUNCTIONAL_UAT_REALM_BRANDING


def _repository_root() -> Path:
    """Find the Backend checkout without relying on an editable-install path."""

    candidates = (Path.cwd(), Path(__file__).resolve().parents[2])
    for candidate in candidates:
        if (candidate / "pyproject.toml").is_file() and candidate.name == "customs-backend":
            return candidate.resolve()
    raise RuntimeError("run customs-backend-profile-init from the customs-backend checkout")


def _workspace_root() -> Path:
    return _repository_root().parent


def _target_path(profile: str) -> Path:
    return _workspace_root() / "_local" / "profiles" / profile / "backend.env"


def _random_secret() -> str:
    return secrets.token_urlsafe(32)


def _restrict_to_owner(path: Path) -> None:
    """Best-effort owner-only mode before an ignored local file is installed.

    POSIX honours ``0o600``. Windows ACLs are inherited from the user's local
    workspace; this call does not broaden those ACLs and intentionally avoids
    shelling out to manipulate machine-wide security policy.
    """

    try:
        os.chmod(path, 0o600)
    except OSError:
        pass


def _dev_fixture() -> list[tuple[str, str]]:
    agent_service_token = _random_secret()
    return [
        ("CUSTOMS_BACKEND_ENVIRONMENT", "local-development"),
        ("CUSTOMS_BACKEND_PUBLIC_BASE_URL", "http://127.0.0.1:18081"),
        ("CUSTOMS_BACKEND_AGENT_BASE_URL", "http://127.0.0.1:18090"),
        ("CUSTOMS_BACKEND_AGENT_SERVICE_TOKEN", agent_service_token),
        ("CUSTOMS_AGENT_SERVICE_BEARER_TOKEN", agent_service_token),
        ("CUSTOMS_AGENT_SERVICE_MODE", "local-development"),
        ("CUSTOMS_BACKEND_DEV_REVIEWER_SESSION_TOKEN", _random_secret()),
        ("CUSTOMS_BACKEND_DEV_APPROVER_SESSION_TOKEN", _random_secret()),
    ]


def _functional_uat(*, realm_path: Path) -> tuple[list[tuple[str, str]], dict[str, object]]:
    database_password = _random_secret()
    minio_user = "customs-functional-" + secrets.token_hex(6)
    minio_password = _random_secret()
    keycloak_admin_password = _random_secret()
    reviewer_username = "local-functional-reviewer"
    reviewer_password = _random_secret()
    approver_username = "local-functional-approver"
    approver_password = _random_secret()
    agent_service_token = _random_secret()
    values = [
        ("CUSTOMS_BACKEND_ENVIRONMENT", "functional-uat"),
        ("CUSTOMS_BACKEND_PUBLIC_BASE_URL", "http://127.0.0.1:18081"),
        ("CUSTOMS_BACKEND_AGENT_BASE_URL", "http://127.0.0.1:18090"),
        ("CUSTOMS_BACKEND_AGENT_SERVICE_TOKEN", agent_service_token),
        ("CUSTOMS_AGENT_SERVICE_BEARER_TOKEN", agent_service_token),
        ("CUSTOMS_AGENT_SERVICE_MODE", "functional-uat"),
        (
            "CUSTOMS_BACKEND_DATABASE_URL",
            "postgresql://customs:" + database_password + "@127.0.0.1:15432/customs",
        ),
        ("CUSTOMS_BACKEND_MINIO_ENDPOINT", "127.0.0.1:19000"),
        ("CUSTOMS_BACKEND_MINIO_ACCESS_KEY", minio_user),
        ("CUSTOMS_BACKEND_MINIO_SECRET_KEY", minio_password),
        ("CUSTOMS_BACKEND_OIDC_ISSUER", "http://127.0.0.1:18088/realms/customs"),
        ("CUSTOMS_BACKEND_OIDC_AUDIENCE", "customs-workbench-api"),
        (
            "CUSTOMS_BACKEND_OIDC_JWKS_URL",
            "http://127.0.0.1:18088/realms/customs/protocol/openid-connect/certs",
        ),
        ("CUSTOMS_BACKEND_OIDC_CLIENT_ID", "customs-workbench-spa"),
        (
            "CUSTOMS_BACKEND_OIDC_AUTHORIZATION_ENDPOINT",
            "http://127.0.0.1:18088/realms/customs/protocol/openid-connect/auth",
        ),
        (
            "CUSTOMS_BACKEND_OIDC_TOKEN_ENDPOINT",
            "http://127.0.0.1:18088/realms/customs/protocol/openid-connect/token",
        ),
        (
            "CUSTOMS_BACKEND_OIDC_END_SESSION_ENDPOINT",
            "http://127.0.0.1:18088/realms/customs/protocol/openid-connect/logout",
        ),
        # Compose-only values; Backend ignores them after the local stores start.
        ("CUSTOMS_FUNCTIONAL_POSTGRES_PASSWORD", database_password),
        ("CUSTOMS_FUNCTIONAL_MINIO_ROOT_USER", minio_user),
        ("CUSTOMS_FUNCTIONAL_MINIO_ROOT_PASSWORD", minio_password),
        ("CUSTOMS_FUNCTIONAL_KEYCLOAK_ADMIN_PASSWORD", keycloak_admin_password),
        ("CUSTOMS_FUNCTIONAL_REVIEWER_USERNAME", reviewer_username),
        ("CUSTOMS_FUNCTIONAL_REVIEWER_PASSWORD", reviewer_password),
        ("CUSTOMS_FUNCTIONAL_APPROVER_USERNAME", approver_username),
        ("CUSTOMS_FUNCTIONAL_APPROVER_PASSWORD", approver_password),
        ("CUSTOMS_FUNCTIONAL_REALM_FILE", realm_path.as_posix()),
    ]
    realm: dict[str, object] = {
        "realm": "customs",
        "enabled": True,
        # Local UAT sessions must survive a full demo/recording day. Keycloak's
        # defaults (5-minute access tokens, 30-minute idle) force a re-login in
        # the middle of a journey; the Browser renews the access token silently
        # against these longer, loopback-only lifetimes.
        "accessTokenLifespan": 3600,
        "ssoSessionIdleTimeout": 43200,
        "ssoSessionMaxLifespan": 43200,
        "clientSessionIdleTimeout": 43200,
        "clientSessionMaxLifespan": 43200,
        "roles": {"realm": [{"name": "reviewer"}, {"name": "approver"}]},
        "clients": [
            {
                "clientId": "customs-workbench-spa",
                "enabled": True,
                "publicClient": True,
                "protocol": "openid-connect",
                "standardFlowEnabled": True,
                "directAccessGrantsEnabled": False,
                "redirectUris": ["http://127.0.0.1:5173/*"],
                "webOrigins": ["http://127.0.0.1:5173"],
                "protocolMappers": [
                    {
                        "name": "backend-audience",
                        "protocol": "openid-connect",
                        "protocolMapper": "oidc-audience-mapper",
                        "config": {
                            "included.client.audience": "customs-workbench-api",
                            "access.token.claim": "true",
                            "id.token.claim": "true",
                        },
                    },
                    {
                        "name": "roles",
                        "protocol": "openid-connect",
                        "protocolMapper": "oidc-usermodel-realm-role-mapper",
                        "config": {
                            "claim.name": "roles",
                            "multivalued": "true",
                            "access.token.claim": "true",
                            "id.token.claim": "true",
                        },
                    },
                    {
                        "name": "organization",
                        "protocol": "openid-connect",
                        "protocolMapper": "oidc-usermodel-attribute-mapper",
                        "config": {
                            "user.attribute": "org_id",
                            "claim.name": "org_id",
                            "jsonType.label": "String",
                            "access.token.claim": "true",
                            "id.token.claim": "true",
                        },
                    },
                ],
            }
        ],
        "users": [
            {
                "username": reviewer_username,
                "enabled": True,
                "firstName": "Functional",
                "lastName": "Reviewer",
                "email": "local-functional-reviewer@localhost",
                "emailVerified": True,
                "attributes": {"org_id": ["local-functional"]},
                "realmRoles": ["reviewer"],
                "credentials": [
                    {"type": "password", "temporary": False, "value": reviewer_password}
                ],
            },
            {
                "username": approver_username,
                "enabled": True,
                "firstName": "Functional",
                "lastName": "Approver",
                "email": "local-functional-approver@localhost",
                "emailVerified": True,
                "attributes": {"org_id": ["local-functional"]},
                "realmRoles": ["approver"],
                "credentials": [
                    {"type": "password", "temporary": False, "value": approver_password}
                ],
            },
        ],
    }
    realm.update(FUNCTIONAL_UAT_REALM_BRANDING)
    return values, realm


def _write(path: Path, values: list[tuple[str, str]], *, force: bool) -> None:
    workspace_local = (_workspace_root() / "_local" / "profiles").resolve()
    target = path.resolve()
    if not target.is_relative_to(workspace_local):
        raise ValueError("output must remain below the shared _local/profiles directory")
    if target.exists() and not force:
        raise FileExistsError("profile already exists; keep it or pass --force to replace it")
    target.parent.mkdir(parents=True, exist_ok=True)
    content = "\n".join(
        [
            "# Generated local-only configuration. Never commit or send this file.",
            "# Regenerate with customs-backend-profile-init when a profile is reset.",
            *(f"{key}={value}" for key, value in values),
            "",
        ]
    )
    temporary = target.with_suffix(target.suffix + ".tmp")
    try:
        temporary.write_text(content, encoding="utf-8", newline="\n")
        _restrict_to_owner(temporary)
        os.replace(temporary, target)
        _restrict_to_owner(target)
    finally:
        if temporary.exists():
            temporary.unlink(missing_ok=True)


def _write_realm(path: Path, realm: dict[str, object], *, force: bool) -> None:
    if path.exists() and not force:
        raise FileExistsError("functional UAT realm already exists; keep it or pass --force")
    temporary = path.with_suffix(path.suffix + ".tmp")
    try:
        temporary.write_text(
            json.dumps(realm, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
            newline="\n",
        )
        _restrict_to_owner(temporary)
        os.replace(temporary, path)
        _restrict_to_owner(path)
    finally:
        if temporary.exists():
            temporary.unlink(missing_ok=True)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--profile", choices=("dev-fixture", "functional-uat"), required=True)
    parser.add_argument("--force", action="store_true", help="replace this local profile")
    args = parser.parse_args()
    target = _target_path(args.profile)
    if args.profile == "dev-fixture":
        _write(target, _dev_fixture(), force=args.force)
    else:
        realm_path = target.parent / "customs-realm.json"
        if not args.force and (target.exists() or realm_path.exists()):
            raise FileExistsError("functional UAT profile already exists; keep it or pass --force")
        values, realm = _functional_uat(realm_path=realm_path)
        _write(target, values, force=args.force)
        _write_realm(realm_path, realm, force=args.force)
    print(f"Created _local/profiles/{args.profile}/backend.env")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
