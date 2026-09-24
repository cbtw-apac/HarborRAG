"""OIDC/JWKS JWT verifier (ML5-P1).

Verifies RS256-family access tokens against a configured JWKS endpoint --
issuer-neutral in code: Microsoft Entra ID is the production deployment
configuration (issuer/audience/JWKS URI), not an Azure SDK dependency. Any
standards-compliant OIDC provider works the same way.

No network call happens at construction time (``build_token_verifier`` runs
synchronously in the app factory, before the first request). The JWKS
document is fetched lazily on first use and cached by ``PyJWKClient``, which
also handles rotation: an unrecognized ``kid`` triggers one re-fetch, and a
transient fetch failure raises rather than silently falling back to an
unverified token.
"""

from __future__ import annotations

from dataclasses import dataclass, field

import jwt

from harborrag_app.api.auth.base import BaseTokenVerifier
from harborrag_app.api.auth.principal import ROLE_ORDER, Principal
from harborrag_core.contracts.errors import HarborAuthError
from harborrag_core.domain.member import Role


@dataclass(slots=True)
class OidcTokenVerifier(BaseTokenVerifier):
    """Verify RS256-signed JWTs against a JWKS endpoint (OIDC resource server)."""

    jwks_uri: str
    issuer: str
    audience: str
    algorithms: tuple[str, ...] = ("RS256",)
    max_token_lifetime_seconds: int = 3600
    clock_skew_seconds: int = 30
    user_id_claim: str = "sub"
    role_claim: str = "roles"
    tenant_claim: str = "tenants"
    jwks_cache_seconds: float = 300
    _jwks_client: jwt.PyJWKClient = field(init=False, repr=False)

    def __post_init__(self) -> None:
        self._jwks_client = jwt.PyJWKClient(
            self.jwks_uri,
            # Tier-1 (whole JWK Set) cache only, on a TTL: a key an operator
            # rotates out at the IdP must actually stop verifying once the
            # cache expires. PyJWKClient's Tier-2 per-kid cache has no TTL at
            # all -- keeping it enabled would let a revoked signing key keep
            # validating indefinitely once it had ever been looked up.
            cache_keys=False,
            cache_jwk_set=True,
            lifespan=self.jwks_cache_seconds,
        )

    def verify(self, token: str) -> Principal:
        """Decode + validate the JWT; map every PyJWT/JWKS failure to HarborAuthError."""
        try:
            signing_key = self._jwks_client.get_signing_key_from_jwt(token)
            claims = jwt.decode(
                token,
                signing_key.key,
                algorithms=list(self.algorithms),
                audience=self.audience,
                issuer=self.issuer,
                leeway=self.clock_skew_seconds,
                options={"require": ["sub", "iat", "exp", "iss", "aud"]},
            )
        except jwt.ExpiredSignatureError as exc:
            raise HarborAuthError("token expired") from exc
        except jwt.PyJWTError as exc:
            # Covers signature/claim failures (InvalidTokenError family) and
            # JWKS resolution failures (PyJWKClientError, including a
            # transient fetch outage) alike: never accept an unverifiable
            # token, and never leak which specific check failed.
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

        role = self._extract_role(claims)
        tenant_ids = self._extract_tenants(claims)

        user_id = claims.get(self.user_id_claim)
        if not isinstance(user_id, str) or not user_id.strip() or len(user_id) > 512:
            raise HarborAuthError("token has invalid user identity claim")

        return Principal(
            subject=subject,
            role=role,
            tenant_ids=tenant_ids,
            token_kind="jwt",
            user_id=user_id,
        )

    def _extract_role(self, claims: dict[str, object]) -> Role:
        """Read the role claim, accepting either a bare string or a JSON array.

        Entra app roles arrive as an array (a principal can be assigned more
        than one app role); when several recognized HarborRAG roles are
        present, the highest-privilege one wins rather than the first, so
        role ordering in the array configured on the Entra side is not
        security-relevant.
        """
        raw = claims.get(self.role_claim)
        if isinstance(raw, str):
            candidates: list[str] = [raw]
        elif isinstance(raw, list) and all(isinstance(item, str) for item in raw):
            candidates = raw
        else:
            raise HarborAuthError(f"token missing a usable {self.role_claim!r} claim")
        recognized = [role for role in candidates if role in ROLE_ORDER]
        if not recognized:
            raise HarborAuthError(f"token has no recognized role in {self.role_claim!r}")
        return max(recognized, key=lambda role: ROLE_ORDER[role])

    def _extract_tenants(self, claims: dict[str, object]) -> frozenset[str]:
        raw_tenants = claims.get(self.tenant_claim)
        if (
            not isinstance(raw_tenants, list)
            or not raw_tenants
            or any(not isinstance(item, str) or not item for item in raw_tenants)
        ):
            raise HarborAuthError(f"token has invalid {self.tenant_claim!r} claim")
        return frozenset(raw_tenants)


__all__ = ["OidcTokenVerifier"]
