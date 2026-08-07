from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest

from customs_backend.functional_uat_branding import FUNCTIONAL_UAT_REALM_BRANDING
from tools import sync_functional_uat_keycloak_branding as branding


def _realm(*, current: bool) -> dict[str, object]:
    value: dict[str, object] = {
        "realm": "customs",
        "bruteForceProtected": True,
        "roles": {"realm": [{"name": "reviewer"}, {"name": "approver"}]},
        "clients": [{"clientId": "customs-workbench-spa", "standardFlowEnabled": True}],
    }
    if current:
        value.update(FUNCTIONAL_UAT_REALM_BRANDING)
    else:
        value.update(
            {
                "displayName": "old name",
                "internationalizationEnabled": False,
                "supportedLocales": ["en"],
                "defaultLocale": "en",
                "loginTheme": "keycloak",
            }
        )
    return value


def test_sync_updates_only_branding_and_preserves_existing_realm_values() -> None:
    state = {"realm": _realm(current=False)}
    calls: list[tuple[str, str]] = []
    put_body: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append((request.method, request.url.path))
        if request.url.path == "/realms/master/protocol/openid-connect/token":
            return httpx.Response(200, json={"access_token": "t" * 32}, request=request)
        if request.method == "GET" and request.url.path == "/admin/realms/customs":
            return httpx.Response(200, json=state["realm"], request=request)
        if request.method == "PUT" and request.url.path == "/admin/realms/customs":
            put_body.update(json.loads(request.content))
            state["realm"].update(put_body)
            return httpx.Response(204, request=request)
        return httpx.Response(404, request=request)

    with httpx.Client(base_url="http://127.0.0.1:18088", transport=httpx.MockTransport(handler)) as client:
        result = branding._sync_with_client(client, admin_password="local-admin-test-value")

    assert result.status == "updated"
    assert result.changed_fields == tuple(FUNCTIONAL_UAT_REALM_BRANDING)
    assert {key: put_body[key] for key in FUNCTIONAL_UAT_REALM_BRANDING} == (
        FUNCTIONAL_UAT_REALM_BRANDING
    )
    assert set(put_body) == {"realm", *FUNCTIONAL_UAT_REALM_BRANDING}
    assert "bruteForceProtected" not in put_body
    assert "roles" not in put_body
    assert "clients" not in put_body
    assert calls == [
        ("POST", "/realms/master/protocol/openid-connect/token"),
        ("GET", "/admin/realms/customs"),
        ("PUT", "/admin/realms/customs"),
        ("GET", "/admin/realms/customs"),
    ]


def test_sync_is_idempotent_when_branding_is_already_current() -> None:
    calls: list[tuple[str, str]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append((request.method, request.url.path))
        if request.url.path == "/realms/master/protocol/openid-connect/token":
            return httpx.Response(200, json={"access_token": "t" * 32}, request=request)
        if request.method == "GET" and request.url.path == "/admin/realms/customs":
            return httpx.Response(200, json=_realm(current=True), request=request)
        return httpx.Response(404, request=request)

    with httpx.Client(base_url="http://127.0.0.1:18088", transport=httpx.MockTransport(handler)) as client:
        result = branding._sync_with_client(client, admin_password="local-admin-test-value")

    assert result.status == "already_current"
    assert result.changed_fields == ()
    assert calls == [
        ("POST", "/realms/master/protocol/openid-connect/token"),
        ("GET", "/admin/realms/customs"),
    ]


def test_sync_constructs_a_literal_loopback_client_without_proxy_trust(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, object] = {}

    class CapturingClient:
        def __init__(self, **kwargs: object) -> None:
            captured.update(kwargs)

        def __enter__(self) -> CapturingClient:
            return self

        def __exit__(self, *_: object) -> None:
            return None

    monkeypatch.setenv("HTTP_PROXY", "http://proxy.invalid:8080")
    monkeypatch.setattr(branding, "_profile_path", lambda: Path("backend.env"))
    monkeypatch.setattr(branding, "_read_admin_password", lambda _: "local-admin-test-value")
    monkeypatch.setattr(
        branding,
        "_sync_with_client",
        lambda _client, *, admin_password: branding.BrandingSyncResult(
            status="already_current", changed_fields=()
        ),
    )
    monkeypatch.setattr(branding.httpx, "Client", CapturingClient)

    assert branding.sync().status == "already_current"
    assert captured["base_url"] == branding.FUNCTIONAL_UAT_KEYCLOAK_BASE_URL
    assert captured["trust_env"] is False


def test_profile_reader_rejects_missing_or_duplicate_admin_password(tmp_path: Path) -> None:
    profile = tmp_path / "backend.env"
    profile.write_text("OTHER=value\n", encoding="utf-8")
    with pytest.raises(branding.KeycloakBrandingSyncError, match="profile_incomplete"):
        branding._read_admin_password(profile)

    profile.write_text(
        "CUSTOMS_FUNCTIONAL_KEYCLOAK_ADMIN_PASSWORD=one\n"
        "CUSTOMS_FUNCTIONAL_KEYCLOAK_ADMIN_PASSWORD=two\n",
        encoding="utf-8",
    )
    with pytest.raises(branding.KeycloakBrandingSyncError, match="profile_incomplete"):
        branding._read_admin_password(profile)


def test_cli_prints_only_safe_status_and_field_names(monkeypatch: pytest.MonkeyPatch, capsys) -> None:
    monkeypatch.setattr(
        branding,
        "sync",
        lambda: branding.BrandingSyncResult(
            status="updated", changed_fields=tuple(FUNCTIONAL_UAT_REALM_BRANDING)
        ),
    )

    assert branding.main([]) == 0
    payload = json.loads(capsys.readouterr().out)

    assert payload == {
        "functional_uat_keycloak_branding": "updated",
        "changed_fields": list(FUNCTIONAL_UAT_REALM_BRANDING),
    }


def test_custom_theme_is_css_and_korean_messages_only() -> None:
    root = Path(__file__).resolve().parents[2] / "deploy" / "functional-uat" / "keycloak-themes"
    theme = root / "customs-workbench" / "login"
    files = {path.relative_to(theme).as_posix() for path in theme.rglob("*") if path.is_file()}

    assert files == {
        "theme.properties",
        "messages/messages_ko.properties",
        "resources/css/customs-workbench.css",
    }
    properties = (theme / "theme.properties").read_text(encoding="utf-8")
    assert "parent=keycloak" in properties
    assert "locales=ko" in properties
    messages = (theme / "messages" / "messages_ko.properties").read_text(encoding="utf-8")
    assert "loginTitle=통관 AI 사전심사 작업대" in messages


def test_compose_mounts_the_theme_read_only() -> None:
    compose = (
        Path(__file__).resolve().parents[2] / "deploy" / "functional-uat" / "compose.yaml"
    ).read_text(encoding="utf-8")

    assert "source: ./keycloak-themes/customs-workbench" in compose
    assert "target: /opt/keycloak/themes/customs-workbench" in compose
    assert compose.count("read_only: true") >= 2
