"""A session created for a human must still be found when that human returns.

Conversations are keyed by the end user, not by the credential that carried
the request, so the identity used to create a session and the identity used
to complete on it have to be the same one. When one service credential fronts
several people -- the deployment this change exists for -- creating as the
credential and completing as the person would make every turn look like an
unknown session, which no fake that creates and completes under one identity
can catch.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from chat_service_fixtures import FakeChatFacade, FakeRetrievalFacade, FakeRuntime
from test_agent_service import _Chat
from workflow_control_fixtures import FakeComposition

from harborrag_app.workflow_control.agent import AgentExecutionOptions
from harborrag_app.workflow_control.chat import ChatExecutionOptions
from harborrag_app.workflow_control.composition.factories import AppServiceFactories
from harborrag_app.workflow_control.composition.service import AppService
from harborrag_app.workflow_control.memory import MemoryAccess

TENANT = "ACME"
PRINCIPAL = "svc-1"
USER = "alice@example.com"


def _service(runtime: object) -> AppService:
    return AppService(
        FakeComposition({"runtime": {"ready": True}}),
        factories=AppServiceFactories(
            retrieval_runtime=lambda _settings: runtime,  # type: ignore[arg-type]
        ),
    )


@pytest.mark.asyncio
async def test_a_chat_session_created_for_a_user_is_found_on_that_user_s_turn() -> None:
    service = _service(FakeRuntime(FakeChatFacade(), FakeRetrievalFacade()))

    created = await service.create_chat_session(
        tenant_id=TENANT,
        principal_id=PRINCIPAL,
        user_id=USER,
    )
    session_id = str(created.data["session_id"])

    assert await service.chat_session_exists(
        session_id, tenant_id=TENANT, principal_id=PRINCIPAL, user_id=USER
    )
    result = await service.chat_completion(
        "Hello",
        tenant_id=TENANT,
        principal_id=PRINCIPAL,
        options=ChatExecutionOptions(session_id=session_id, user_id=USER),
    )
    assert result.ok is True


@pytest.mark.asyncio
async def test_another_user_on_the_same_credential_does_not_see_that_session() -> None:
    service = _service(FakeRuntime(FakeChatFacade(), FakeRetrievalFacade()))

    created = await service.create_chat_session(
        tenant_id=TENANT,
        principal_id=PRINCIPAL,
        user_id=USER,
    )
    session_id = str(created.data["session_id"])

    assert not await service.chat_session_exists(
        session_id, tenant_id=TENANT, principal_id=PRINCIPAL, user_id="bob@example.com"
    )


@pytest.mark.asyncio
async def test_an_agent_session_created_for_a_user_is_found_on_that_user_s_run() -> None:
    service = _service(SimpleNamespace(chat=_Chat()))

    created = await service.create_agent_session(
        tenant_id=TENANT,
        principal_id=PRINCIPAL,
        user_id=USER,
    )
    session_id = str(created.data["session_id"])

    assert await service.agent_session_exists(
        session_id, tenant_id=TENANT, principal_id=PRINCIPAL, user_id=USER
    )
    result = await service.agent_completion(
        "Hello",
        tenant_id=TENANT,
        principal_id=PRINCIPAL,
        options=AgentExecutionOptions(session_id=session_id, user_id=USER),
    )
    assert result.ok is True


@pytest.mark.asyncio
async def test_an_omitted_user_id_still_falls_back_to_the_principal() -> None:
    """The CLI and ``auth_mode=none`` pass no claim; both halves must agree."""

    service = _service(FakeRuntime(FakeChatFacade(), FakeRetrievalFacade()))

    created = await service.create_chat_session(tenant_id=TENANT, principal_id=PRINCIPAL)
    session_id = str(created.data["session_id"])

    result = await service.chat_completion(
        "Hello",
        tenant_id=TENANT,
        principal_id=PRINCIPAL,
        options=ChatExecutionOptions(session_id=session_id),
    )
    assert result.ok is True


def _access(user_id: str = USER, session_id: str | None = None) -> MemoryAccess:
    return MemoryAccess(
        tenant_id=TENANT,
        principal_id=PRINCIPAL,
        user_id=user_id,
        session_id=session_id,
    )


@pytest.mark.asyncio
async def test_a_named_session_is_listed_for_its_owner_only() -> None:
    """The composed service wires the directory over the same conversations."""

    service = _service(FakeRuntime(FakeChatFacade(), FakeRetrievalFacade()))

    created = await service.create_chat_session(
        tenant_id=TENANT,
        principal_id=PRINCIPAL,
        user_id=USER,
        title="Runbook triage",
    )
    session_id = str(created.data["session_id"])

    listed = await service.list_conversations(_access())
    conversations = listed.data["conversations"]
    assert isinstance(conversations, list)
    assert [(row["session_id"], row["title"]) for row in conversations] == [
        (session_id, "Runbook triage")
    ]
    # Another person on the same credential has no conversations at all.
    other = await service.list_conversations(_access("bob@example.com"))
    assert other.data["conversations"] == []


@pytest.mark.asyncio
async def test_renaming_and_reading_a_session_go_through_the_owner_identity() -> None:
    service = _service(FakeRuntime(FakeChatFacade(), FakeRetrievalFacade()))

    created = await service.create_chat_session(
        tenant_id=TENANT,
        principal_id=PRINCIPAL,
        user_id=USER,
    )
    session_id = str(created.data["session_id"])
    await service.chat_completion(
        "Hello",
        tenant_id=TENANT,
        principal_id=PRINCIPAL,
        options=ChatExecutionOptions(session_id=session_id, user_id=USER),
    )

    renamed = await service.rename_conversation(_access(session_id=session_id), title="Greetings")
    messages = await service.conversation_messages(_access(session_id=session_id))

    assert renamed.data == {"session_id": session_id, "title": "Greetings"}
    rows = messages.data["messages"]
    assert isinstance(rows, list)
    assert [row["role"] for row in rows] == ["user", "assistant"]
