"""Synchronize only the local Functional-UAT Keycloak display and login theme.

The tool intentionally does not import or reset a realm.  It reads the local
bootstrap-admin password in memory, updates only the five branding fields, and
prints a compact result with field names only.  Realm representations, tokens,
and credentials never leave this process through stdout or stderr.
"""

from __future__ import annotations

import argparse
import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx

from customs_backend.functional_uat_branding import (
    FUNCTIONAL_UAT_KEYCLOAK_BASE_URL,
    FUNCTIONAL_UAT_KEYCLOAK_REALM,
    FUNCTIONAL_UAT_REALM_BRANDING,
)


class KeycloakBrandingSyncError(RuntimeError):
    """The local Keycloak branding state could not be read or updated safely."""


_PROFILE_KEY = "CUSTOMS_FUNCTIONAL_KEYCLOAK_ADMIN_PASSWORD"
_TOKEN_PATH = "/realms/master/protocol/openid-connect/token"
_REALM_PATH = f"/admin/realms/{FUNCTIONAL_UAT_KEYCLOAK_REALM}"


@dataclass(frozen=True, slots=True)
class BrandingSyncResult:
    status: str
    changed_fields: tuple[str, ...]


def _workspace_root() -> Path:
    return Path(__file__).resolve().parents[2]


def _profile_path() -> Path:
    path = _workspace_root() / "_local" / "profiles" / "functional-uat" / "backend.env"
    if not path.is_file() or path.is_symlink():
        raise KeycloakBrandingSyncError("functional_uat_keycloak_profile_unavailable")
    return path


def _read_admin_password(path: Path) -> str:
    values: list[str] = []
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError as exc:
        raise KeycloakBrandingSyncError("functional_uat_keycloak_profile_unavailable") from exc
    for line in lines:
        if line.lstrip().startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        if key == _PROFILE_KEY:
            values.append(value)
    if len(values) != 1 or not values[0]:
        raise KeycloakBrandingSyncError("functional_uat_keycloak_profile_incomplete")
    return values[0]


def _json_mapping(response: httpx.Response, *, error_code: str) -> Mapping[str, Any]:
    try:
        payload = response.json()
    except json.JSONDecodeError as exc:
        raise KeycloakBrandingSyncError(error_code) from exc
    if not isinstance(payload, Mapping):
        raise KeycloakBrandingSyncError(error_code)
    return payload


def _access_token(client: httpx.Client, password: str) -> str:
    try:
        response = client.post(
            _TOKEN_PATH,
            data={
                "client_id": "admin-cli",
                "grant_type": "password",
                "username": "local-functional-admin",
                "password": password,
            },
        )
    except httpx.HTTPError as exc:
        raise KeycloakBrandingSyncError("functional_uat_keycloak_token_unavailable") from exc
    if response.status_code != 200:
        raise KeycloakBrandingSyncError("functional_uat_keycloak_token_rejected")
    payload = _json_mapping(response, error_code="functional_uat_keycloak_token_invalid")
    token = payload.get("access_token")
    if not isinstance(token, str) or len(token) < 16:
        raise KeycloakBrandingSyncError("functional_uat_keycloak_token_invalid")
    return token


def _authorization(token: str) -> dict[str, str]:
    return {"Authorization": "Bearer " + token}


def _realm_representation(client: httpx.Client, token: str) -> Mapping[str, Any]:
    try:
        response = client.get(_REALM_PATH, headers=_authorization(token))
    except httpx.HTTPError as exc:
        raise KeycloakBrandingSyncError("functional_uat_keycloak_realm_unavailable") from exc
    if response.status_code != 200:
        raise KeycloakBrandingSyncError("functional_uat_keycloak_realm_unavailable")
    realm = _json_mapping(response, error_code="functional_uat_keycloak_realm_invalid")
    if realm.get("realm") != FUNCTIONAL_UAT_KEYCLOAK_REALM:
        raise KeycloakBrandingSyncError("functional_uat_keycloak_realm_invalid")
    return realm


def _changed_fields(realm: Mapping[str, Any]) -> tuple[str, ...]:
    return tuple(
        key for key, value in FUNCTIONAL_UAT_REALM_BRANDING.items() if realm.get(key) != value
    )


def _sync_with_client(client: httpx.Client, *, admin_password: str) -> BrandingSyncResult:
    token = _access_token(client, admin_password)
    realm = _realm_representation(client, token)
    changed_fields = _changed_fields(realm)
    if not changed_fields:
        return BrandingSyncResult(status="already_current", changed_fields=())

    # Send the narrowest possible top-level representation.  The realm name is
    # an identity guard; users, roles, clients, OIDC flow, and all unrelated
    # realm values are deliberately absent from the update payload.
    updated_realm = {"realm": FUNCTIONAL_UAT_KEYCLOAK_REALM, **FUNCTIONAL_UAT_REALM_BRANDING}
    try:
        response = client.put(_REALM_PATH, headers=_authorization(token), json=updated_realm)
    except httpx.HTTPError as exc:
        raise KeycloakBrandingSyncError("functional_uat_keycloak_update_unavailable") from exc
    if response.status_code != 204:
        raise KeycloakBrandingSyncError("functional_uat_keycloak_update_rejected")

    verified = _realm_representation(client, token)
    if _changed_fields(verified):
        raise KeycloakBrandingSyncError("functional_uat_keycloak_update_unverified")
    return BrandingSyncResult(status="updated", changed_fields=changed_fields)


def sync() -> BrandingSyncResult:
    """Apply idempotent local realm branding without displaying sensitive values."""

    password = _read_admin_password(_profile_path())
    try:
        with httpx.Client(
            base_url=FUNCTIONAL_UAT_KEYCLOAK_BASE_URL,
            trust_env=False,
            timeout=httpx.Timeout(15.0, connect=5.0),
        ) as client:
            return _sync_with_client(client, admin_password=password)
    except httpx.HTTPError as exc:
        raise KeycloakBrandingSyncError("functional_uat_keycloak_service_unavailable") from exc


def main(argv: Sequence[str] | None = None) -> int:
    argparse.ArgumentParser(description=__doc__).parse_args(argv)
    try:
        result = sync()
    except KeycloakBrandingSyncError as exc:
        print(
            json.dumps(
                {"functional_uat_keycloak_branding": "failed", "reason": str(exc)},
                ensure_ascii=False,
            )
        )
        return 1
    print(
        json.dumps(
            {
                "functional_uat_keycloak_branding": result.status,
                "changed_fields": list(result.changed_fields),
            },
            ensure_ascii=False,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
