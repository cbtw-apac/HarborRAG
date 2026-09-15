"""Use the real immutable writer and tenant-scoped memory store."""

from __future__ import annotations

import asyncio

import pytest

from harborrag_adapters.repositories.errors import HarborStorageNotFoundError
from harborrag_adapters.repositories.object_store import (
    ImmutableArtifactReader,
    ImmutableArtifactWriter,
    MemoryObjectStore,
)
from harborrag_adapters.topology.artifacts import ExtractionArtifacts
from harborrag_core.schemas.object_store import PutObjectRequest
from harborrag_core.storage import StorageOperationContext
from harborrag_core.topology.extraction import (
    ChunkExtractionInput,
    EvidenceSpan,
    ExtractedEntity,
    ExtractionOutput,
    ExtractionProfile,
)


def profile():
    return ExtractionProfile(model="model", deployment_revision="revision", prompt_digest="prompt")


def value():
    return ChunkExtractionInput(chunk_id="chunk-1", content="Alpha Beta", context="source context")


def output(name="Alpha"):
    start = 0 if name == "Alpha" else 6
    return ExtractionOutput(
        entities=(
            ExtractedEntity(
                local_id="e1",
                name=name,
                entity_type="service",
                span=EvidenceSpan(start=start, end=start + len(name), quote=name),
            ),
        )
    )


def artifacts(store):
    return ExtractionArtifacts(ImmutableArtifactWriter(store), ImmutableArtifactReader(store))


@pytest.mark.asyncio
async def test_concurrent_valid_extractions_freeze_the_first_result_once():
    async with MemoryObjectStore() as store:
        repo = artifacts(store)
        context = StorageOperationContext.system("tenant-a")
        refs = await asyncio.gather(
            *(
                repo.freeze(profile(), value(), extracted, context=context)
                for extracted in (output("Alpha"), output("Beta"))
            )
        )
        assert refs[0] == refs[1]
        winner = await repo.read(refs[0], value(), profile=profile(), context=context)
        assert winner in (output("Alpha"), output("Beta"))
        replay = await repo.freeze(profile(), value(), output("Beta"), context=context)
        assert replay == refs[0]
        assert await repo.read(replay, value(), profile=profile(), context=context) == winner


@pytest.mark.asyncio
async def test_identical_content_cannot_read_another_tenants_artifact():
    async with MemoryObjectStore() as store:
        repo = artifacts(store)
        tenant_a = StorageOperationContext.system("tenant-a")
        tenant_b = StorageOperationContext.system("tenant-b")
        first = await repo.freeze(profile(), value(), output(), context=tenant_a)
        assert await repo.find(profile(), value(), context=tenant_b) is None
        with pytest.raises(HarborStorageNotFoundError):
            await repo.read(first, value(), profile=profile(), context=tenant_b)
        second = await repo.freeze(profile(), value(), output("Beta"), context=tenant_b)
        assert first.key == second.key
        assert first.sha256 != second.sha256
        assert await repo.read(first, value(), profile=profile(), context=tenant_a) == output()


@pytest.mark.asyncio
async def test_corrupted_bytes_and_wrong_reference_metadata_are_rejected():
    async with MemoryObjectStore() as store:
        repo = artifacts(store)
        context = StorageOperationContext.system("tenant-a")
        reference = await repo.freeze(profile(), value(), output(), context=context)
        for changes in ({"sha256": "0" * 64}, {"byte_size": reference.byte_size + 1}):
            with pytest.raises(ValueError, match="integrity check"):
                await repo.read(
                    reference.model_copy(update=changes),
                    value(),
                    profile=profile(),
                    context=context,
                )
        await store.put(
            PutObjectRequest(bucket=reference.bucket, key=reference.key, body=b"corrupt"),
            context=context,
        )
        with pytest.raises(ValueError, match="integrity check"):
            await repo.read(reference, value(), profile=profile(), context=context)


@pytest.mark.asyncio
async def test_checkpoint_reference_must_match_profile_and_exact_input_context():
    async with MemoryObjectStore() as store:
        repo = artifacts(store)
        context = StorageOperationContext.system("tenant-a")
        reference = await repo.freeze(profile(), value(), output(), context=context)
        other_profile = profile().model_copy(update={"deployment_revision": "new"})
        with pytest.raises(ValueError, match="profile/input"):
            await repo.read(reference, value(), profile=other_profile, context=context)
        with pytest.raises(ValueError, match="profile/input"):
            await repo.read(
                reference,
                value().model_copy(update={"context": "different"}),
                profile=profile(),
                context=context,
            )
        # Reuse can cross logical chunk IDs when content and contextual input agree.
        assert (
            await repo.read(
                reference,
                value().model_copy(update={"chunk_id": "c2"}),
                profile=profile(),
                context=context,
            )
            == output()
        )


@pytest.mark.asyncio
async def test_invalid_extraction_is_never_frozen():
    async with MemoryObjectStore() as store:
        repo = artifacts(store)
        context = StorageOperationContext.system("tenant-a")
        invalid = value().model_copy(update={"content": "invalid"})
        with pytest.raises(ValueError, match="span does not match"):
            await repo.freeze(profile(), invalid, output(), context=context)
        assert await repo.find(profile(), invalid, context=context) is None


@pytest.mark.asyncio
@pytest.mark.parametrize("flags", [{"complete": False}, {"overflow": True}])
async def test_incomplete_extraction_never_becomes_a_ready_checkpoint(flags):
    async with MemoryObjectStore() as store:
        repo = artifacts(store)
        context = StorageOperationContext.system("tenant-a")
        with pytest.raises(ValueError, match="incomplete or overflow"):
            await repo.freeze(
                profile(), value(), output().model_copy(update=flags), context=context
            )
        assert await repo.find(profile(), value(), context=context) is None
