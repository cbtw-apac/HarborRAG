"""Releasing a body-less but titled page, end to end.

The transform is ``harborrag_engine.ingestion.with_title_as_content`` and its own unit
tests live beside it; what runtime owns is the orchestration -- that capture calls it
before the indexable-content gate, and that the synthesized paragraph lands in canonical
so a reindex rebuilds the same evidence.
"""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path

import pytest

from harborrag_adapters.repositories.object_store import MemoryObjectStore
from harborrag_core.domain.element import DocumentElement
from harborrag_core.domain.parser import ParsedDocument
from harborrag_core.ingestion import (
    BindingKind,
    ReindexJobState,
    SourceAdmissionDecision,
    SourceBinding,
)
from harborrag_runtime.ingestion import (
    DocumentReindexService,
    DocumentReleaseService,
    ReindexRequest,
)

from ...fixtures.connectors import DeterministicEmbedClient, SourceConnector, TextParser
from ...fixtures.release import (
    ReleaseResources,
    build_control_plane,
    build_dependencies,
    build_release_service,
    processing_profile,
    release_request,
)
from ...fixtures.storage import InMemoryKnowledgeGraph, InMemoryVectorRepository


class _HeadingParser(TextParser):
    """Parse the body into headings only, the way a section-index page normalizes."""

    def parse(self, raw):
        parsed = super().parse(raw)
        return ParsedDocument(
            content=parsed.content,
            parser_name=parsed.parser_name,
            parser_version=parsed.parser_version,
            elements=[
                DocumentElement("h1", "heading", raw.text(), {"level": 2}),
            ],
        )


@pytest.mark.asyncio
async def test_release_publishes_a_titled_page_with_an_empty_body(tmp_path: Path) -> None:
    """The pure function can only show the shape; this shows the gate actually flipped."""

    control = build_control_plane(tmp_path)
    store = MemoryObjectStore()
    connector = SourceConnector()
    connector.body = ""
    embed = DeterministicEmbedClient()
    async with control, store:
        service = build_release_service(
            ReleaseResources(
                control,
                store,
                TextParser(),
                embed,
                InMemoryVectorRepository(),
                InMemoryKnowledgeGraph(),
            )
        )

        outcome = await service.release(release_request(source_version="title-only-1"), connector)

        assert outcome.decision != SourceAdmissionDecision.UNSUPPORTED
        assert outcome.published is True
        assert outcome.evidence_chunks == 1
        assert any("Worker guide" in text for text in embed.inputs)


@pytest.mark.asyncio
async def test_release_publishes_a_heading_only_page_instead_of_failing(tmp_path: Path) -> None:
    """Before the gate agreed with segmentation this raised ValueError: vector
    projection batch must not be empty, at stage=BuildProjections."""

    control = build_control_plane(tmp_path)
    store = MemoryObjectStore()
    connector = SourceConnector()
    embed = DeterministicEmbedClient()
    async with control, store:
        service = build_release_service(
            ReleaseResources(
                control,
                store,
                _HeadingParser(),
                embed,
                InMemoryVectorRepository(),
                InMemoryKnowledgeGraph(),
            )
        )

        outcome = await service.release(release_request(source_version="headings-1"), connector)

        assert outcome.published is True
        assert outcome.evidence_chunks == 1
        assert any("Worker guide" in text for text in embed.inputs)


@pytest.mark.asyncio
async def test_release_skips_a_heading_only_attachment_rather_than_failing(tmp_path: Path) -> None:
    """An attachment gets no title fallback, so it must still exit cleanly as
    UNSUPPORTED -- the crash it used to take was the real defect, not the skip."""

    control = build_control_plane(tmp_path)
    store = MemoryObjectStore()
    connector = SourceConnector()
    embed = DeterministicEmbedClient()
    async with control, store:
        service = build_release_service(
            ReleaseResources(
                control,
                store,
                _HeadingParser(),
                embed,
                InMemoryVectorRepository(),
                InMemoryKnowledgeGraph(),
            )
        )
        request = release_request(source_version="attached-headings-1")
        request = replace(
            request,
            source_identity=request.source_identity.model_copy(
                update={
                    "binding": SourceBinding(
                        kind=BindingKind.ATTACHMENT,
                        parent_source_item_id="docs/worker.txt",
                    )
                }
            ),
        )

        outcome = await service.release(request, connector)

        assert outcome.decision == SourceAdmissionDecision.UNSUPPORTED
        assert outcome.published is False
        assert embed.inputs == []


@pytest.mark.asyncio
async def test_reindex_rechunks_a_heading_only_page_from_canonical(tmp_path: Path) -> None:
    """The synthesized paragraph has to be *in* the canonical content, not just in the
    chunks.

    Reindex enters at release_prepared and so runs neither the capture gate nor the
    title fallback -- it re-chunks whatever canonical holds. If the fallback lived only
    in the chunking step, a chunk-strategy reindex would rebuild this document into a
    route with no evidence and hit the same empty-batch ValueError the release path just
    stopped taking.
    """

    control = build_control_plane(tmp_path)
    resources = ReleaseResources(
        control=control,
        store=MemoryObjectStore(),
        parser=_HeadingParser(),
        embed=DeterministicEmbedClient(),
        vectors=InMemoryVectorRepository(),
        graph=InMemoryKnowledgeGraph(),
    )
    async with control, resources.store:
        dependencies = build_dependencies(resources)
        first = await DocumentReleaseService(dependencies).release(
            release_request(source_version="reindexed-headings-1"),
            SourceConnector(),
        )
        assert first.published is True

        # A chunk-strategy change is the lane that rebuilds chunks from canonical.
        job = await DocumentReindexService(dependencies).run(
            ReindexRequest(
                reindex_job_id="reindex-headings-1",
                tenant_id="default",
                processing=processing_profile().model_copy(
                    update={"chunk_strategy": "canonical-v2"}
                ),
                document_id=first.document_id,
            )
        )

        assert job.status == ReindexJobState.COMPLETED
        assert job.published_count == 1
        assert resources.parser.calls == 1  # canonical was reused, not re-parsed
