"""HS256 JWT verifier against a shared secret (ST4 dev/compose auth).

OIDC/JWKS verification for Okta/Azure AD replaces this in M5 behind the same
BaseTokenVerifier seam.
"""

from __future__ import annotations

from dataclasses import dataclass

import jwt
from harborrag_core.contracts.errors import HarborAuthError

from harborrag_app.api.auth.base import BaseTokenVerifier
from harborrag_app.api.auth.principal import ROLE_ORDER, Principal


@dataclass(slots=True)
class HmacTokenVerifier(BaseTokenVerifier):
    """Verify HS256-signed JWTs carrying `sub` and `role` claims."""

    secret: str
    algorithm: str = "HS256"

    def verify(self, token: str) -> Principal:
        """Decode + validate the JWT; map all PyJWT failures to HarborAuthError."""
        try:
            claims = jwt.decode(token, self.secret, algorithms=[self.algorithm])
        except jwt.ExpiredSignatureError as exc:
            raise HarborAuthError("token expired") from exc
        except jwt.InvalidTokenError as exc:
            raise HarborAuthError("invalid token") from exc
        subject = claims.get("sub")
        if not isinstance(subject, str) or not subject:
            raise HarborAuthError("token missing sub claim")
        role = claims.get("role", "reader")
        if not isinstance(role, str) or role not in ROLE_ORDER:
            raise HarborAuthError(f"unknown role {role!r}")
        return Principal(subject=subject, role=role, token_kind="jwt")
