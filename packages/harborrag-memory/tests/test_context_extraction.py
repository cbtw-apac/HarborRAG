from __future__ import annotations

from datetime import timedelta
from typing import Any

import pytest
from context_test_fakes import (
    NOW,
    EmbedderFake,
    MemoryIndexFake,
    MemoryRepositoryFake,
    memory_row,
    row,
    structured_model,
)

from harborrag_core.ports.conversation import ConversationMessage
from harborrag_core.ports.memory import MemoryOwner, MemoryScope, MemoryType
from harborrag_memory import MemoryExtractor, MemoryPolicy
from harborrag_memory.context import content_hash
from harborrag_memory.context.extraction import reference_token

pytestmark = [pytest.mark.unit]

MESSAGES: tuple[ConversationMessage, ...] = (
    row("user", "I always want answers in Vietnamese.", message_id="m-1"),
    row("assistant", "Understood, Vietnamese from now on.", message_id="m-2"),
)


def fact(content: str, **overrides: Any) -> dict[str, Any]:
    return {"content": content, "scope": "user", "importance": 0.8, **overrides}


def payload(*facts: dict[str, Any]) -> dict[str, Any]:
    return {"facts": list(facts)}


def extractor(
    memories: MemoryRepositoryFake,
    *payloads: Any,
    index: MemoryIndexFake | None = None,
    embedder: EmbedderFake | None = None,
    policy: MemoryPolicy | None = None,
    failure: str | None = None,
) -> MemoryExtractor:
    return MemoryExtractor(
        policy=policy or MemoryPolicy(),
        memories=memories,
        model=structured_model(*payloads, failure=failure),
        index=index,
        embedder=embedder,
        clock=lambda: NOW,
    )


@pytest.mark.asyncio
async def test_extract_drops_blank_and_low_importance_facts(
    memories: MemoryRepositoryFake, owner: MemoryOwner
) -> None:
    saved = await extractor(
        memories,
        payload(
            fact("The user prefers Vietnamese answers."),
            fact("   ", importance=0.9),
            fact("The user mentioned the weather.", importance=0.1),
        ),
    ).extract(owner, messages=MESSAGES)

    assert [memory.content for memory in saved] == ["The user prefers Vietnamese answers."]
    assert len(memories.rows) == 1


@pytest.mark.asyncio
async def test_extract_records_provenance_and_content_hash(
    memories: MemoryRepositoryFake, owner: MemoryOwner
) -> None:
    (saved,) = await extractor(
        memories,
        payload(
            fact(
                "The user prefers Vietnamese answers.",
                memory_type="preference",
                entities=["user-1", "  "],
            )
        ),
    ).extract(owner, messages=MESSAGES)

    assert saved.memory_type is MemoryType.PREFERENCE
    assert saved.scope is MemoryScope.USER
    assert saved.valid_from == NOW
    assert saved.invalid_at is None
    assert saved.source_session_id == "s-1"
    assert saved.source_message_ids == ("m-1", "m-2")
    assert saved.entity_ids == ("user-1",)
    assert saved.content_hash == content_hash(
        "The user prefers Vietnamese answers.", MemoryScope.USER
    )


@pytest.mark.asyncio
async def test_extract_is_idempotent_across_a_retry(
    memories: MemoryRepositoryFake, owner: MemoryOwner
) -> None:
    proposal = payload(fact("The user prefers Vietnamese answers."))
    subject = extractor(memories, proposal, dict(proposal))

    first = await subject.extract(owner, messages=MESSAGES)
    second = await subject.extract(owner, messages=MESSAGES)

    assert len(first) == 1
    assert second == ()
    assert len(memories.rows) == 1


