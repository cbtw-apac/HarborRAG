"""Development owner tokens for the Compose API (scripts/deployment/dev.sh token)."""

from __future__ import annotations

from typing import Any

import jwt
import pytest
from pydantic import SecretStr

from harborrag_app.api.auth.dev_token import DEV_TOKEN_SUBJECT, main, mint_dev_token
from harborrag_app.api.auth.hmac import HmacTokenVerifier
from harborrag_app.api.settings import ApiSettings
from harborrag_core.contracts.errors import HarborConfigurationError
from harborrag_core.domain.identity import DEFAULT_USER

pytestmark = pytest.mark.unit

SECRET = "0123456789abcdef" * 4


def _settings(**overrides: Any) -> ApiSettings:
    values: dict[str, Any] = {
        "env": "dev",
        "auth_mode": "hmac",
        "auth_secret": SecretStr(SECRET),
        "auth_user_id_claim": "sub",
        "auth_issuer": "harborrag",
        "auth_audience": "harborrag-api",
        "auth_max_token_lifetime_seconds": 3600,
    }
    values.update(overrides)
    return ApiSettings(**values)


def test_dev_token_verifies_as_an_unrestricted_owner() -> None:
    principal = HmacTokenVerifier(secret=SECRET).verify(mint_dev_token(_settings()))

    assert principal.subject == DEV_TOKEN_SUBJECT
    assert principal.role == "owner"
    assert principal.tenant_ids == frozenset({"*"})


def test_dev_token_lifetime_never_exceeds_the_verifier_ceiling() -> None:
    token = mint_dev_token(_settings(auth_max_token_lifetime_seconds=600), now=1_000)
    claims = jwt.decode(
        token,
        SECRET,
        algorithms=["HS256"],
        audience="harborrag-api",
        options={"verify_exp": False, "verify_iat": False},
    )

    assert claims["iat"] == 1_000
    assert claims["exp"] - claims["iat"] == 600


def test_dev_token_carries_a_custom_user_id_claim() -> None:
    token = mint_dev_token(_settings(auth_user_id_claim="oid"))
    principal = HmacTokenVerifier(secret=SECRET, user_id_claim="oid").verify(token)

    assert principal.user_id == DEV_TOKEN_SUBJECT


@pytest.mark.parametrize(
    "overrides",
    [
        {"env": "prod", "api_capacity_redis_url": SecretStr("rediss://cache.example.com:6380/0")},
        {"auth_mode": "none"},
        {"auth_secret": None},
    ],
)
def test_dev_token_refuses_outside_hmac_development(overrides: dict[str, Any]) -> None:
    with pytest.raises(HarborConfigurationError):
        mint_dev_token(_settings(**overrides))


def test_main_prints_one_token_and_fails_closed(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("HARBORRAG_ENV", "dev")
    monkeypatch.setenv("HARBORRAG_AUTH_MODE", "hmac")
    monkeypatch.setenv("HARBORRAG_AUTH_SECRET", SECRET)
    monkeypatch.delenv("HARBORRAG_API_CAPACITY_REDIS_URL", raising=False)

    assert main() == 0
    assert capsys.readouterr().out.strip().count(".") == 2

    monkeypatch.setenv("HARBORRAG_AUTH_MODE", "none")
    assert main() == 2
    assert "HARBORRAG_AUTH_MODE=hmac" in capsys.readouterr().err


def test_dev_identity_is_distinct_from_the_user_less_sentinel() -> None:
    principal = HmacTokenVerifier(secret=SECRET).verify(mint_dev_token(_settings()))

    assert DEV_TOKEN_SUBJECT != DEFAULT_USER
    assert principal.user_id != DEFAULT_USER


@pytest.mark.parametrize("secret", ["", "too-short"])
def test_dev_token_refuses_a_weak_secret(secret: str) -> None:
    with pytest.raises(HarborConfigurationError):
        mint_dev_token(_settings(auth_secret=SecretStr(secret)))


def test_main_never_echoes_input_values_on_invalid_settings(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("HARBORRAG_ENV", "dev")
    monkeypatch.setenv("HARBORRAG_AUTH_MODE", "hmac")
    monkeypatch.setenv("HARBORRAG_AUTH_SECRET", SECRET)
    monkeypatch.setenv("HARBORRAG_AUTH_MAX_TOKEN_LIFETIME_SECONDS", f"bad-{SECRET}")

    assert main() == 1
    captured = capsys.readouterr()
    assert SECRET not in captured.err + captured.out
    assert "auth_max_token_lifetime_seconds" in captured.err
