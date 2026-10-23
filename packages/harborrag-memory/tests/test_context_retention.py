"""Retention becomes ``expires_at`` on every memory the library writes (R5-02)."""

from __future__ import annotations

from datetime import timedelta

import pytest
from context_test_fakes import (
    NOW,
    MemoryRepositoryFake,
    MessageStoreFake,
    chat_model,
    row,
    structured_model,
)

from harborrag_core.ports.conversation import ConversationIdentity
from harborrag_core.ports.memory import Memory, MemoryOwner, MemoryScope, MemoryType
from harborrag_memory import (
    MemoryContextBuilder,
    MemoryExtractor,
    MemoryPolicy,
    MemoryRetention,
)
from harborrag_memory.errors import MemoryConfigurationError
from harborrag_memory.tools.memory_tools import AgentMemoryTools

pytestmark = [pytest.mark.unit]

RETENTION = MemoryRetention(session_days=90, user_days=365, project_days=0)

MESSAGES = (
    row("user", "I always want answers in Vietnamese.", message_id="m-1"),
    row("assistant", "Understood, Vietnamese from now on.", message_id="m-2"),
)


async def extract_one(policy: MemoryPolicy, owner: MemoryOwner, *, scope: str = "user") -> Memory:
    proposed = {"content": "The user prefers Vietnamese.", "scope": scope, "importance": 0.8}
    (saved,) = await MemoryExtractor(
        policy=policy,
        memories=MemoryRepositoryFake(),
        model=structured_model({"facts": [proposed]}),
        clock=lambda: NOW,
    ).extract(owner, messages=MESSAGES)
    return saved


@pytest.mark.asyncio
async def test_an_extracted_user_memory_expires_after_the_user_retention(
    owner: MemoryOwner,
) -> None:
    saved = await extract_one(MemoryPolicy(retention=RETENTION), owner)

    assert saved.scope is MemoryScope.USER
    assert saved.expires_at == NOW + timedelta(days=365)


@pytest.mark.asyncio
async def test_zero_retention_days_means_the_memory_never_expires(owner: MemoryOwner) -> None:
    saved = await extract_one(MemoryPolicy(retention=MemoryRetention(user_days=0)), owner)

    assert saved.expires_at is None


@pytest.mark.asyncio
async def test_retention_follows_the_scope_the_fact_is_stored_at(owner: MemoryOwner) -> None:
    # A project fact degrades to the session by default, so it takes the
    # session's retention, not the project's.
    saved = await extract_one(MemoryPolicy(retention=RETENTION), owner, scope="project")

    assert saved.scope is MemoryScope.SESSION
    assert saved.expires_at == NOW + timedelta(days=90)


@pytest.mark.asyncio
async def test_the_default_policy_expires_nothing(owner: MemoryOwner) -> None:
    assert (await extract_one(MemoryPolicy(), owner)).expires_at is None


@pytest.mark.asyncio
async def test_the_rolling_summary_expires_after_the_session_retention(
    owner: MemoryOwner, identity: ConversationIdentity
) -> None:
    store = MessageStoreFake()
    memories = MemoryRepositoryFake()
    await store.append_messages(
        identity,
        [row("user" if i % 2 == 0 else "assistant", f"turn-{i}", token_count=20) for i in range(6)],
    )
    policy = MemoryPolicy(
        recent_max_messages=10,
        recent_max_tokens=100,
        summary_keep_messages=2,
        query_rewrite=False,
        retention=RETENTION,
    )

    await MemoryContextBuilder(
        policy=policy,
        messages=store,
        memories=memories,
        model=chat_model("Dana owns ingestion."),
        clock=lambda: NOW,
    ).build(owner, "what now?")

    (summary,) = memories.saved
    assert summary.memory_type is MemoryType.SUMMARY
    assert summary.expires_at == NOW + timedelta(days=90)


@pytest.mark.asyncio
async def test_an_agent_recorded_memory_carries_the_scope_retention(owner: MemoryOwner) -> None:
    memories = MemoryRepositoryFake()
    tools = AgentMemoryTools(
        owner=owner, memories=memories, policy=MemoryPolicy(retention=RETENTION)
    )

    result = await tools.manage({"content": "Dana owns ingest.", "scope": "user"})

    stored = memories.rows[str(result["memory_id"])]
    assert stored.expires_at is not None
    assert stored.expires_at - stored.created_at == timedelta(days=365)


@pytest.mark.asyncio
async def test_an_agent_recorded_project_memory_never_expires_with_zero_days(
    owner: MemoryOwner,
) -> None:
    memories = MemoryRepositoryFake()
    tools = AgentMemoryTools(
        owner=owner,
        memories=memories,
        policy=MemoryPolicy(retention=RETENTION, extraction_project_scope=True),
    )

    result = await tools.manage({"content": "Ingest runs nightly.", "scope": "project"})

    assert memories.rows[str(result["memory_id"])].expires_at is None


def test_retention_rejects_negative_days() -> None:
    with pytest.raises(MemoryConfigurationError):
        MemoryRetention(user_days=-1)


def test_run_memories_follow_the_session_and_tenant_memories_never_expire() -> None:
    assert RETENTION.days(MemoryScope.RUN) == 90
    assert RETENTION.expires_at(MemoryScope.TENANT, NOW) is None