@pytest.mark.asyncio
async def test_extract_suppresses_a_near_duplicate_at_the_threshold(
    memories: MemoryRepositoryFake,
    index: MemoryIndexFake,
    embedder: EmbedderFake,
    owner: MemoryOwner,
) -> None:
    stored = memory_row(
        "The user asks for Vietnamese replies.",
        scope=MemoryScope.USER,
        owner=MemoryOwner(tenant_id="tenant-1", user_id="user-1"),
        content_hash=content_hash("The user asks for Vietnamese replies.", MemoryScope.USER),
    )
    memories.rows[stored.memory_id] = stored
    index.scores = {stored.memory_id: MemoryPolicy().dedup_threshold}

    saved = await extractor(
        memories,
        payload(fact("The user prefers Vietnamese answers.")),
        index=index,
        embedder=embedder,
    ).extract(owner, messages=MESSAGES)

    assert saved == ()
    assert list(memories.rows) == [stored.memory_id]
    assert embedder.texts == ["The user prefers Vietnamese answers."]


@pytest.mark.asyncio
async def test_extract_stores_a_fact_below_the_dedup_threshold(
    memories: MemoryRepositoryFake,
    index: MemoryIndexFake,
    embedder: EmbedderFake,
    owner: MemoryOwner,
) -> None:
    stored = memory_row(
        "The user asks for Vietnamese replies.",
        scope=MemoryScope.USER,
        owner=MemoryOwner(tenant_id="tenant-1", user_id="user-1"),
    )
    memories.rows[stored.memory_id] = stored
    index.scores = {stored.memory_id: 0.5}

    saved = await extractor(
        memories,
        payload(fact("The user prefers Vietnamese answers.")),
        index=index,
        embedder=embedder,
    ).extract(owner, messages=MESSAGES)

    assert len(saved) == 1
    assert index.indexed == list(saved)
    assert index.embeddings[-1] == embedder.vector


@pytest.mark.asyncio
async def test_extract_invalidates_a_superseded_memory_without_deleting_it(
    memories: MemoryRepositoryFake, owner: MemoryOwner
) -> None:
    prior = memory_row(
        "Ravi owns ingest.",
        scope=MemoryScope.USER,
        owner=MemoryOwner(tenant_id="tenant-1", user_id="user-1"),
        updated_at=NOW - timedelta(hours=48),
        valid_from=NOW - timedelta(hours=48),
        content_hash=content_hash("Ravi owns ingest.", MemoryScope.USER),
    )
    memories.rows[prior.memory_id] = prior

    (saved,) = await extractor(
        memories,
        payload(fact("Dana owns ingest.", replaces=reference_token(prior))),
    ).extract(owner, messages=MESSAGES)

    amended = memories.rows[prior.memory_id]
    assert amended.content == "Ravi owns ingest."
    assert amended.invalid_at == NOW
    assert amended.superseded_by == saved.memory_id
    assert amended.is_valid_at(NOW) is False
    assert saved.is_valid_at(NOW) is True


@pytest.mark.asyncio
async def test_extract_ignores_an_unknown_replaces_reference(
    memories: MemoryRepositoryFake, owner: MemoryOwner
) -> None:
    saved = await extractor(
        memories,
        payload(fact("Dana owns ingest.", replaces="deadbeefcafe")),
    ).extract(owner, messages=MESSAGES)

    assert len(saved) == 1
    assert all(stored.invalid_at is None for stored in memories.rows.values())


@pytest.mark.asyncio
async def test_extract_refuses_to_supersede_a_memory_in_another_scope(
    memories: MemoryRepositoryFake, owner: MemoryOwner
) -> None:
    prior = memory_row(
        "Ravi owns ingest.",
        scope=MemoryScope.SESSION,
        owner=MemoryOwner(tenant_id="tenant-1", user_id="user-1", session_id="s-1"),
        updated_at=NOW - timedelta(hours=48),
        valid_from=NOW - timedelta(hours=48),
        content_hash=content_hash("Ravi owns ingest.", MemoryScope.SESSION),
    )
    memories.rows[prior.memory_id] = prior

    (saved,) = await extractor(
        memories,
        payload(fact("Dana owns ingest.", scope="user", replaces=reference_token(prior))),
    ).extract(owner, messages=MESSAGES)

    assert saved.scope is MemoryScope.USER
    assert memories.rows[prior.memory_id].invalid_at is None
    assert memories.rows[prior.memory_id].superseded_by is None


