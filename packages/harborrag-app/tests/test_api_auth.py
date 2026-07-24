"""Bearer JWT auth + RBAC enforcement on the diagnostics route (ST4/ST9)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import jwt
import pytest
from fastapi.testclient import TestClient

from harborrag_app.api.app import create_fastapi_app
from harborrag_app.api.auth.mock import MockTokenVerifier
from harborrag_app.api.settings import ApiSettings
from harborrag_core.contracts.errors import HarborAuthError, HarborCapabilityError

SECRET = "test-secret-at-least-32-bytes-long-for-hs256"
DIAG = "/api/v1/diagnostics"


def _token(role: str, *, expired: bool = False, secret: str = SECRET) -> str:
    """Mint an HS256 JWT with sub/role/exp claims for tests."""
    delta = timedelta(minutes=-5 if expired else 5)
    claims = {"sub": "u1", "role": role, "exp": datetime.now(UTC) + delta}
    return jwt.encode(claims, secret, algorithm="HS256")


def _hmac_client() -> TestClient:
    """Client against an app locked to hmac auth mode."""
    settings = ApiSettings(auth_mode="hmac", auth_secret=SECRET)
    return TestClient(create_fastapi_app(settings), raise_server_exceptions=False)


@pytest.mark.blackbox
def test_none_mode_grants_implicit_owner() -> None:
    """auth_mode=none (dev default) reaches admin-gated diagnostics."""
    with TestClient(create_fastapi_app(ApiSettings())) as client:
        response = client.get(DIAG)
        assert response.status_code == 200
        assert response.json()["principal"] == {"subject": "dev", "role": "owner"}


@pytest.mark.blackbox
@pytest.mark.parametrize(
    ("headers", "expected_status"),
    [
        ({}, 401),  # missing token
        ({"Authorization": "Bearer garbage"}, 401),  # unparseable
        ({"Authorization": f"Bearer {_token('admin', expired=True)}"}, 401),
        (
            {
                "Authorization": f"Bearer {_token('admin', secret='wrong-secret-also-32-bytes-long!!')}"
            },
            401,
        ),
        ({"Authorization": f"Bearer {_token('reader')}"}, 403),  # authned, low role
        ({"Authorization": f"Bearer {_token('editor')}"}, 403),
        ({"Authorization": f"Bearer {_token('admin')}"}, 200),
        ({"Authorization": f"Bearer {_token('owner')}"}, 200),
    ],
    ids=[
        "missing",
        "garbage",
        "expired",
        "bad-signature",
        "reader",
        "editor",
        "admin",
        "owner",
    ],
)
def test_hmac_mode_enforces_auth_and_roles(headers: dict[str, str], expected_status: int) -> None:
    """hmac mode: 401 for bad/missing tokens, 403 below admin, 200 at admin+;
    failures always use the harbor_auth_error envelope."""
    with _hmac_client() as client:
        response = client.get(DIAG, headers=headers)
        assert response.status_code == expected_status
        if expected_status != 200:
            assert response.json()["error"]["code"] == "harbor_auth_error"


@pytest.mark.blackbox
def test_diagnostics_never_echoes_the_auth_secret() -> None:
    """The settings echo redacts auth_secret (plan §8.2: no secret values out)."""
    with _hmac_client() as client:
        response = client.get(DIAG, headers={"Authorization": f"Bearer {_token('admin')}"})
        assert response.status_code == 200
        assert SECRET not in response.text
        assert response.json()["settings"]["auth_secret"] == "<redacted>"


@pytest.mark.blackbox
def test_oidc_mode_is_a_declared_missing_capability() -> None:
    """auth_mode=oidc fails fast at factory time until M5 delivers it."""
    with pytest.raises(HarborCapabilityError):
        create_fastapi_app(ApiSettings(auth_mode="oidc"))


@pytest.mark.blackbox
def test_non_string_role_claim_is_401_not_500() -> None:
    """A JWT whose role claim is not a string (e.g. a list) must be rejected
    as 401 harbor_auth_error, never crash the lookup into a 500."""
    bad = jwt.encode(
        {"sub": "u1", "role": [], "exp": datetime.now(UTC) + timedelta(minutes=5)},
        SECRET,
        algorithm="HS256",
    )
    with _hmac_client() as client:
        response = client.get(DIAG, headers={"Authorization": f"Bearer {bad}"})
        assert response.status_code == 401
        assert response.json()["error"]["code"] == "harbor_auth_error"


@pytest.mark.blackbox
def test_prod_env_refuses_disabled_auth() -> None:
    """Fail closed: env=prod with auth_mode=none must not boot."""
    from harborrag_core.contracts.errors import HarborConfigurationError

    with pytest.raises(HarborConfigurationError):
        create_fastapi_app(ApiSettings(env="prod", auth_mode="none"))


@pytest.mark.whitebox
def test_mock_verifier_is_deterministic() -> None:
    """MockTokenVerifier accepts mock-<role> and rejects everything else."""
    verifier = MockTokenVerifier()
    principal = verifier.verify("mock-admin")
    assert (principal.subject, principal.role) == ("mock-user", "admin")
    with pytest.raises(HarborAuthError):
        verifier.verify("mock-santa")
    with pytest.raises(HarborAuthError):
        verifier.verify("real-admin")
