"""Extraction stays correct when indexing fails after the canonical save (R1g-06)."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import timedelta

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

from harborrag_core.ports.memory import Memory, MemoryOwner, MemoryScope
from harborrag_memory import MemoryExtractor, MemoryPolicy, MemoryRecall
from harborrag_memory.context import content_hash
from harborrag_memory.context.extraction import INDEX_PENDING_KEY, reference_token

pytestmark = [pytest.mark.unit]

MESSAGES = (
    row("user", "Actually Bob owns ingest now", message_id="m-1"),
    row("assistant", "Noted: Bob owns ingest.", message_id="m-2"),
)


@dataclass
class FlakyEmbedder:
    """Succeed for the first ``ok`` calls, then raise like a provider 5xx."""

    ok: int
    calls: int = 0

    async def __call__(self, text: str) -> Sequence[float]:
        self.calls += 1
        if self.calls > self.ok:
            raise RuntimeError("embed provider 503")
        return (0.5, 0.25)


@dataclass
class FailingWriteIndexFake(MemoryIndexFake):
    """An index whose writes fail while ``fail_write`` is set; reads still work."""

    fail_write: bool = True

    async def index_memory(
        self, memory: Memory, *, embedding: Sequence[float] | None = None
    ) -> None:
        if self.fail_write:
            raise RuntimeError("index write unavailable")
        await super().index_memory(memory, embedding=embedding)


def seed_dana(memories: MemoryRepositoryFake, index: MemoryIndexFake) -> Memory:
    prior = memory_row(
        "Dana owns ingest.",
        scope=MemoryScope.USER,
        updated_at=NOW - timedelta(hours=48),
        valid_from=NOW - timedelta(hours=48),
        content_hash=content_hash("Dana owns ingest.", MemoryScope.USER),
    )
    memories.rows[prior.memory_id] = prior
    index.scores[prior.memory_id] = 0.5
    return prior


def proposal(prior: Memory) -> dict[str, object]:
    return {
        "facts": [
            {
                "content": "Bob owns ingest.",
                "scope": "user",
                "importance": 0.8,
                "replaces": reference_token(prior),
            }
        ]
    }


def extractor(
    memories: MemoryRepositoryFake,
    index: MemoryIndexFake,
    embedder: object,
    prior: Memory,
) -> MemoryExtractor:
    return MemoryExtractor(
        policy=MemoryPolicy(),
        memories=memories,
        model=structured_model(proposal(prior)),
        index=index,
        embedder=embedder,  # type: ignore[arg-type]
        clock=lambda: NOW,
    )


def bob(memories: MemoryRepositoryFake) -> Memory:
    (found,) = [memory for memory in memories.rows.values() if memory.content == "Bob owns ingest."]
    return found


@pytest.mark.asyncio
async def test_a_failed_index_write_still_supersedes_and_a_retry_reindexes(
    owner: MemoryOwner,
) -> None:
    memories = MemoryRepositoryFake()
    index = MemoryIndexFake(memories)
    prior = seed_dana(memories, index)

    # Call 1 embeds for the restatement check; call 2, for the index write, fails.
    await extractor(memories, index, FlakyEmbedder(ok=1), prior).extract(owner, messages=MESSAGES)

    assert bob(memories).memory_id not in index.scores
    assert memories.rows[prior.memory_id].superseded_by == bob(memories).memory_id

    await extractor(memories, index, EmbedderFake(), prior).extract(owner, messages=MESSAGES)

    assert bob(memories).memory_id in index.scores
    assert len([m for m in memories.rows.values() if m.content == "Bob owns ingest."]) == 1
    recalled = await MemoryRecall(
        policy=MemoryPolicy(),
        memories=memories,
        index=index,
        embedder=EmbedderFake(),
        clock=lambda: NOW,
    ).recall(owner, "Who owns ingest?")
    assert [memory.content for memory in recalled] == ["Bob owns ingest."]


@pytest.mark.asyncio
async def test_a_retry_after_an_index_outage_completes_a_supersession_it_missed(
    owner: MemoryOwner,
) -> None:
    memories = MemoryRepositoryFake()
    index = FailingWriteIndexFake(memories)
    prior = seed_dana(memories, index)
    # Simulate the pre-fix outcome: the replacement row is durable but
    # unindexed, and the contradicted fact is still valid.
    stored = memory_row(
        "Bob owns ingest.",
        scope=MemoryScope.USER,
        valid_from=NOW - timedelta(hours=1),
        content_hash=content_hash("Bob owns ingest.", MemoryScope.USER),
        metadata={INDEX_PENDING_KEY: True},
    )
    memories.rows[stored.memory_id] = stored
    index.fail_write = False

    saved = await extractor(memories, index, EmbedderFake(), prior).extract(
        owner, messages=MESSAGES
    )

    assert saved == ()
    assert [memory.memory_id for memory in index.indexed] == [stored.memory_id]
    assert memories.rows[prior.memory_id].superseded_by == stored.memory_id


@pytest.mark.asyncio
async def test_a_failing_index_write_never_blocks_supersession(owner: MemoryOwner) -> None:
    memories = MemoryRepositoryFake()
    index = FailingWriteIndexFake(memories)
    prior = seed_dana(memories, index)

    await extractor(memories, index, EmbedderFake(), prior).extract(owner, messages=MESSAGES)

    assert memories.rows[prior.memory_id].invalid_at == NOW
    assert memories.rows[prior.memory_id].superseded_by == bob(memories).memory_id


@pytest.mark.asyncio
async def test_an_already_indexed_duplicate_is_not_re_embedded(owner: MemoryOwner) -> None:
    """Review #8: only a row known to be unindexed is repaired."""

    memories = MemoryRepositoryFake()
    index = MemoryIndexFake(memories)
    prior = seed_dana(memories, index)
    embedder = EmbedderFake()
    await extractor(memories, index, embedder, prior).extract(owner, messages=MESSAGES)
    indexed_once = list(index.indexed)
    embedded_once = list(embedder.texts)

    await extractor(memories, index, embedder, prior).extract(owner, messages=MESSAGES)

    assert index.indexed == indexed_once
    assert embedder.texts == embedded_once


@pytest.mark.asyncio
async def test_a_row_saved_before_its_index_write_is_marked_until_indexed(
    owner: MemoryOwner,
) -> None:
    memories = MemoryRepositoryFake()
    index = FailingWriteIndexFake(memories)
    prior = seed_dana(memories, index)

    await extractor(memories, index, EmbedderFake(), prior).extract(owner, messages=MESSAGES)

    assert bob(memories).metadata.get(INDEX_PENDING_KEY) is True
    index.fail_write = False
    await extractor(memories, index, EmbedderFake(), prior).extract(owner, messages=MESSAGES)

    assert INDEX_PENDING_KEY not in bob(memories).metadata
    assert [memory.memory_id for memory in index.indexed] == [bob(memories).memory_id]
