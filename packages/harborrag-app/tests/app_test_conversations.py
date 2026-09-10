"""Shared harness for the conversation-directory route tests.

Two callers are modelled deliberately: a conversation is owned by the *human*
(the ``oid`` claim), while the ``sub`` is only the credential that acted, so a
second caller here differs in both. That is what makes "another user cannot
see my conversations" a real assertion rather than an artifact of the fake
keying its sessions by whichever field the test happened to vary.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, datetime, timedelta

import jwt
import pytest
from app_test_fixtures import MockAppService
from fastapi import FastAPI

from harborrag_app.api import app as api_app
from harborrag_app.api.app import create_fastapi_app
from harborrag_app.api.settings import ApiSettings
from harborrag_core.base import utc_now
from harborrag_core.ports.conversation import (
    ConversationIdentity,
    ConversationKind,
    ConversationMessage,
)

SECRET = "test-secret-at-least-32-bytes-long-for-hs256"
CONVERSATIONS = "/v1/chat/conversations"
# (credential subject, end-user identity)
ALICE = ("cred-1", "alice")
BOB = ("cred-2", "bob")


def token(
    caller: tuple[str, str] = ALICE,
    *,
    role: str = "reader",
    tenants: tuple[str, ...] = ("DEFAULT",),
) -> str:
    """A bearer token for one caller, whose ``oid`` claim owns conversations."""

    now = datetime.now(UTC)
    subject, user_id = caller
    return jwt.encode(
        {
            "sub": subject,
            "oid": user_id,
            "role": role,
            "tenants": list(tenants),
            "iat": now,
            "exp": now + timedelta(minutes=5),
            "iss": "harborrag",
            "aud": "harborrag-api",
        },
        SECRET,
        algorithm="HS256",
    )


def auth(
    caller: tuple[str, str] = ALICE,
    *,
    role: str = "reader",
    tenants: tuple[str, ...] = ("DEFAULT",),
) -> dict[str, str]:
    """Authorization header for one caller."""

    return {"Authorization": f"Bearer {token(caller, role=role, tenants=tenants)}"}


def identity(
    session_id: str,
    caller: tuple[str, str] = ALICE,
    *,
    tenant: str = "DEFAULT",
) -> ConversationIdentity:
    """The stored isolation key for one caller's conversation."""

    subject, user_id = caller
    return ConversationIdentity(tenant, subject, session_id, user_id)


def message(  # noqa: PLR0913 - one stored message field per argument
    message_id: str,
    role: str = "user",
    content: str = "where is the runbook?",
    *,
    token_count: int | None = None,
    citations_json: str | None = None,
    run_id: str | None = None,
    partial: bool = False,
) -> ConversationMessage:
    """One stored conversation message."""

    return ConversationMessage(
        message_id=message_id,
        role=role,  # type: ignore[arg-type]
        content=content,
        created_at=utc_now(),
        token_count=token_count,
        citations_json=citations_json,
        run_id=run_id,
        partial=partial,
    )


async def seed(  # noqa: PLR0913 - one stored conversation attribute per argument
    service: MockAppService,
    session_id: str,
    caller: tuple[str, str] = ALICE,
    *,
    kind: ConversationKind = "chat",
    title: str | None = None,
    messages: Sequence[ConversationMessage] = (),
    tenant: str = "DEFAULT",
) -> ConversationIdentity:
    """Create one stored conversation, optionally with messages."""

    owner = identity(session_id, caller, tenant=tenant)
    await service.conversations.create(owner, kind=kind, title=title)
    if messages:
        await service.conversations.append_messages(owner, tuple(messages))
    return owner


def hmac_app(monkeypatch: pytest.MonkeyPatch, service: MockAppService) -> FastAPI:
    """An authenticated app whose end-user identity comes from the ``oid`` claim."""

    monkeypatch.setattr(api_app, "select_app_service", lambda: (service, "test"))
    settings = ApiSettings(auth_mode="hmac", auth_secret=SECRET, auth_user_id_claim="oid")
    return create_fastapi_app(settings)


__all__ = [
    "ALICE",
    "BOB",
    "CONVERSATIONS",
    "SECRET",
    "auth",
    "hmac_app",
    "identity",
    "message",
    "seed",
    "token",
]
