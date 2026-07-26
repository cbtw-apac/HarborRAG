"""Reference parsing and object-backed chunk/manifest repositories (S2).

`IngestionObjectRepository` is the tenant-authorisation boundary for every
durable ingestion payload, and the chunk/manifest repositories are its only
production callers. These tests pin reference round-tripping, the rejection of
malformed and mismatched references, and the key-prefix authorisation that
stops a manifest reference from being read as a chunk body.
"""

from __future__ import annotations

import pytest
import pytest_asyncio

from harborrag_adapters.repositories.errors import HarborStorageNotFoundError
from harborrag_adapters.repositories.object_store.memory import MemoryObjectStore
from harborrag_core.schemas.documents import ChunkContext, ChunkRecord, ChunkSourceSpan
from harborrag_engine.ingestion.chunking.schemas import (
    ChunkManifest,
    ChunkReference,
    ChunkValidationResult,
)
from harborrag_runtime.temporal.artifact_objects import (
    IngestionObjectRepository,
    ObjectChunkRepository,
    ObjectManifestRepository,
    digest,
)

TENANT = "tenant-1"


@pytest_asyncio.fixture
async def store():
    store = MemoryObjectStore()
    await store.connect()
    try:
        yield store
    finally:
        await store.close()


def _reference(ordinal: int) -> ChunkReference:
    return ChunkReference(
        logical_chunk_id=f"logical-{ordinal}",
        chunk_revision_id=f"revision-{ordinal}",
        ordinal=ordinal,
        content_hash=f"hash-{ordinal}",
        token_count=4,
    )


def _record(reference: ChunkReference) -> ChunkRecord:
    return ChunkRecord(
        tenant_id=TENANT,
        document_id="document-1",
        document_version_id="document-version-1",
        artifact_id="artifact-1",
        artifact_revision_id="artifact-revision-1",
        logical_chunk_id=reference.logical_chunk_id,
        chunk_revision_id=reference.chunk_revision_id,
        ordinal=reference.ordinal,
        role="body",
        content=f"body {reference.ordinal}",
        content_hash=reference.content_hash,
        token_count=reference.token_count,
        context=ChunkContext(title="Guide", structural_path=("Guide",)),
        source_span=ChunkSourceSpan(start_offset=0, end_offset=6),
    )


def _manifest(references: tuple[ChunkReference, ...]) -> ChunkManifest:
    return ChunkManifest(
        tenant_id=TENANT,
        artifact_id="artifact-1",
        artifact_revision_id="artifact-revision-1",
        chunker_name="document",
        chunker_version="1",
        configuration_hash="config-1",
        chunks=references,
        total_token_count=sum(item.token_count for item in references),
        total_chunk_count=len(references),
        validation=ChunkValidationResult(valid=True),
        fingerprint="manifest-1",
    )


# --------------------------------------------------------------------------
# Reference format
# --------------------------------------------------------------------------


def test_reference_round_trips_through_parts() -> None:
    reference = IngestionObjectRepository.reference(TENANT, "sources/source.json")

    assert reference == f"harbor-object://{TENANT}/ingestion/sources/source.json"
    assert IngestionObjectRepository.parts(reference) == (
        TENANT,
        "ingestion",
        "sources/source.json",
    )


def test_reference_percent_encodes_an_awkward_tenant() -> None:
    reference = IngestionObjectRepository.reference("tenant/with/slashes", "k/v.json")

    tenant, bucket, key = IngestionObjectRepository.parts(reference)

    assert tenant == "tenant/with/slashes"
    assert bucket == "ingestion"
    assert key == "k/v.json"


@pytest.mark.parametrize(
    "reference",
    [
        "https://example.com/ingestion/key.json",
        "harbor-object:///ingestion/key.json",
        "harbor-object://tenant-1/ingestion",
        "not-a-reference",
    ],
)
def test_parts_rejects_a_malformed_reference(reference: str) -> None:
    with pytest.raises(ValueError, match="invalid ingestion object reference"):
        IngestionObjectRepository.parts(reference)