@pytest.mark.asyncio
async def test_extract_never_widens_the_scope_a_conversation_writes(
    memories: MemoryRepositoryFake, owner: MemoryOwner
) -> None:
    saved = await extractor(
        memories,
        payload(
            fact("The tenant standardised on Postgres.", scope="tenant"),
            fact("Ingest runs nightly.", scope="global"),
        ),
    ).extract(owner, messages=MESSAGES)

    assert [memory.scope for memory in saved] == [MemoryScope.SESSION, MemoryScope.SESSION]


@pytest.mark.asyncio
async def test_extract_degrades_a_project_fact_when_the_owner_has_no_project(
    memories: MemoryRepositoryFake,
) -> None:
    projectless = MemoryOwner(tenant_id="tenant-1", principal_id="principal-1", session_id="s-1")

    (saved,) = await extractor(
        memories,
        payload(fact("Ingest runs nightly.", scope="project")),
    ).extract(projectless, messages=MESSAGES)

    assert saved.scope is MemoryScope.SESSION
    assert saved.owner.user_id == "principal-1"


@pytest.mark.asyncio
async def test_extract_shows_existing_memories_and_forbids_following_the_conversation(
    memories: MemoryRepositoryFake, owner: MemoryOwner
) -> None:
    stored = memory_row(
        "Ravi owns ingest.",
        scope=MemoryScope.USER,
        owner=MemoryOwner(tenant_id="tenant-1", user_id="user-1"),
        content_hash=content_hash("Ravi owns ingest.", MemoryScope.USER),
    )
    memories.rows[stored.memory_id] = stored
    subject = extractor(memories, payload())
    model = subject._model  # noqa: SLF001 - the fake's recorded prompts are the assertion

    await subject.extract(owner, messages=MESSAGES)

    assert "untrusted data" in model.systems[0]
    assert "credential" in model.systems[0]
    assert f"[{reference_token(stored)}]" in model.prompts[0]
    assert "I always want answers in Vietnamese." in model.prompts[0]


@pytest.mark.asyncio
async def test_extract_swallows_a_model_failure(
    memories: MemoryRepositoryFake, owner: MemoryOwner
) -> None:
    assert await extractor(memories, failure="model down").extract(owner, messages=MESSAGES) == ()
    assert memories.rows == {}


@pytest.mark.asyncio
async def test_extract_saves_nothing_when_the_first_write_fails(
    memories: MemoryRepositoryFake, owner: MemoryOwner
) -> None:
    memories.fail_save = True

    saved = await extractor(
        memories,
        payload(fact("The user prefers Vietnamese answers.")),
    ).extract(owner, messages=MESSAGES)

    assert saved == ()


@pytest.mark.asyncio
async def test_extract_returns_the_facts_it_stored_before_a_write_failed(
    memories: MemoryRepositoryFake, owner: MemoryOwner
) -> None:
    memories.fail_save_after = 1

    saved = await extractor(
        memories,
        payload(
            fact("The user prefers Vietnamese answers."),
            fact("Dana owns ingest."),
        ),
    ).extract(owner, messages=MESSAGES)

    assert [memory.content for memory in saved] == ["The user prefers Vietnamese answers."]


@pytest.mark.asyncio
async def test_extract_does_nothing_when_memory_is_disabled_or_there_are_no_messages(
    memories: MemoryRepositoryFake, owner: MemoryOwner
) -> None:
    disabled = extractor(memories, payload(fact("x")), policy=MemoryPolicy.disabled())

    assert await disabled.extract(owner, messages=MESSAGES) == ()
    assert await extractor(memories, payload(fact("x"))).extract(owner, messages=()) == ()
    assert memories.rows == {}
