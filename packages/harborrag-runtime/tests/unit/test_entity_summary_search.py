"""An entity card is a routing key, and the authority still decides what it serves."""

from datetime import UTC, datetime

import pytest

from harborrag_core.indexing import VectorIndexRecord, VectorSearchResult
from harborrag_core.ingestion import DocumentIdentityBuilder
from harborrag_core.models.embed import HarborEmbedding, HarborEmbedResponse
from harborrag_core.schemas.vector import VectorIndexScanPage
from harborrag_core.storage import StorageOperationContext
from harborrag_core.summaries import (
    SummaryAttribute,
    SummaryBinding,
    SummaryCard,
    SummaryManifest,
)
from harborrag_core.topology.derived import ContextualIndexProfile
from harborrag_runtime.retrieval.entity_summary import EntitySummarySearch
from harborrag_runtime.topology.entity_summary_index import (
    EntitySummaryIndex,
    entity_embedding_text,
    entity_point_id,
)

PROFILE = ContextualIndexProfile(model="embed", dimension=2, deployment_revision="r1")
CONTEXT = StorageOperationContext.system("DEFAULT")


class Embed:
    def __init__(self):
        self.texts: list[str] = []

    async def aembed(self, request):
        self.texts.extend(request.inputs)
        return HarborEmbedResponse(
            embeddings=tuple(
                HarborEmbedding(index=index, value=(1.0, 0.0))
                for index, _ in enumerate(request.inputs)
            ),
            logical_model="embed",
            embedding_space="test",
            provider="fake",
            provider_model="embed",
            deployment="test",
            request_id="request",
        )


class Vectors:
    """Just enough of the vector port to observe writes, prunes and searches."""

    def __init__(self, records=None, hits=()):
        self.records: dict[str, dict[str, VectorIndexRecord]] = records or {}
        self.hits = hits
        self.deleted: list[str] = []
        self.specs: list[str] = []

    async def index_exists(self, name, *, context):
        return name in self.records

    async def ensure_index(self, spec, *, context):
        self.specs.append(spec.index_name)
        self.records.setdefault(spec.index_name, {})

    async def upsert_records(self, index_name, records, *, context):
        self.records.setdefault(index_name, {}).update({row.id: row for row in records})

    async def get_records(self, index_name, ids, *, context):
        return [
            self.records.get(index_name, {})[key]
            for key in ids
            if key in self.records.get(index_name, {})
        ]

    async def delete_records(self, index_name, ids, *, context):
        self.deleted.extend(ids)
        for key in ids:
            self.records.get(index_name, {}).pop(key, None)

    async def scan_records(self, index_name, *, limit, cursor, filters=None, context):
        rows = list(self.records.get(index_name, {}).values())
        if filters is not None:
            wanted = {condition.value for condition in filters.must}
            rows = [row for row in rows if row.payload.get("source_scope_id") in wanted]
        return VectorIndexScanPage(records=rows, next_cursor=None)

    async def search(self, query, *, context):
        return list(self.hits)


def binding(node_key: str, chunks=("chunk-1",)) -> SummaryBinding:
    card = SummaryCard(
        description="Senior Java engineer, banking background, cleared round two.",
        topics=("hiring",),
        attributes=(SummaryAttribute(name="stage", values=("round 2",)),),
    )
    return SummaryBinding(
        manifest=SummaryManifest(
            node_key=node_key,
            kind="SourceEntity",
            source_scope_id="scope",
            input_chunk_ids=chunks,
            policy_fingerprint="policy",
            membership_digest="members",
            input_digest="inputs",
        ),
        card=card,
        generation_key="generation",
        artifact_hash=card.artifact_hash,
        revision=1,
        updated_at=datetime.now(UTC),
    )


@pytest.mark.asyncio
async def test_publication_is_idempotent_and_removes_entities_the_scope_lost():
    vectors, embed = Vectors(), Embed()
    index = EntitySummaryIndex(vectors, embed, PROFILE)

    written = await index.publish("DEFAULT", "scope", (binding("node-a"), binding("node-b")))
    assert written == 2
    point = entity_point_id("DEFAULT", "node-a", PROFILE.entity_fingerprint)
    stored = vectors.records[PROFILE.entity_index_name][point]
    assert stored.payload["record_kind"] == "entity_summary"
    assert stored.payload["node_key"] == "node-a"
    assert stored.payload["attribute_names"] == ["stage"]
    # The dossier itself belongs to the authority, never to the index.
    assert "description" not in stored.payload

    await index.publish("DEFAULT", "scope", (binding("node-a"),))
    assert entity_point_id("DEFAULT", "node-b", PROFILE.entity_fingerprint) in vectors.deleted
    assert set(vectors.records[PROFILE.entity_index_name]) == {point}


