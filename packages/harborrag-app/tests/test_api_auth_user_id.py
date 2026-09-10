"""End-user identity (``user_id``) resolution from the configured JWT claim."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import jwt
import pytest

from harborrag_app.api.auth.dependencies import build_token_verifier
from harborrag_app.api.auth.hmac import HmacTokenVerifier
from harborrag_app.api.auth.principal import Principal
from harborrag_app.api.settings import ApiSettings
from harborrag_core.contracts.errors import HarborAuthError

SECRET = "test-secret-at-least-32-bytes-long-for-hs256"


def _token(**extra: object) -> str:
    now = datetime.now(UTC)
    claims: dict[str, object] = {
        "sub": "u1",
        "role": "reader",
        "tenants": ["DEFAULT"],
        "iat": now - timedelta(seconds=1),
        "exp": now + timedelta(minutes=5),
        "iss": "harborrag",
        "aud": "harborrag-api",
        **extra,
    }
    return jwt.encode(claims, SECRET, algorithm="HS256")


def test_default_claim_uses_the_subject_as_user_id() -> None:
    principal = HmacTokenVerifier(secret=SECRET).verify(_token(oid="ignored"))

    assert (principal.subject, principal.user_id) == ("u1", "u1")


@pytest.mark.parametrize(("claim", "value"), [("oid", "object-42"), ("email", "u1@example.com")])
def test_custom_claim_sets_user_id(claim: str, value: str) -> None:
    principal = HmacTokenVerifier(secret=SECRET, user_id_claim=claim).verify(
        _token(**{claim: value})
    )

    assert principal.subject == "u1"
    assert principal.user_id == value


def test_missing_custom_claim_falls_back_to_the_subject() -> None:
    principal = HmacTokenVerifier(secret=SECRET, user_id_claim="oid").verify(_token())

    assert principal.user_id == "u1"


@pytest.mark.parametrize("value", [42, "", None, ["a"], {"id": "x"}])
def test_invalid_custom_claim_type_is_rejected(value: object) -> None:
    verifier = HmacTokenVerifier(secret=SECRET, user_id_claim="oid")

    with pytest.raises(HarborAuthError, match="'oid' claim"):
        verifier.verify(_token(oid=value))


def test_settings_wire_the_user_id_claim_into_the_verifier(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HARBORRAG_AUTH_USER_ID_CLAIM", "email")
    settings = ApiSettings(auth_mode="hmac", auth_secret=SECRET)

    verifier = build_token_verifier(settings)

    assert isinstance(verifier, HmacTokenVerifier)
    assert verifier.user_id_claim == "email"
    assert verifier.verify(_token(email="u1@example.com")).user_id == "u1@example.com"


def test_principal_user_id_defaults_to_the_subject() -> None:
    principal = Principal(subject="harborrag-cli", role="owner", tenant_ids=frozenset({"*"}))

    assert principal.user_id == "harborrag-cli"


@pytest.mark.blackbox
def test_a_session_created_under_a_custom_claim_is_usable_on_the_next_request(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The routes must hand the same end-user identity to create and to complete.

    With ``oid`` as the claim, the principal (``sub``) and the user (``oid``)
    differ. A route that creates the session as the credential but completes
    as the human would 404 on every turn, which is exactly the deployment
    this ownership change exists for.
    """

    from app_test_fixtures import MockAppService
    from fastapi.testclient import TestClient

    from harborrag_app.api import app as api_app
    from harborrag_app.api.app import create_fastapi_app

    service = MockAppService()
    monkeypatch.setenv("HARBORRAG_AUTH_USER_ID_CLAIM", "oid")
    monkeypatch.setattr(api_app, "select_app_service", lambda: (service, "test"))
    settings = ApiSettings(auth_mode="hmac", auth_secret=SECRET)
    headers = {"Authorization": f"Bearer {_token(oid='alice@example.com')}"}

    with TestClient(create_fastapi_app(settings)) as client:
        created = client.post("/v1/chat/sessions", json={"tenant": "DEFAULT"}, headers=headers)
        assert created.status_code == 201
        session_id = created.json()["session_id"]
        completion = client.post(
            "/v1/chat/completions",
            json={"tenant": "DEFAULT", "session_id": session_id, "prompt": "Hello", "stream": True},
            headers=headers,
        )

    assert completion.status_code == 200
    assert ("DEFAULT", "u1", "alice@example.com", session_id, "chat") in (
        service.conversation_sessions
    )


@pytest.mark.blackbox
def test_an_agent_session_created_under_a_custom_claim_is_usable_too(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app_test_fixtures import MockAppService
    from fastapi.testclient import TestClient

    from harborrag_app.api import app as api_app
    from harborrag_app.api.app import create_fastapi_app

    service = MockAppService()
    monkeypatch.setenv("HARBORRAG_AUTH_USER_ID_CLAIM", "oid")
    monkeypatch.setattr(api_app, "select_app_service", lambda: (service, "test"))
    settings = ApiSettings(auth_mode="hmac", auth_secret=SECRET)
    headers = {"Authorization": f"Bearer {_token(oid='alice@example.com')}"}

    with TestClient(create_fastapi_app(settings)) as client:
        created = client.post("/v1/agent/sessions", json={"tenant": "DEFAULT"}, headers=headers)
        assert created.status_code == 201
        session_id = created.json()["session_id"]
        completion = client.post(
            "/v1/agent/completions",
            json={"tenant": "DEFAULT", "session_id": session_id, "prompt": "Hello", "stream": True},
            headers=headers,
        )

    assert completion.status_code == 200
    assert ("DEFAULT", "u1", "alice@example.com", session_id, "agent") in (
        service.conversation_sessions
    )
