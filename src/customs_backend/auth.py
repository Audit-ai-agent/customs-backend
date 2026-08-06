from __future__ import annotations

from collections.abc import Callable
from threading import RLock
from time import monotonic
from typing import Any

from fastapi import Depends, HTTPException, Request, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from customs_backend.domain import Principal, Role
from customs_backend.ports import AuthorizationError, TokenVerifier


class OidcJwtVerifier:
    """Validate signature and mandatory OIDC claims; never decode unverified JWTs."""

    def __init__(
        self,
        *,
        issuer: str | None,
        audience: str | None,
        jwks_url: str | None,
        org_claim: str = "org_id",
        roles_claim: str = "roles",
    ) -> None:
        try:
            import jwt
            from jwt import PyJWKClient, PyJWKClientError
        except ImportError as exc:  # pragma: no cover - functional-UAT dependency
            raise RuntimeError("install the functional-uat extra to use the OIDC verifier") from exc
        self._issuer = issuer
        self._audience = audience
        self._jwks_url = jwks_url
        self._org_claim = org_claim
        self._roles_claim = roles_claim
        self._jwt = jwt
        self._pyjwt_client_error = PyJWKClientError
        self._jwks = (
            PyJWKClient(
                jwks_url,
                cache_keys=True,
                max_cached_keys=16,
                cache_jwk_set=True,
                lifespan=300,
                timeout=3.0,
            )
            if jwks_url
            else None
        )
        self._jwks_lock = RLock()
        self._known_keys: dict[str, Any] = {}
        self._negative_kids: dict[str, float] = {}
        self._last_unknown_refresh = 0.0
        self._unknown_refresh_interval_seconds = 60.0

    def _load_keys(self, *, refresh: bool) -> dict[str, Any]:
        if self._jwks is None:
            return {}
        keys = {
            value.key_id: value
            for value in self._jwks.get_signing_keys(refresh=refresh)
            if isinstance(value.key_id, str) and value.key_id
        }
        self._known_keys = keys
        return keys

    def _signing_key(self, token: str) -> Any:
        try:
            header = self._jwt.get_unverified_header(token)
        except self._jwt.PyJWTError as exc:
            raise AuthorizationError("invalid bearer token") from exc
        kid = header.get("kid")
        if not isinstance(kid, str) or not kid or len(kid) > 256:
            raise AuthorizationError("invalid bearer token")
        now = monotonic()
        with self._jwks_lock:
            self._negative_kids = {
                value: expires_at
                for value, expires_at in self._negative_kids.items()
                if expires_at > now
            }
            if kid in self._negative_kids:
                raise AuthorizationError("invalid bearer token")
            keys = self._known_keys or self._load_keys(refresh=False)
            signing_key = keys.get(kid)
            if signing_key is None and (
                now - self._last_unknown_refresh >= self._unknown_refresh_interval_seconds
            ):
                self._last_unknown_refresh = now
                keys = self._load_keys(refresh=True)
                signing_key = keys.get(kid)
            if signing_key is None:
                if len(self._negative_kids) >= 1_024:
                    self._negative_kids.clear()
                self._negative_kids[kid] = now + self._unknown_refresh_interval_seconds
                raise AuthorizationError("invalid bearer token")
            return signing_key

    @property
    def configured(self) -> bool:
        return bool(self._issuer and self._audience and self._jwks)

    def health(self) -> bool:
        if not self.configured or self._jwks is None:
            return False
        try:
            with self._jwks_lock:
                keys = self._known_keys or self._load_keys(refresh=False)
        except Exception:
            return False
        return bool(keys)

    def verify(self, token: str) -> Principal:
        if not self.configured or self._jwks is None:
            raise AuthorizationError("OIDC verifier is not configured")
        try:
            signing_key = self._signing_key(token)
            claims = self._jwt.decode(
                token,
                signing_key.key,
                algorithms=["RS256", "ES256"],
                issuer=self._issuer,
                audience=self._audience,
                options={
                    "require": ["exp", "iat", "iss", "aud", "sub", self._org_claim],
                },
            )
        except (self._jwt.PyJWTError, self._pyjwt_client_error) as exc:
            raise AuthorizationError("invalid bearer token") from exc
        subject = claims.get("sub")
        org_id = claims.get(self._org_claim)
        if not isinstance(subject, str) or not isinstance(org_id, str):
            raise AuthorizationError("subject and organization claims must be strings")
        roles_value = claims.get(self._roles_claim, [])
        if not isinstance(roles_value, list):
            raise AuthorizationError("roles claim must be an array")
        roles = frozenset(
            Role(value)
            for value in roles_value
            if isinstance(value, str) and value in Role._value2member_map_
        )
        if not roles:
            raise AuthorizationError("no supported role claim")
        try:
            return Principal(subject=subject, org_id=org_id, roles=roles)
        except ValueError as exc:
            raise AuthorizationError("subject or organization claim is invalid") from exc


class StaticTokenVerifier:
    """Explicit test adapter; it is never selected from environment variables."""

    def __init__(self, tokens: dict[str, Principal]) -> None:
        self._tokens = tokens

    def verify(self, token: str) -> Principal:
        try:
            return self._tokens[token]
        except KeyError as exc:
            raise AuthorizationError("invalid test token") from exc


bearer = HTTPBearer(auto_error=False)


def principal_dependency(
    request: Request,
    credentials: HTTPAuthorizationCredentials | None = Depends(bearer),
) -> Principal:
    if credentials is None or credentials.scheme.casefold() != "bearer":
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, detail="bearer token required"
        )
    verifier: TokenVerifier = request.app.state.container.token_verifier
    try:
        return verifier.verify(credentials.credentials)
    except AuthorizationError:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, detail="invalid bearer token"
        ) from None


def require_roles(*allowed: Role) -> Callable[[Principal], Principal]:
    allowed_set = frozenset(allowed)

    def dependency(principal: Principal = Depends(principal_dependency)) -> Principal:
        if not principal.roles.intersection(allowed_set):
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="insufficient role")
        return principal

    return dependency
