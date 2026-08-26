"""Bearer JWT authentication and role enforcement on public API resources."""

from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta
from pathlib import Path

import jwt
import pytest
from app_test_auth import MockTokenVerifier
from fastapi.testclient import TestClient

from harborrag_app.api.app import create_fastapi_app
from harborrag_app.api.auth.dependencies import authorize_tenant
from harborrag_app.api.auth.principal import Principal
from harborrag_app.api.settings import ApiSettings
from harborrag_core.contracts.errors import HarborAuthError, HarborCapabilityError

SECRET = "test-secret-at-least-32-bytes-long-for-hs256"
PROTECTED = "/v1/ingestions/missing/cancel"


@pytest.fixture(autouse=True)
def _isolated_control_database(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setenv("HARBORRAG_ENV", "dev")
    monkeypatch.setenv(
        "HARBORRAG_CONTROL_DB_URL",
        f"sqlite+aiosqlite:///{tmp_path}/control.db",
    )


def _token(role: str, *, expired: bool = False, secret: str = SECRET) -> str:
    """Mint an HS256 JWT with sub/role/exp claims for tests."""
    delta = timedelta(minutes=-5 if expired else 5)
    now = datetime.now(UTC)
    claims = {
        "sub": "u1",
        "role": role,
        "tenants": ["DEFAULT"],
        "iat": now - timedelta(seconds=1),
        "exp": now + delta,
        "iss": "harborrag",
        "aud": "harborrag-api",
    }
    return jwt.encode(claims, secret, algorithm="HS256")


def _hmac_client() -> TestClient:
    """Client against an app locked to hmac auth mode."""
    settings = ApiSettings(auth_mode="hmac", auth_secret=SECRET)
    return TestClient(create_fastapi_app(settings), raise_server_exceptions=False)


@pytest.mark.blackbox
def test_none_mode_grants_implicit_owner() -> None:
    """auth_mode=none reaches an editor route as the implicit owner."""
    with TestClient(create_fastapi_app(ApiSettings())) as client:
        response = client.post(PROTECTED)
        assert response.status_code == 404
        assert response.json()["error"]["code"] == "INGESTION_NOT_FOUND"


@pytest.mark.blackbox
def test_metrics_requires_admin_in_hmac_mode() -> None:
    """Operational metrics do not expose process data without admin auth."""
    with _hmac_client() as client:
        assert client.get("/metrics").status_code == 404
        assert client.get("/api/v1/metrics").status_code == 401
        reader = client.get(
            "/api/v1/metrics",
            headers={"Authorization": f"Bearer {_token('reader')}"},
        )
        assert reader.status_code == 403
        admin = client.get(
            "/api/v1/metrics",
            headers={"Authorization": f"Bearer {_token('admin')}"},
        )
        assert admin.status_code == 200
        assert "harborrag_api_info" in admin.text


@pytest.mark.blackbox
@pytest.mark.parametrize(
    ("role", "expired", "secret", "literal_token", "expected_status"),
    [
        (None, False, SECRET, None, 401),
        (None, False, SECRET, "garbage", 401),
        ("admin", True, SECRET, None, 401),
        ("admin", False, "wrong-secret-also-32-bytes-long!!", None, 401),
        ("reader", False, SECRET, None, 403),
        ("editor", False, SECRET, None, 404),
        ("admin", False, SECRET, None, 404),
        ("owner", False, SECRET, None, 404),
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
def test_hmac_mode_enforces_auth_and_roles(
    role: str | None,
    expired: bool,
    secret: str,
    literal_token: str | None,
    expected_status: int,
) -> None:
    """HMAC mode rejects invalid tokens and readers below the editor role."""
    token = _token(role, expired=expired, secret=secret) if role is not None else literal_token
    headers = {"Authorization": f"Bearer {token}"} if token is not None else {}
    with _hmac_client() as client:
        response = client.post(PROTECTED, headers=headers)
        assert response.status_code == expected_status
        if expected_status in {401, 403}:
            assert response.json()["error"]["code"] == "harbor_auth_error"


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
        {
            "sub": "u1",
            "role": [],
            "iat": datetime.now(UTC),
            "exp": datetime.now(UTC) + timedelta(minutes=5),
            "iss": "harborrag",
            "aud": "harborrag-api",
        },
        SECRET,
        algorithm="HS256",
    )
    with _hmac_client() as client:
        response = client.post(PROTECTED, headers={"Authorization": f"Bearer {bad}"})
        assert response.status_code == 401
        assert response.json()["error"]["code"] == "harbor_auth_error"


@pytest.mark.blackbox
def test_prod_env_refuses_disabled_auth() -> None:
    """Fail closed: env=prod with auth_mode=none must not boot."""
    from harborrag_core.contracts.errors import HarborConfigurationError

    with pytest.raises(HarborConfigurationError):
        create_fastapi_app(
            ApiSettings(
                env="prod",
                auth_mode="none",
                api_capacity_redis_url="rediss://localhost:6379/1",
            )
        )
    with pytest.raises(HarborConfigurationError):
        create_fastapi_app(
            ApiSettings(
                env="prod",
                host="0.0.0.0",
                auth_mode="none",
                allow_insecure_dev=True,
                api_capacity_redis_url="rediss://localhost:6379/1",
            )
        )


@pytest.mark.blackbox
def test_dev_env_disabled_auth_logs_a_loud_warning(caplog: pytest.LogCaptureFixture) -> None:
    """`env` and `auth_mode` both default to permissive values, so a real
    deployment that forgets HARBORRAG_ENV=prod boots wide open with no
    signal. The prod check can't catch that misconfiguration, so booting
    with auth disabled must never be silent."""
    # The application intentionally disables propagation from the ``harborrag``
    # namespace so uvicorn's root handlers do not duplicate every record. Attach
    # pytest's capture handler to that namespace directly instead of relying on
    # test order to leave propagation enabled.
    namespace_logger = logging.getLogger("harborrag")
    namespace_logger.addHandler(caplog.handler)
    try:
        with caplog.at_level("WARNING", logger="harborrag.app.api.auth"):
            create_fastapi_app(ApiSettings())
    finally:
        namespace_logger.removeHandler(caplog.handler)

    assert any("auth" in record.message.lower() for record in caplog.records)
    assert any("HARBORRAG_AUTH_MODE=none" in record.message for record in caplog.records)


@pytest.mark.blackbox
def test_dev_defaults_to_safe_loopback_binding(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("HARBORRAG_HOST")

    settings = ApiSettings()
    app = create_fastapi_app(settings)

    assert settings.host == "127.0.0.1"
    assert app.state.token_verifier is None


@pytest.mark.blackbox
def test_disabled_auth_requires_explicit_opt_in_for_non_loopback_binding() -> None:
    from harborrag_core.contracts.errors import HarborConfigurationError

    with pytest.raises(HarborConfigurationError, match="loopback"):
        create_fastapi_app(ApiSettings(host="0.0.0.0"))

    app = create_fastapi_app(ApiSettings(host="0.0.0.0", allow_insecure_dev=True))
    assert app.state.token_verifier is None


def test_tenant_authorization_is_independent_of_global_role() -> None:
    principal = Principal(
        subject="admin-1",
        role="owner",
        tenant_ids=frozenset({"tenant-a"}),
    )

    authorize_tenant(principal, "tenant-a")
    with pytest.raises(HarborAuthError, match="tenant access"):
        authorize_tenant(principal, "tenant-b")


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
