"""Independent generated-view recovery, conservative budgets and raw preservation."""

from dataclasses import replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from types import SimpleNamespace

import pytest

from harborrag_adapters.repositories.object_store import (
    ImmutableArtifact,
    ImmutableArtifactReader,
    ImmutableArtifactWriter,
    MemoryObjectStore,
)
from harborrag_core.ingestion import ArtifactReference
from harborrag_core.storage import StorageOperationContext
from harborrag_core.topology import (
    ChunkExtractionInput,
    DocumentTopologyBuild,
    ExtractionProfile,
    TopologyJob,
    TopologyPolicy,
)
from harborrag_core.topology.budget import BudgetAdmission, BudgetReservation
from harborrag_core.topology.derived import (
    ChunkEnrichment,
    ContextualIndexProfile,
    DescriptionOutput,
    DescriptionPacket,
)
from harborrag_runtime.topology.contextual import ContextualMaterializer, ContextualResources
from harborrag_runtime.topology.derived_models import (
    DerivedBudget,
    DescriptionArtifacts,
    FrozenDescriptionGenerator,
)
from harborrag_runtime.topology.derived_projection import DerivedVectorProjection


class Embedder:
    def __init__(self):
        self.calls = []

    async def aembed(self, request):
        self.calls.append(request)
        return SimpleNamespace(embeddings=(SimpleNamespace(value=(0.5, 0.25)),))


class Vectors:
    def __init__(self):
        self.records = {"raw-evidence": {"raw": "untouched"}}
        self.corrupt = False

    async def ensure_index(self, spec, *, context):
        self.records.setdefault(spec.index_name, {})

    async def upsert_records(self, index, records, *, context):
        self.records[index].update({row.id: row for row in records})

    async def get_records(self, index, ids, *, context):
        return [] if self.corrupt else [self.records[index][key] for key in ids]


class Budget:
    def __init__(self):
        self.requests = []
        self.settlements = []
        self.admitted = True

    async def reserve_for_build(self, tenant, build, request):
        self.requests.append(request)
        return BudgetAdmission(
            admitted=self.admitted,
            reason=None if self.admitted else "spending_paused",
            reservation=BudgetReservation(
                reservation_id=request.reservation_id,
                tenant_id=tenant,
                job_id=build,
                fence=1,
                reserved_tokens=request.input_tokens + request.output_tokens,
                reserved_cost_usd=request.cost_usd,
                expires_at=datetime.now(UTC) + timedelta(seconds=30),
            )
            if self.admitted
            else None,
        )

    async def settle_budget(self, tenant, reservation, usage):
        self.settlements.append((reservation, usage))


class Descriptions:
    def __init__(self):
        self.calls = 0

    async def generate(self, packets):
        self.calls += 1
        return DescriptionOutput(
            description="Conditional deployment.",
            cited_packet_ids=(packets[0].packet_id,),
            complete=True,
        )


class RacingWriter:
    """Freeze another valid LLM result immediately before this writer."""

    def __init__(self, delegate: ImmutableArtifactWriter, winner: DescriptionOutput) -> None:
        self.delegate = delegate
        self.winner = winner

    async def put(
        self,
        artifact: ImmutableArtifact,
        *,
        context: StorageOperationContext,
    ):
        await self.delegate.put(
            replace(artifact, payload=self.winner.model_dump_json().encode()),
            context=context,
        )
        return await self.delegate.put(artifact, context=context)


def job():
    policy = TopologyPolicy(
        tenant_id="tenant",
        source_scope_id="scope",
        enabled=True,
        profile=ExtractionProfile(model="model", deployment_revision="r", prompt_digest="p"),
    )
    return TopologyJob(
        job_id="job",
        tenant_id="tenant",
        document_id="doc",
        document_version_id="version",
        source_scope_id="scope",
        policy=policy,
        policy_revision=1,
        state="accepted",
        attempts=1,
        fence=1,
    )


