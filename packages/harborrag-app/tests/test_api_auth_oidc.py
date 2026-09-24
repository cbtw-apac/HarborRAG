"""OIDC/JWKS verifier: config validation (fail closed) and token verification."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa
from jwt.algorithms import RSAAlgorithm

from harborrag_app.api.app import create_fastapi_app
from harborrag_app.api.auth.dependencies import build_token_verifier
from harborrag_app.api.auth.oidc import OidcTokenVerifier
from harborrag_app.api.settings import ApiSettings
from harborrag_core.contracts.errors import HarborAuthError, HarborConfigurationError

ISSUER = "https://login.microsoftonline.com/test-tenant/v2.0"
AUDIENCE = "api://harborrag"
JWKS_URI = "https://login.microsoftonline.com/test-tenant/discovery/v2.0/keys"
KID = "entra-key-1"

_private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
_public_jwk = RSAAlgorithm(RSAAlgorithm.SHA256).to_jwk(_private_key.public_key(), as_dict=True)
_public_jwk.update(kid=KID, use="sig", alg="RS256")


def _base_settings(**overrides: Any) -> ApiSettings:
    fields: dict[str, Any] = {
        "auth_mode": "oidc",
        "auth_issuer": ISSUER,
        "auth_audience": AUDIENCE,
        "oidc_jwks_uri": JWKS_URI,
    }
    fields.update(overrides)
    return ApiSettings(**fields)


def _token(
    *, kid: str = KID, key: Any = _private_key, algorithm: str = "RS256", **extra: object
) -> str:
    now = datetime.now(UTC)
    claims: dict[str, object] = {
        "sub": "entra-sub-1",
        "oid": "entra-object-1",
        "roles": ["editor"],
        "tenants": ["DEFAULT"],
        "iat": now - timedelta(seconds=1),
        "exp": now + timedelta(minutes=5),
        "iss": ISSUER,
        "aud": AUDIENCE,
        **extra,
    }
    return jwt.encode(claims, key, algorithm=algorithm, headers={"kid": kid})


def _verifier(**overrides: Any) -> OidcTokenVerifier:
    verifier = build_token_verifier(_base_settings(**overrides))
    assert isinstance(verifier, OidcTokenVerifier)
    verifier._jwks_client.fetch_data = lambda: {"keys": [_public_jwk]}  # type: ignore[method-assign]
    return verifier


# --- Startup config validation (fail closed) --------------------------------


def test_oidc_requires_jwks_uri() -> None:
    with pytest.raises(HarborConfigurationError, match="OIDC_JWKS_URI"):
        build_token_verifier(
            ApiSettings(auth_mode="oidc", auth_issuer=ISSUER, auth_audience=AUDIENCE)
        )


def test_oidc_rejects_insecure_jwks_uri_by_default() -> None:
    with pytest.raises(HarborConfigurationError, match="encrypted transport"):
        build_token_verifier(_base_settings(oidc_jwks_uri="http://evil.example/jwks"))


def test_oidc_rejects_malformed_jwks_uri() -> None:
    with pytest.raises(HarborConfigurationError, match="OIDC_JWKS_URI"):
        build_token_verifier(_base_settings(oidc_jwks_uri="not-a-url"))


def test_oidc_rejects_default_dev_issuer() -> None:
    """The hmac-mode default issuer ('harborrag') is not an absolute URL,
    so leaving auth_issuer unset for oidc fails startup instead of silently
    minting a verifier nothing will ever present a matching token for."""
    with pytest.raises(HarborConfigurationError, match="AUTH_ISSUER"):
        build_token_verifier(
            ApiSettings(auth_mode="oidc", auth_audience=AUDIENCE, oidc_jwks_uri=JWKS_URI)
        )


def test_oidc_rejects_blank_audience() -> None:
    with pytest.raises(HarborConfigurationError, match="AUTH_AUDIENCE"):
        build_token_verifier(
            ApiSettings(
                auth_mode="oidc",
                auth_issuer=ISSUER,
                auth_audience="   ",
                oidc_jwks_uri=JWKS_URI,
            )
        )


def test_oidc_rejects_none_algorithm() -> None:
    with pytest.raises(ValueError, match="'none'"):
        _base_settings(oidc_algorithms=["none"])


def test_app_factory_fails_closed_for_incomplete_oidc_config() -> None:
    with pytest.raises(HarborConfigurationError):
        create_fastapi_app(ApiSettings(auth_mode="oidc"))


# --- Token verification -------------------------------------------------


def test_valid_entra_shaped_token_is_accepted() -> None:
    verifier = _verifier(auth_user_id_claim="oid")

    principal = verifier.verify(_token())

    assert principal.subject == "entra-sub-1"
    assert principal.user_id == "entra-object-1"
    assert principal.role == "editor"
    assert principal.tenant_ids == frozenset({"DEFAULT"})
    assert principal.token_kind == "jwt"


def test_multiple_roles_claim_picks_highest_privilege() -> None:
    verifier = _verifier()

    principal = verifier.verify(_token(roles=["reader", "owner", "editor"]))

    assert principal.role == "owner"


def test_role_claim_as_bare_string_is_accepted() -> None:
    verifier = _verifier()

    principal = verifier.verify(_token(roles="admin"))

    assert principal.role == "admin"


@pytest.mark.parametrize(
    "extra",
    [
        {"roles": ["not-a-role"]},
        {"roles": []},
        {"roles": 42},
        {"tenants": []},
        {"tenants": "DEFAULT"},
        {"tenants": [1]},
    ],
)
def test_malformed_role_or_tenant_claim_is_401_not_500(extra: dict[str, object]) -> None:
    verifier = _verifier()

    with pytest.raises(HarborAuthError):
        verifier.verify(_token(**extra))


def test_expired_token_is_rejected() -> None:
    verifier = _verifier()
    now = datetime.now(UTC)

    with pytest.raises(HarborAuthError, match="expired"):
        verifier.verify(_token(iat=now - timedelta(minutes=10), exp=now - timedelta(minutes=5)))


def test_wrong_audience_is_rejected() -> None:
    verifier = _verifier()

    with pytest.raises(HarborAuthError):
        verifier.verify(_token(aud="api://someone-else"))


def test_wrong_issuer_is_rejected() -> None:
    verifier = _verifier()

    with pytest.raises(HarborAuthError):
        verifier.verify(_token(iss="https://attacker.example/v2.0"))


def test_unknown_kid_is_401_not_500() -> None:
    verifier = _verifier()

    with pytest.raises(HarborAuthError):
        verifier.verify(_token(kid="some-other-key"))


def test_garbage_token_is_401_not_500() -> None:
    verifier = _verifier()

    with pytest.raises(HarborAuthError):
        verifier.verify("not-a-jwt-at-all")


def test_hs256_signed_token_is_rejected_even_with_rs256_kid_header() -> None:
    """An attacker cannot downgrade to a symmetric algorithm the verifier
    never agreed to accept, even if they can control the kid header."""
    verifier = _verifier()
    forged = _token(key="attacker-controlled-secret", algorithm="HS256")

    with pytest.raises(HarborAuthError):
        verifier.verify(forged)


def test_transient_jwks_fetch_failure_does_not_admit_the_request() -> None:
    verifier = build_token_verifier(_base_settings())
    assert isinstance(verifier, OidcTokenVerifier)

    def _boom() -> dict[str, object]:
        raise jwt.PyJWKClientConnectionError("jwks endpoint unreachable")

    verifier._jwks_client.fetch_data = _boom  # type: ignore[method-assign]

    with pytest.raises(HarborAuthError):
        verifier.verify(_token())


def test_jwks_rotation_recovers_after_new_key_is_published() -> None:
    """A token signed by a key not yet in the cached JWKS is rejected until
    the client re-fetches and finds it -- exercising the same 'unknown kid
    triggers a re-fetch' path a real rotation goes through."""
    other_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    other_kid = "entra-key-2"
    other_jwk = RSAAlgorithm(RSAAlgorithm.SHA256).to_jwk(other_key.public_key(), as_dict=True)
    other_jwk.update(kid=other_kid, use="sig", alg="RS256")

    verifier = build_token_verifier(_base_settings())
    assert isinstance(verifier, OidcTokenVerifier)
    keysets = [{"keys": [_public_jwk]}, {"keys": [_public_jwk, other_jwk]}]
    verifier._jwks_client.fetch_data = lambda: keysets.pop(0) if len(keysets) > 1 else keysets[0]  # type: ignore[method-assign]

    rotated_token = _token(kid=other_kid, key=other_key)
    principal = verifier.verify(rotated_token)

    assert principal.subject == "entra-sub-1"
