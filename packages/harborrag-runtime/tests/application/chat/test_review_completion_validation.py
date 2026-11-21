"""A completion body the transport rejects is never stored in the session's history."""

from __future__ import annotations

import pytest

from harborrag_memory.locks import SessionLocks
from harborrag_memory.tiers.in_memory import InMemoryConversationMemory
from harborrag_runtime.application.chat import ChatApplicationService
from harborrag_runtime.application.chat.client import ChatClientMixin
from harborrag_runtime.application.chat.completion_request import (
    CompletionCaller,
    CompletionRequest,
    ReplayKey,
)
from harborrag_runtime.application.memory import ConversationSessionService
from harborrag_runtime.config.settings import RuntimeSettings
from harborrag_runtime.memory import ConversationIdentity
from harborrag_runtime.testing.chat_fakes import (
    FakeChatFacade,
    FakeConfigProvider,
    FakeRetrievalFacade,
    FakeRuntime,
)
from harborrag_runtime.testing.chat_service_double import (
    ANSWER,
    PASSAGES,
    FakeConnectionCatalog,
    ProjectSets,
)

pytestmark = pytest.mark.unit

TENANT = "ACME"
READER = "reader-1"
SESSION = "ses_a"
IDENTITY = ConversationIdentity(TENANT, READER, SESSION, READER)


class _Projects:
    known_projects = {"demo"}
    tenant_projects: dict[str, set[str]] = {}


class _Client(ChatClientMixin):
    def __init__(self, chat: ChatApplicationService, memory: InMemoryConversationMemory) -> None:
        self._chat = chat
        self._sessions = ConversationSessionService(memory)


async def _client() -> tuple[_Client, InMemoryConversationMemory]:
    memory = InMemoryConversationMemory()
    await memory.create(IDENTITY, project_id="demo")
    catalog = FakeConnectionCatalog()
    catalog.add("demo", "demo/demo-tickets", ("scope-demo-tickets",), documents=4)
    runtime = FakeRuntime(FakeChatFacade(answer=ANSWER), FakeRetrievalFacade(PASSAGES))
    chat = ChatApplicationService(
        lambda: runtime,  # type: ignore[arg-type, return-value]
        RuntimeSettings(),
        memory=memory,
        locks=SessionLocks(memory),
        projects=ProjectSets(_Projects()),  # type: ignore[arg-type]
        config=FakeConfigProvider(),
        catalog=catalog,
    )
    return _Client(chat, memory), memory


def _request() -> CompletionRequest:
    return CompletionRequest(
        message="Which runbook covers rollback?",
        session_id=SESSION,
        mode="instant",
        filters=None,
        project_ids=(),
        connections=(),
        trace_id="trace-1",
    )


def _rejected(body: str) -> object:
    del body
    raise ValueError("the wire model rejects this body")


@pytest.mark.asyncio
async def test_complete_chat_request_with_a_rejected_body_leaves_no_history() -> None:
    client, memory = await _client()
    replay = ReplayKey("key-1", "hash-1")

    with pytest.raises(ValueError, match="wire model"):
        await client.complete_chat_request(
            _request(),
            caller=CompletionCaller(TENANT, READER, READER),
            replay=replay,
            validate_body=_rejected,
        )

    assert await memory.recent_messages(IDENTITY, limit=10) == ()
    claim = await memory.claim_completion(
        tenant_id=TENANT, user_id=READER, key="key-1", request_hash="hash-1"
    )
    assert claim.status == "failed"


@pytest.mark.asyncio
async def test_complete_chat_request_stores_the_body_it_validated() -> None:
    client, memory = await _client()
    validated: list[str] = []

    outcome = await client.complete_chat_request(
        _request(),
        caller=CompletionCaller(TENANT, READER, READER),
        replay=ReplayKey("key-1", "hash-1"),
        validate_body=validated.append,
    )

    assert validated == [outcome.body]
    assert [message.role for message in await memory.recent_messages(IDENTITY, limit=10)] == [
        "user",
        "assistant",
    ]