@pytest.mark.asyncio
async def test_another_scopes_points_survive_this_scopes_prune():
    vectors, embed = Vectors(), Embed()
    index = EntitySummaryIndex(vectors, embed, PROFILE)
    await index.publish("DEFAULT", "other", (binding("node-other"),))
    await index.publish("DEFAULT", "scope", (binding("node-a"),))
    assert vectors.deleted == []
    assert len(vectors.records[PROFILE.entity_index_name]) == 2


def test_the_embedded_text_repeats_the_facets_a_query_would_name():
    text = entity_embedding_text(binding("node-a"))
    assert "cleared round two" in text
    assert "stage: round 2" in text


class Summaries:
    def __init__(self, evidence):
        self.evidence = evidence
        self.asked: list[tuple[str, ...]] = []

    async def entity_evidence(self, tenant_id, node_keys, *, access):
        self.asked.append(node_keys)
        return self.evidence

    async def views(self, *args, **kwargs):  # pragma: no cover - unused here
        return {}


def evidence_record(chunk: str) -> VectorIndexRecord:
    return VectorIndexRecord(
        id=DocumentIdentityBuilder().point_id(chunk_id=chunk),
        tenant_id="DEFAULT",
        vector=[1.0, 0.0],
        payload={
            "chunk_id": chunk,
            "record_kind": "evidence",
            "document_id": "doc-1",
            "document_version_id": "version-1",
            "content": "Cleared the second interview round.",
        },
    )


def entity_hit(node_key: str) -> VectorSearchResult:
    point = entity_point_id("DEFAULT", node_key, PROFILE.entity_fingerprint)
    return VectorSearchResult(
        id=point,
        score=0.9,
        raw_score=0.9,
        payload={
            "record_kind": "entity_summary",
            "projection_point_id": point,
            "node_key": node_key,
            "source_scope_id": "scope",
        },
    )


@pytest.mark.asyncio
async def test_an_entity_hit_contributes_the_chunks_the_authority_released():
    chunk = "chunk-1"
    vectors = Vectors(
        records={
            PROFILE.entity_index_name: {},
            "evidence": {evidence_record(chunk).id: evidence_record(chunk)},
        },
        hits=(entity_hit("node-a"),),
    )
    summaries = Summaries({"node-a": (chunk,)})
    search = EntitySummarySearch(summaries, vectors, PROFILE)
    fused = await search.expand((), (1.0, 0.0), context=CONTEXT)
    assert [str(item.payload["chunk_id"]) for item in fused] == [chunk]
    assert summaries.asked == [("node-a",)]


@pytest.mark.asyncio
async def test_a_binding_the_caller_may_not_read_contributes_nothing():
    chunk = "chunk-1"
    vectors = Vectors(
        records={
            PROFILE.entity_index_name: {},
            "evidence": {evidence_record(chunk).id: evidence_record(chunk)},
        },
        hits=(entity_hit("node-a"),),
    )
    # The vector index still matched; the authority declined to release evidence.
    search = EntitySummarySearch(Summaries({}), vectors, PROFILE)
    assert await search.expand((), (1.0, 0.0), context=CONTEXT) == ()


@pytest.mark.asyncio
async def test_a_missing_index_or_absent_vector_degrades_to_plain_chunk_retrieval():
    existing = (VectorSearchResult(id="a", score=1.0, raw_score=1.0, payload={"chunk_id": "c"}),)
    search = EntitySummarySearch(Summaries({}), Vectors(), PROFILE)
    assert await search.expand(existing, None, context=CONTEXT) == existing
    assert await search.expand(existing, (1.0, 0.0), context=CONTEXT) == existing
    # A vector of the wrong width is a profile mismatch, not a reason to fail.
    assert await search.expand(existing, (1.0, 0.0, 0.0), context=CONTEXT) == existing


def test_the_entity_index_is_its_own_collection():
    assert PROFILE.entity_index_name.startswith("entity-v2-")
    assert PROFILE.entity_index_name != PROFILE.parent_index_name
    assert PROFILE.entity_fingerprint != PROFILE.parent_fingerprint
