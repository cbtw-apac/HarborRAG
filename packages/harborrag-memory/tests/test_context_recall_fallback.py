"""Repository fallback recall against a fake that filters text like SQL ``icontains``."""

from __future__ import annotations

from dataclasses import dataclass

import pytest
from context_test_fakes import NOW, MemoryRepositoryFake, memory_row

from harborrag_core.ports.memory import Memory, MemoryOwner, MemoryQuery, MemoryScope
from harborrag_memory import MemoryPolicy, MemoryRecall
from harborrag_memory.context.recall import fallback_terms

pytestmark = [pytest.mark.unit]


@dataclass
class SubstringRepositoryFake(MemoryRepositoryFake):
    """Apply ``query.text`` as a case-insensitive substring, as SQL does."""

    async def search(self, query: MemoryQuery) -> tuple[Memory, ...]:
        found = await super().search(query)
        if not query.text:
            return found
        needle = query.text.casefold()
        return tuple(memory for memory in found if needle in memory.content.casefold())


def recall(memories: MemoryRepositoryFake) -> MemoryRecall:
    return MemoryRecall(policy=MemoryPolicy(), memories=memories, clock=lambda: NOW)


def seed(memories: MemoryRepositoryFake, *rows: Memory) -> None:
    for stored in rows:
        memories.rows[stored.memory_id] = stored


@pytest.mark.asyncio
async def test_a_natural_question_recalls_the_fact_it_asks_about(owner: MemoryOwner) -> None:
    """R1g-02: the whole question never appears inside a stored fact."""

    memories = SubstringRepositoryFake()
    seed(memories, memory_row("Dana Lee owns the ingest pipeline", scope=MemoryScope.USER))

    recalled = await recall(memories).recall(owner, "Who owns the ingest pipeline?")

    assert [memory.content for memory in recalled] == ["Dana Lee owns the ingest pipeline"]


@pytest.mark.asyncio
async def test_a_fact_matching_more_terms_outranks_a_more_important_partial_match(
    owner: MemoryOwner,
) -> None:
    memories = SubstringRepositoryFake()
    seed(
        memories,
        memory_row("Ravi owns search", scope=MemoryScope.USER, importance=0.9),
        memory_row("Dana owns the ingest pipeline", scope=MemoryScope.USER, importance=0.5),
        memory_row("The cafeteria opens at nine", scope=MemoryScope.USER, importance=1.0),
    )

    recalled = await recall(memories).recall(owner, "Who owns the ingest pipeline?")

    assert [memory.content for memory in recalled] == [
        "Dana owns the ingest pipeline",
        "Ravi owns search",
    ]


@pytest.mark.asyncio
async def test_a_question_of_only_stopwords_keeps_the_whole_text_search(
    owner: MemoryOwner,
) -> None:
    memories = SubstringRepositoryFake()
    seed(memories, memory_row("who is it", scope=MemoryScope.USER))

    recalled = await recall(memories).recall(owner, "who is it")

    assert [memory.content for memory in recalled] == ["who is it"]
    assert {query.text for query in memories.queries} == {"who is it"}


def test_fallback_terms_drop_function_words_and_cap_the_search_count() -> None:
    assert fallback_terms("Who owns the ingest pipeline?") == ("pipeline", "ingest", "owns")
    many = "alpha bravo charlie delta echoes foxtrot golfing hotel"
    assert len(fallback_terms(many)) == 6


@dataclass
class FlakyTermRepositoryFake(SubstringRepositoryFake):
    """Fail the search for one term; every other term still answers."""

    failing_term: str = "pipeline"

    async def search(self, query: MemoryQuery) -> tuple[Memory, ...]:
        if query.text == self.failing_term:
            raise RuntimeError("search unavailable")
        return await super().search(query)


@pytest.mark.asyncio
async def test_one_failing_term_search_keeps_the_other_terms_hits(owner: MemoryOwner) -> None:
    """Review #10: a partial failure no longer discards what the other terms found."""

    memories = FlakyTermRepositoryFake()
    seed(memories, memory_row("Dana Lee owns the ingest pipeline", scope=MemoryScope.USER))

    recalled = await recall(memories).recall(owner, "Who owns the ingest pipeline?")

    assert [memory.content for memory in recalled] == ["Dana Lee owns the ingest pipeline"]


@pytest.mark.asyncio
async def test_a_scope_whose_every_term_search_fails_yields_nothing(owner: MemoryOwner) -> None:
    memories = SubstringRepositoryFake()
    memories.fail_search = True
    seed(memories, memory_row("Dana Lee owns the ingest pipeline", scope=MemoryScope.USER))

    assert await recall(memories).recall(owner, "Who owns the ingest pipeline?") == ()
