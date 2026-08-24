"""Body-less but titled documents must still reach both projections."""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path

import pytest

from harborrag_adapters.repositories.object_store import MemoryObjectStore
from harborrag_core.domain.document import Document
from harborrag_core.domain.element import DocumentElement
from harborrag_core.domain.parser import ParsedDocument
from harborrag_core.domain.provenance import DocumentProvenance
from harborrag_core.ingestion import (
    BindingKind,
    ReindexJobState,
    SourceAdmissionDecision,
    SourceBinding,
)
from harborrag_engine.ingestion import produces_evidence
from harborrag_runtime.ingestion import (
    DocumentReindexService,
    DocumentReleaseService,
    ReindexRequest,
)
from harborrag_runtime.ingestion.document.title_content import with_title_as_content

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


def _document(title: str, elements: list[DocumentElement]) -> Document:
    return Document(
        id="doc-1",
        title=title,
        content=list(elements),
        content_type="confluence_page",
        provenance=DocumentProvenance(source="confluence"),
    )


def _fallback(document: Document, binding: BindingKind = BindingKind.ROOT) -> Document:
    return with_title_as_content(document, binding=binding)


# The capture-stage gate itself, not a copy of it: DocumentCaptureStages
# ._has_indexable_content delegates to this, so the tests cannot drift from production.
_indexable = produces_evidence


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


def test_a_titled_page_with_no_body_becomes_indexable() -> None:
    """The measured defect: 30 Confluence pages were dropped for having no body.

    A Confluence index page whose body is only a child-page macro normalizes to zero
    content elements. It was therefore dropped as unsupported -- no document version and
    so no graph node -- which removed exactly the section pages that hold the hierarchy
    together and left unnamed stubs in their place.
    """

    page = _document(
        "Quality Control Management",
        [DocumentElement("macro-1", "paragraph", None, {"macro": "children"})],
    )
    assert _indexable(page) is False

    result = _fallback(page)

    assert _indexable(result) is True
    titles = [
        element.content
        for element in result.content
        if element.metadata.get("role") == "document.title"
    ]
    assert titles == ["Quality Control Management"]
    # The original body is preserved after the synthesized heading.
    assert [element.id for element in result.content] == ["doc-1#title", "macro-1"]


def test_whitespace_only_body_still_gets_its_title() -> None:
    page = _document("Section Index", [DocumentElement("p1", "paragraph", "   ")])

    assert _indexable(_fallback(page)) is True


def test_a_title_that_is_only_the_document_id_is_not_content() -> None:
    """``DocumentNormalizer._title`` falls back to ``raw.id``, and ``Document.id`` is
    ``raw.id`` too, so an unguarded fallback would make every parsed document indexable
    and retire the unsupported decision by accident."""

    page = _document("doc-1", [DocumentElement("p1", "paragraph", None)])

    result = _fallback(page)

    assert result is page
    assert _indexable(result) is False


def test_a_document_with_neither_title_nor_body_stays_unsupported() -> None:
    page = _document("", [DocumentElement("p1", "paragraph", None)])

    result = _fallback(page)

    assert result is page
    assert _indexable(result) is False


def test_a_page_that_already_has_prose_is_untouched() -> None:
    page = _document("Runbook", [DocumentElement("p1", "paragraph", "Restart the worker.")])

    assert _fallback(page) is page


@pytest.mark.parametrize("binding", [BindingKind.ATTACHMENT, BindingKind.EMBEDDED])
def test_a_payload_of_another_document_keeps_todays_behaviour(binding: BindingKind) -> None:
    """An attachment's title is a filename, not a page name, and its parent already
    links it into the graph -- so a chunk holding nothing but ``diagram.png`` would be
    pure retrieval noise. Only self-standing source objects get the fallback."""

    binary = _document("diagram.png", [DocumentElement("p1", "paragraph", None)])

    assert with_title_as_content(binary, binding=binding) is binary


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


def test_a_page_built_only_from_headings_has_no_evidence() -> None:
    """Segmentation turns a heading into a section path and never into a unit, so a
    heading-only page reached BuildProjections and raised on an empty vector batch."""

    page = _document(
        "Quality Control Management",
        [DocumentElement("h1", "heading", "Sub-pages", {"level": 2})],
    )
    assert _indexable(page) is False

    result = _fallback(page)

    assert _indexable(result) is True
    # The heading survives as structure; the title becomes the evidence.
    assert [element.type for element in result.content] == ["paragraph", "heading"]


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
