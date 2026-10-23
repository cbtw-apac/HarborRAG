"""Authenticated session and memory ownership use a signed end-user claim."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import jwt
import pytest
from app_test_fixtures import MockAppService
from fastapi.testclient import TestClient
from test_api_chat_stream import _sse_frames

from harborrag_app.api import app as api_app
from harborrag_app.api.app import create_fastapi_app
from harborrag_app.api.auth.dependencies import build_token_verifier
from harborrag_app.api.auth.hmac import HmacTokenVerifier
from harborrag_app.api.auth.principal import Principal
from harborrag_app.api.settings import ApiSettings
from harborrag_core.contracts.errors import HarborAuthError
from harborrag_core.domain.identity import DEFAULT_USER

SECRET = "test-secret-at-least-32-bytes-long-for-hs256"


def _token(**extra: object) -> str:
    now = datetime.now(UTC)
    claims: dict[str, object] = {
        "sub": "shared-service",
        "role": "reader",
        "tenants": ["DEFAULT"],
        "iat": now - timedelta(seconds=1),
        "exp": now + timedelta(minutes=5),
        "iss": "harborrag",
        "aud": "harborrag-api",
        **extra,
    }
    return jwt.encode(claims, SECRET, algorithm="HS256")


def _headers(user_id: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {_token(oid=user_id)}"}


def test_subject_is_the_default_verified_user_claim() -> None:
    principal = HmacTokenVerifier(secret=SECRET).verify(_token(oid="ignored"))

    assert (principal.subject, principal.user_id) == ("shared-service", "shared-service")


@pytest.mark.parametrize(("claim", "value"), [("oid", "object-42"), ("email", "u1@example.com")])
def test_configured_signed_claim_becomes_end_user(claim: str, value: str) -> None:
    principal = HmacTokenVerifier(secret=SECRET, user_id_claim=claim).verify(
        _token(**{claim: value})
    )

    assert principal.subject == "shared-service"
    assert principal.user_id == value


@pytest.mark.parametrize("value", [None, 42, "", "   ", ["a"], {"id": "x"}, "x" * 513])
def test_missing_or_invalid_custom_claim_is_rejected(value: object) -> None:
    verifier = HmacTokenVerifier(secret=SECRET, user_id_claim="oid")
    with pytest.raises(HarborAuthError, match="user identity"):
        verifier.verify(_token(oid=value))


def test_configured_claim_is_required() -> None:
    with pytest.raises(HarborAuthError, match="user identity"):
        HmacTokenVerifier(secret=SECRET, user_id_claim="oid").verify(_token())


def test_user_claim_setting_reaches_verifier(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HARBORRAG_AUTH_USER_ID_CLAIM", "email")
    verifier = build_token_verifier(ApiSettings(auth_mode="hmac", auth_secret=SECRET))

    assert isinstance(verifier, HmacTokenVerifier)
    assert verifier.user_id_claim == "email"
    assert verifier.verify(_token(email="u1@example.com")).user_id == "u1@example.com"


def test_direct_principals_keep_explicit_user_identity() -> None:
    direct = Principal(subject="harborrag-cli", role="owner", tenant_ids=frozenset({"*"}))
    verified = Principal(
        subject="shared-service",
        role="reader",
        tenant_ids=frozenset({"DEFAULT"}),
        user_id="alice",
    )

    assert direct.user_id == DEFAULT_USER
    assert verified.user_id == "alice"
    with pytest.raises(ValueError, match="must not be blank"):
        Principal(
            subject="shared-service", role="reader", tenant_ids=frozenset({"DEFAULT"}), user_id=""
        )


@pytest.mark.blackbox
@pytest.mark.parametrize("surface", ["chat", "agent"])
def test_missing_signed_user_claim_returns_401_before_session_creation(
    monkeypatch: pytest.MonkeyPatch,
    surface: str,
) -> None:
    service = MockAppService()
    monkeypatch.setattr(api_app, "select_app_service", lambda: (service, "test"))
    settings = ApiSettings(auth_mode="hmac", auth_secret=SECRET, auth_user_id_claim="oid")

    with TestClient(create_fastapi_app(settings)) as client:
        response = client.post(
            f"/v1/{surface}/completions",
            json={"prompt": "Explain the release"},
            headers={"Authorization": f"Bearer {_token()}"},
        )
        assert response.status_code == 401
        assert client.get(f"/v1/{surface}/sessions").status_code == 401

    assert not service.conversation_sessions


@pytest.mark.blackbox
@pytest.mark.parametrize("surface", ["chat", "agent"])
def test_completion_session_history_and_replay_are_isolated_by_user(
    monkeypatch: pytest.MonkeyPatch,
    surface: str,
) -> None:
    service = MockAppService()
    monkeypatch.setattr(api_app, "select_app_service", lambda: (service, "test"))
    settings = ApiSettings(auth_mode="hmac", auth_secret=SECRET, auth_user_id_claim="oid")
    path = f"/v1/{surface}"

    with TestClient(create_fastapi_app(settings)) as client:
        first = client.post(
            f"{path}/completions",
            json={"prompt": "Explain the release", "idempotency_key": "request-1"},
            headers=_headers("alice"),
        )
        assert first.status_code == 200
        session_id = first.json()["session_id"]
        assert session_id
        assert first.json()["session_id"] == session_id
        assert (
            "DEFAULT",
            "shared-service",
            "alice",
            session_id,
            surface,
        ) in service.conversation_sessions
        assert (
            client.get(f"{path}/sessions", headers=_headers("alice")).json()["sessions"][0][
                "session_id"
            ]
            == session_id
        )
        assert client.get(f"{path}/sessions", headers=_headers("bob")).json()["sessions"] == []

        foreign = client.post(
            f"{path}/completions",
            json={"session_id": session_id, "prompt": "Explain the release"},
            headers=_headers("bob"),
        )
        assert foreign.status_code == 404
        assert (
            client.get(
                f"{path}/sessions/{session_id}/messages", headers=_headers("bob")
            ).status_code
            == 404
        )
        assert (
            client.patch(
                f"{path}/sessions/{session_id}", json={"title": "stolen"}, headers=_headers("bob")
            ).status_code
            == 404
        )
        assert (
            client.delete(f"{path}/sessions/{session_id}", headers=_headers("bob")).status_code
            == 404
        )
        assert (
            client.get(
                f"{path}/sessions/{session_id}/messages", headers=_headers("alice")
            ).status_code
            == 200
        )
        if surface == "agent":
            assert (
                client.post(
                    "/v1/agent/runs/run-1/resume",
                    json={"session_id": session_id},
                    headers=_headers("bob"),
                ).status_code
                == 404
            )

        replay = client.post(
            f"{path}/completions",
            json={"prompt": "Explain the release", "idempotency_key": "request-1"},
            headers=_headers("alice"),
        )
        assert replay.status_code == 200
        assert replay.headers["idempotency-replayed"] == "true"
        assert replay.json()["session_id"] == session_id

        other_credential = {"Authorization": f"Bearer {_token(sub='other-service', oid='alice')}"}
        same_user = client.post(
            f"{path}/completions",
            json={"prompt": "Explain the release", "idempotency_key": "request-1"},
            headers=other_credential,
        )
        assert same_user.status_code == 200
        assert same_user.headers["idempotency-replayed"] == "true"
        assert same_user.json()["session_id"] == session_id

        bob = client.post(
            f"{path}/completions",
            json={"prompt": "Explain the release", "idempotency_key": "request-1"},
            headers=_headers("bob"),
        )
        assert bob.status_code == 200
        assert bob.headers["idempotency-replayed"] == "false"
        assert bob.json()["session_id"] != session_id


@pytest.mark.blackbox
@pytest.mark.parametrize("surface", ["chat", "agent"])
def test_stream_announces_session_id_before_completion(
    monkeypatch: pytest.MonkeyPatch,
    surface: str,
) -> None:
    service = MockAppService()
    monkeypatch.setattr(api_app, "select_app_service", lambda: (service, "test"))
    settings = ApiSettings(auth_mode="hmac", auth_secret=SECRET, auth_user_id_claim="oid")

    with TestClient(create_fastapi_app(settings)) as client:
        response = client.post(
            f"/v1/{surface}/completions",
            json={"prompt": "Explain the release", "stream": True},
            headers=_headers("alice"),
        )

    assert response.status_code == 200
    frames = _sse_frames(response.text)
    session_id = frames[0][1]["session_id"]
    assert isinstance(session_id, str) and session_id
    assert frames[0][0] == "response.started"
    assert frames[-1][0] == "response.completed"
    assert frames[-1][1]["session_id"] == session_id
    assert (
        "DEFAULT",
        "shared-service",
        "alice",
        session_id,
        surface,
    ) in service.conversation_sessions