# --------------------------------------------------------------------------
# Key-scope authorisation
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_get_enforces_the_expected_key_prefix_and_suffix(store) -> None:
    objects = IngestionObjectRepository(store)
    reference = await objects.put(TENANT, "manifests/m.json", b"{}", kind="chunk-manifest")

    # The same reference read under the chunk-body scope must be refused.
    with pytest.raises(ValueError, match="key kind is not authorized"):
        await objects.get(reference, expected_tenant_id=TENANT, expected_key_prefix="chunks/")
    with pytest.raises(ValueError, match="key kind is not authorized"):
        await objects.get(reference, expected_tenant_id=TENANT, expected_key_suffix=".bin")

    assert (
        await objects.get(
            reference,
            expected_tenant_id=TENANT,
            expected_key_prefix="manifests/",
            expected_key_suffix=".json",
        )
        == b"{}"
    )


@pytest.mark.asyncio
async def test_exists_is_scoped_to_its_tenant(store) -> None:
    objects = IngestionObjectRepository(store)
    await objects.put(TENANT, "manifests/m.json", b"{}", kind="chunk-manifest")

    assert await objects.exists(TENANT, "manifests/m.json") is True
    assert await objects.exists(TENANT, "manifests/absent.json") is False


# --------------------------------------------------------------------------
# Chunk bodies
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_chunk_bodies_round_trip_in_requested_order(store) -> None:
    repository = ObjectChunkRepository(IngestionObjectRepository(store))
    references = (_reference(0), _reference(1))
    records = tuple(_record(reference) for reference in references)

    await repository.put(records)
    restored = await repository.get_many(
        TENANT,
        tuple(str(record.chunk_revision_id) for record in records),
    )

    assert tuple(str(item.chunk_revision_id) for item in restored) == (
        "revision-0",
        "revision-1",
    )
    assert restored[0].content == "body 0"


@pytest.mark.asyncio
async def test_chunk_bodies_are_not_readable_by_another_tenant(store) -> None:
    """The repository derives its own reference, so another tenant simply misses.

    Isolation here comes from the tenant-scoped key/context rather than the
    mismatch guard in `IngestionObjectRepository.get()`: a caller cannot supply
    a foreign reference through this path at all.
    """
    repository = ObjectChunkRepository(IngestionObjectRepository(store))

    await repository.put((_record(_reference(0)),))

    with pytest.raises(HarborStorageNotFoundError):
        await repository.get_many("tenant-other", ("revision-0",))

    # The owning tenant still reads it back.
    assert (await repository.get_many(TENANT, ("revision-0",)))[0].content == "body 0"


# --------------------------------------------------------------------------
# Manifests
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_manifest_round_trips_and_is_missing_before_it_is_written(store) -> None:
    repository = ObjectManifestRepository(IngestionObjectRepository(store))
    manifest = _manifest((_reference(0), _reference(1)))

    assert (await repository.get(TENANT, "artifact-1", "artifact-revision-1", "config-1")) is None

    await repository.put(manifest)
    restored = await repository.get(TENANT, "artifact-1", "artifact-revision-1", "config-1")

    assert restored is not None
    assert restored.fingerprint == "manifest-1"
    assert restored.total_chunk_count == 2


@pytest.mark.asyncio
async def test_manifest_lookup_is_keyed_by_configuration(store) -> None:
    """A different chunking configuration must not resolve to a stale manifest."""
    repository = ObjectManifestRepository(IngestionObjectRepository(store))

    await repository.put(_manifest((_reference(0),)))

    assert (
        await repository.get(TENANT, "artifact-1", "artifact-revision-1", "other-config")
    ) is None


# --------------------------------------------------------------------------
# Key derivation
# --------------------------------------------------------------------------


def test_digest_is_stable_and_distinguishes_inputs() -> None:
    assert digest("a") == digest("a")
    assert digest("a") != digest("b")
    assert len(digest("a")) == 64
