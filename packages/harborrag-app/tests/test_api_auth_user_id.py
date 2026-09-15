"""User accounts are deferred; authenticated credentials share DEFAULT_USER per tenant."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import jwt
import pytest

from harborrag_app.api.auth.dependencies import build_token_verifier
from harborrag_app.api.auth.hmac import HmacTokenVerifier
from harborrag_app.api.auth.principal import Principal
from harborrag_app.api.settings import ApiSettings
from harborrag_core.domain.identity import DEFAULT_USER

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


def test_subject_is_provenance_and_user_id_is_the_shared_default() -> None:
    principal = HmacTokenVerifier(secret=SECRET).verify(_token(oid="ignored"))

    assert (principal.subject, principal.user_id) == ("u1", DEFAULT_USER)


@pytest.mark.parametrize(("claim", "value"), [("oid", "object-42"), ("email", "u1@example.com")])
def test_custom_claim_cannot_change_the_default_user(claim: str, value: str) -> None:
    principal = HmacTokenVerifier(secret=SECRET, user_id_claim=claim).verify(
        _token(**{claim: value})
    )

    assert principal.subject == "u1"
    assert principal.user_id == DEFAULT_USER


def test_missing_custom_claim_keeps_the_default_user() -> None:
    principal = HmacTokenVerifier(secret=SECRET, user_id_claim="oid").verify(_token())

    assert principal.user_id == DEFAULT_USER


@pytest.mark.parametrize("value", [42, "", None, ["a"], {"id": "x"}])
def test_unused_custom_claim_does_not_affect_authentication(value: object) -> None:
    verifier = HmacTokenVerifier(secret=SECRET, user_id_claim="oid")

    assert verifier.verify(_token(oid=value)).user_id == DEFAULT_USER


def test_legacy_claim_configuration_keeps_the_default_user(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HARBORRAG_AUTH_USER_ID_CLAIM", "email")
    settings = ApiSettings(auth_mode="hmac", auth_secret=SECRET)

    verifier = build_token_verifier(settings)

    assert isinstance(verifier, HmacTokenVerifier)
    assert verifier.user_id_claim == "email"
    assert verifier.verify(_token(email="u1@example.com")).user_id == DEFAULT_USER


def test_principal_uses_default_user_and_preserves_the_subject() -> None:
    principal = Principal(subject="harborrag-cli", role="owner", tenant_ids=frozenset({"*"}))

    assert principal.user_id == DEFAULT_USER
    assert principal.subject == "harborrag-cli"


@pytest.mark.blackbox
def test_a_session_created_under_a_custom_claim_is_usable_on_the_next_request(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Creation and completion agree on DEFAULT_USER despite legacy user claims."""

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
    assert ("DEFAULT", "u1", DEFAULT_USER, session_id, "chat") in (service.conversation_sessions)


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
    assert ("DEFAULT", "u1", DEFAULT_USER, session_id, "agent") in (service.conversation_sessions)
