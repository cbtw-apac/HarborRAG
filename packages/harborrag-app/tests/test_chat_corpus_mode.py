"""RAG chat reads the shared corpus on the same basis as the agent path.

Ingestion writes no ACL snapshot rows, so a tenant ingested under
``tenant_shared`` that chat searched as ``source_acl`` came back empty: every
hit was filtered out by an allow-list nothing ever populated.
"""

from __future__ import annotations

import pytest
from chat_service_fixtures import FakeChatFacade, FakeRetrievalFacade, FakeRuntime

from harborrag_app.workflow_control.chat import ChatApplicationService, ChatExecutionOptions
from harborrag_runtime.config.settings import RuntimeSettings
from harborrag_runtime.memory import ConversationIdentity, InMemoryConversationMemory


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("tenant_id", "expected"),
    [("SHARED", "tenant_shared"), ("OTHER", "source_acl")],
)
async def test_only_the_shared_tenant_searches_on_the_shared_basis(
    tenant_id: str, expected: str
) -> None:
    memory = InMemoryConversationMemory()
    await memory.create(
        ConversationIdentity(tenant_id, "reader-1", "session-1", "reader-1"), kind="chat"
    )
    retrieval = FakeRetrievalFacade()
    service = ChatApplicationService(
        lambda: FakeRuntime(FakeChatFacade(answer="ok"), retrieval),  # type: ignore[arg-type]
        RuntimeSettings(
            corpus_access_mode="tenant_shared",
            corpus_shared_tenant_id="SHARED",
        ),
        memory=memory,
    )

    await service.complete(
        "What does the release policy say?",
        tenant_id=tenant_id,
        principal_id="reader-1",
        options=ChatExecutionOptions(session_id="session-1", user_id="reader-1"),
    )

    assert retrieval.request.access.corpus_mode == expected
