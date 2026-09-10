"""Retrieval surfaces the document title and section it already stores.

Two Confluence pages -- "FE Onboarding Checklist" and "BE Onboarding
checklist" -- retrieved together were merged into one incoherent list,
because the prompt identified each source only by an opaque
``document_id`` hash. The titles were in the vector payload the whole time
and were dropped when the result was projected.
"""

from __future__ import annotations

import pytest
from retrieval_test_support import FakeChunkReader, FakeEmbedClient, FakeVectorRepository
from retrieval_test_support import policy as _policy
from retrieval_test_support import resources as _resources

from harborrag_runtime.retrieval import RetrievalOptions, RuntimeRetrievalService


@pytest.mark.asyncio
async def test_results_carry_the_document_title_and_section() -> None:
    service = RuntimeRetrievalService(
        resources=_resources(
            embed=FakeEmbedClient(),
            vectors=FakeVectorRepository(),
            chunks=FakeChunkReader(),
        ),
        policy=_policy(),
    )

    report = await service.retrieve(
        "release acceptance",
        tenant_id="tenant-1",
        top_k=2,
        options=RetrievalOptions(),
    )

    metadata = report.results[0].metadata
    assert metadata["document_title"] == "FE Onboarding Checklist"
    assert metadata["section_path"] == ["Onboarding", "Accounts"]


@pytest.mark.asyncio
async def test_missing_title_and_section_degrade_to_empty_not_missing() -> None:
    """A payload without them still projects: older chunks predate the fields."""

    vectors = FakeVectorRepository()
    vectors.drop_provenance = True  # type: ignore[attr-defined]
    service = RuntimeRetrievalService(
        resources=_resources(embed=FakeEmbedClient(), vectors=vectors, chunks=FakeChunkReader()),
        policy=_policy(),
    )

    report = await service.retrieve(
        "release acceptance", tenant_id="tenant-1", top_k=2, options=RetrievalOptions()
    )

    assert report.results[0].metadata["document_title"] == ""
    assert report.results[0].metadata["section_path"] == []
