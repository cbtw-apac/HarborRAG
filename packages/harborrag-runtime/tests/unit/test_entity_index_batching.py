"""Publishing a large scope's entity index is a few batched requests, not one per card."""

from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta

import pytest
from test_entity_summary_search import PROFILE, Embed, Vectors, binding

from harborrag_core.models.embed import HarborEmbedding, HarborEmbedResponse
from harborrag_core.summaries import SummaryLease, SummaryPolicy
from harborrag_runtime.topology.entity_summary_index import EntitySummaryIndex, entity_point_id
from harborrag_runtime.topology.summary_service import SummaryProjectionService


class CountingEmbed(Embed):
    def __init__(self) -> None:
        super().__init__()
        self.requests = 0

    async def aembed(self, request):
        self.requests += 1
        return await super().aembed(request)


class CountingVectors(Vectors):
    def __init__(self) -> None:
        super().__init__()
        self.upserts: list[int] = []

    async def upsert_records(self, index_name, records, *, context):
        self.upserts.append(len(records))
        await super().upsert_records(index_name, records, context=context)


@pytest.mark.asyncio
async def test_cards_are_embedded_and_upserted_in_batches() -> None:
    vectors, embed = CountingVectors(), CountingEmbed()
    bindings = tuple(binding(f"node-{n}") for n in range(300))

    written = await EntitySummaryIndex(vectors, embed, PROFILE).publish(
        "DEFAULT", "scope", bindings
    )

    assert written == 300
    assert embed.requests == 5  # ceil(300 / 64), not 300
    assert vectors.upserts == [256, 44]
    assert len(vectors.records[PROFILE.entity_index_name]) == 300


class ReversedEmbed(Embed):
    """A provider that answers a batch out of order: vectors carry their input index."""

    async def aembed(self, request):
        count = len(request.inputs)
        return HarborEmbedResponse(
            embeddings=tuple(
                HarborEmbedding(index=index, value=(float(index), 1.0))
                for index in reversed(range(count))
            ),
            logical_model="embed",
            embedding_space="test",
            provider="fake",
            provider_model="embed",
            deployment="test",
            request_id="request",
        )


@pytest.mark.asyncio
async def test_each_vector_is_matched_to_its_card_by_index() -> None:
    vectors = Vectors()
    await EntitySummaryIndex(vectors, ReversedEmbed(), PROFILE).publish(
        "DEFAULT", "scope", (binding("node-0"), binding("node-1"), binding("node-2"))
    )
    stored = vectors.records[PROFILE.entity_index_name]
    for position in range(3):
        point = entity_point_id("DEFAULT", f"node-{position}", PROFILE.entity_fingerprint)
        assert stored[point].vector[0] == float(position)


class ShortEmbed(Embed):
    async def aembed(self, request):
        response = await super().aembed(request)
        return response.model_copy(update={"embeddings": response.embeddings[:-1]})


@pytest.mark.asyncio
async def test_a_batch_answered_short_is_refused() -> None:
    with pytest.raises(ValueError, match="do not match"):
        await EntitySummaryIndex(Vectors(), ShortEmbed(), PROFILE).publish(
            "DEFAULT", "scope", (binding("node-0"), binding("node-1"))
        )


class _Current:
    async def scope_current(self, lease):
        return True


class _Failing:
    async def publish(self, *args, **kwargs):
        raise RuntimeError("vector store rejected the request")


@pytest.mark.asyncio
async def test_a_failed_publication_is_logged_as_an_error(caplog) -> None:
    service = SummaryProjectionService(
        repository=_Current(),
        budget=None,
        settings=None,
        generator_factory=None,
        load_inputs=None,
        entity_index=_Failing(),
    )
    lease = SummaryLease(
        tenant_id="DEFAULT",
        source_scope_id="scope",
        revision=1,
        fence=1,
        policy=SummaryPolicy(model_fingerprint="m"),
        lease_until=datetime.now(UTC) + timedelta(minutes=5),
    )
    with caplog.at_level(logging.WARNING, logger="harborrag.runtime.topology"):
        await service._publish_entities(lease, (binding("node-a"),))
    (entry,) = [r for r in caplog.records if "Entity summary index" in r.getMessage()]
    assert entry.levelno == logging.ERROR
    assert entry.exc_info is not None