def build():
    return DocumentTopologyBuild(
        build_id="build",
        job_id="job",
        document_id="doc",
        document_version_id="version",
        source_scope_id="scope",
        chunk_ids=("chunk",),
        projection_revision="semantic-v2",
        artifact=ArtifactReference(
            bucket="artifacts",
            key="build",
            media_type="application/json",
            sha256="a" * 64,
            byte_size=1,
        ),
    )


@pytest.mark.asyncio
async def test_contextual_freezes_once_and_repairs_without_reembedding_or_raw_mutation():
    store = MemoryObjectStore()
    await store.connect()
    reader, writer = ImmutableArtifactReader(store), ImmutableArtifactWriter(store)
    embed = Embedder()
    profile = ContextualIndexProfile(model="model", dimension=2, deployment_revision="r")
    materializer = ContextualMaterializer(ContextualResources(embed, writer, reader), profile)
    inputs = (ChunkExtractionInput(chunk_id="chunk", content="Original evidence."),)
    outputs = {"chunk": ChunkEnrichment(chunk_id="chunk", description="Navigation context.")}
    manifest = await materializer.materialize(job(), "build", inputs, outputs)
    assert manifest is not None
    assert embed.calls[0].inputs == ("Navigation context.\n\nOriginal evidence.",)
    assert await materializer.materialize(job(), "build", inputs, outputs) == manifest
    assert len(embed.calls) == 1
    vectors = Vectors()
    projection = DerivedVectorProjection(vectors, reader)
    context = StorageOperationContext.system("tenant")
    await projection.publish(build(), manifest, context=context)
    vectors.records[manifest.index_name].clear()
    await projection.publish(build(), manifest, context=context)
    assert vectors.records["raw-evidence"] == {"raw": "untouched"}
    assert len(embed.calls) == 1
    vectors.corrupt = True
    with pytest.raises(ValueError, match="verification"):
        await projection.publish(build(), manifest, context=context)
    await store.close()


@pytest.mark.asyncio
async def test_description_freeze_reuses_without_spending_and_retains_unknown_charge():
    store = MemoryObjectStore()
    await store.connect()
    repository, delegate = Budget(), Descriptions()
    generator = FrozenDescriptionGenerator(
        delegate,
        DerivedBudget(repository, "tenant", "build", Decimal("0.1")),
        DescriptionArtifacts(
            ImmutableArtifactReader(store), ImmutableArtifactWriter(store), "profile"
        ),
    )
    packets = (
        DescriptionPacket(
            packet_id="packet", text="Deploy only if approved.", chunk_ids=("chunk",)
        ),
    )
    output = await generator.generate(packets)
    repository.admitted = False
    assert await generator.generate(packets) == output
    assert delegate.calls == len(repository.requests) == len(repository.settlements) == 1
    assert repository.requests[0].provider_calls == 3
    assert repository.settlements[0][1].input_tokens is None
    await store.close()


@pytest.mark.asyncio
async def test_description_freeze_adopts_verified_concurrent_winner():
    store = MemoryObjectStore()
    await store.connect()
    repository, delegate = Budget(), Descriptions()
    winner = DescriptionOutput(
        description="Concurrent winner.",
        cited_packet_ids=("packet",),
        complete=True,
    )
    writer = ImmutableArtifactWriter(store)
    generator = FrozenDescriptionGenerator(
        delegate,
        DerivedBudget(repository, "tenant", "build", Decimal("0.1")),
        DescriptionArtifacts(
            ImmutableArtifactReader(store), RacingWriter(writer, winner), "profile"  # type: ignore[arg-type]
        ),
    )
    packets = (
        DescriptionPacket(
            packet_id="packet", text="Deploy only if approved.", chunk_ids=("chunk",)
        ),
    )

    assert await generator.generate(packets) == winner
    assert delegate.calls == len(repository.requests) == len(repository.settlements) == 1
    assert await generator.generate(packets) == winner
    assert delegate.calls == 1
    await store.close()
