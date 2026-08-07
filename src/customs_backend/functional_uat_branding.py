"""Small, local-only branding constants shared by Functional-UAT setup tools."""

from __future__ import annotations

from typing import Final


FUNCTIONAL_UAT_KEYCLOAK_BASE_URL: Final = "http://127.0.0.1:18088"
FUNCTIONAL_UAT_KEYCLOAK_REALM: Final = "customs"
FUNCTIONAL_UAT_LOGIN_THEME: Final = "customs-workbench"
FUNCTIONAL_UAT_DISPLAY_NAME: Final = "통관 AI 사전심사 작업대"
FUNCTIONAL_UAT_LOCALE: Final = "ko"
FUNCTIONAL_UAT_REALM_BRANDING: Final[dict[str, object]] = {
    "displayName": FUNCTIONAL_UAT_DISPLAY_NAME,
    "internationalizationEnabled": True,
    "supportedLocales": [FUNCTIONAL_UAT_LOCALE],
    "defaultLocale": FUNCTIONAL_UAT_LOCALE,
    "loginTheme": FUNCTIONAL_UAT_LOGIN_THEME,
}


__all__ = [
    "FUNCTIONAL_UAT_DISPLAY_NAME",
    "FUNCTIONAL_UAT_KEYCLOAK_BASE_URL",
    "FUNCTIONAL_UAT_KEYCLOAK_REALM",
    "FUNCTIONAL_UAT_LOCALE",
    "FUNCTIONAL_UAT_LOGIN_THEME",
    "FUNCTIONAL_UAT_REALM_BRANDING",
]
