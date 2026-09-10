"""A tenant that has never ingested is not a broken service.

The vector adapter reports a missing collection as ``HarborStorageNotFoundError``,
a bare ``RuntimeError`` outside the Harbor hierarchy. Every caller therefore
treated "you have not ingested anything yet" as an unexpected fault and told
the user the service was unavailable, which is both wrong and unactionable.
"""

from __future__ import annotations

import pytest
from retrieval_test_support import FakeChunkReader, FakeEmbedClient, FakeVectorRepository
from retrieval_test_support import policy as _policy
from retrieval_test_support import resources as _resources

from harborrag_adapters.repositories.errors import (
    HarborStorageNotFoundError,
    StorageErrorContext,
)
from harborrag_core.contracts.errors import HarborNoIndexedContentError
from harborrag_core.storage import StorageFamily
from harborrag_runtime.retrieval import RetrievalOptions, RuntimeRetrievalService


def _missing_collection() -> HarborStorageNotFoundError:
    """The error the Qdrant adapter raises before the first ingest creates it."""

    return HarborStorageNotFoundError(
        "collection schema 'evidence' does not exist",
        context=StorageErrorContext(
            family=StorageFamily.VECTOR,
            backend="qdrant",
            instance_name="primary",
            operation="schema_lookup",
        ),
    )


class MissingCollectionRepository(FakeVectorRepository):
    """A vector store with no collection for this tenant, as before first ingest."""

    async def search(self, query, *, context):
        raise _missing_collection()

    async def sparse_search(self, query, *, context):
        raise _missing_collection()

    async def hybrid_search(self, query, *, context):
        raise _missing_collection()


@pytest.mark.asyncio
async def test_missing_collection_is_reported_as_no_indexed_content() -> None:
    service = RuntimeRetrievalService(
        resources=_resources(
            embed=FakeEmbedClient(),
            vectors=MissingCollectionRepository(),
            chunks=FakeChunkReader(),
        ),
        policy=_policy(),
    )

    with pytest.raises(HarborNoIndexedContentError):
        await service.retrieve(
            "anything at all",
            tenant_id="tenant-1",
            top_k=2,
            options=RetrievalOptions(),
        )


@pytest.mark.asyncio
async def test_no_indexed_content_message_names_no_credentials_or_paths() -> None:
    """The message reaches the caller, so it carries nothing but the condition."""

    service = RuntimeRetrievalService(
        resources=_resources(
            embed=FakeEmbedClient(),
            vectors=MissingCollectionRepository(),
            chunks=FakeChunkReader(),
        ),
        policy=_policy(),
    )

    with pytest.raises(HarborNoIndexedContentError) as caught:
        await service.retrieve("q", tenant_id="tenant-1", top_k=2, options=RetrievalOptions())

    assert "collection schema" not in str(caught.value)
