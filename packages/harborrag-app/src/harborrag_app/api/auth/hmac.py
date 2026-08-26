"""HS256 JWT verifier against a shared secret (ST4 dev/compose auth).

OIDC/JWKS verification for Okta/Azure AD replaces this in M5 behind the same
BaseTokenVerifier seam.
"""

from __future__ import annotations

from dataclasses import dataclass

import jwt

from harborrag_app.api.auth.base import BaseTokenVerifier
from harborrag_app.api.auth.principal import ROLE_ORDER, Principal
from harborrag_core.contracts.errors import HarborAuthError


@dataclass(slots=True)
class HmacTokenVerifier(BaseTokenVerifier):
    """Verify HS256-signed JWTs carrying `sub` and `role` claims."""

    secret: str
    algorithm: str = "HS256"
    issuer: str = "harborrag"
    audience: str = "harborrag-api"
    max_token_lifetime_seconds: int = 3600
    clock_skew_seconds: int = 30

    def verify(self, token: str) -> Principal:
        """Decode + validate the JWT; map all PyJWT failures to HarborAuthError."""
        try:
            claims = jwt.decode(
                token,
                self.secret,
                algorithms=[self.algorithm],
                audience=self.audience,
                issuer=self.issuer,
                leeway=self.clock_skew_seconds,
                options={"require": ["sub", "role", "tenants", "iat", "exp", "iss", "aud"]},
            )
        except jwt.ExpiredSignatureError as exc:
            raise HarborAuthError("token expired") from exc
        except jwt.InvalidTokenError as exc:
            raise HarborAuthError("invalid token") from exc
        issued_at = claims.get("iat")
        expires_at = claims.get("exp")
        if (
            not isinstance(issued_at, (int, float))
            or isinstance(issued_at, bool)
            or not isinstance(expires_at, (int, float))
            or isinstance(expires_at, bool)
            or expires_at <= issued_at
            or expires_at - issued_at > self.max_token_lifetime_seconds
        ):
            raise HarborAuthError("invalid token lifetime")
        subject = claims.get("sub")
        if not isinstance(subject, str) or not subject:
            raise HarborAuthError("token missing sub claim")
        role = claims.get("role")
        if not isinstance(role, str) or role not in ROLE_ORDER:
            raise HarborAuthError(f"unknown role {role!r}")
        raw_tenants = claims.get("tenants")
        if (
            not isinstance(raw_tenants, list)
            or not raw_tenants
            or any(not isinstance(item, str) or not item for item in raw_tenants)
        ):
            raise HarborAuthError("token has invalid tenants claim")
        return Principal(
            subject=subject,
            role=role,
            tenant_ids=frozenset(raw_tenants),
            token_kind="jwt",
        )
