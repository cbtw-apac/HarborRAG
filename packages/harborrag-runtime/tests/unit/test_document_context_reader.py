"""Document context windows stay bounded, anchored and bound to one version."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from harborrag_core.chunking import (
    ChunkHierarchy,
    ChunkKind,
    ChunkRecord,
    ChunkSecurity,
    ConnectorType,
    DocumentKind,
    RecordKind,
)
from harborrag_core.contracts.errors import HarborCapabilityError
from harborrag_core.contracts.reader import DocumentContextRequest
from harborrag_core.security import AccessContext
from harborrag_core.storage import StorageOperationContext
from harborrag_runtime.retrieval import document_context
from harborrag_runtime.retrieval.document_context import DocumentContextReader

ACCESS = AccessContext.system("tenant-1")
CONTEXT = StorageOperationContext.for_access(
    ACCESS, operation_kind="document-context", idempotency_key="request-1"
)
SECTIONS = (("Overview",), ("Details",), ("Details",))


def _chunk(index: int, content: str, tenant_id: str = "tenant-1") -> ChunkRecord:
    return ChunkRecord(
        strategy_version="strategy-1",
        logical_chunk_id=f"logical-{index}",
        chunk_id=f"chunk-{index}",
        connector_type=ConnectorType.LOCAL,
        document_kind=DocumentKind.LOCAL_FILE,
        record_kind=RecordKind.EVIDENCE,
        chunk_kind=ChunkKind.TEXT,
        tenant_id=tenant_id,
        connection_id="connection-1",
        source_scope_id="source-1",
        source_item_id="guide.md",
        source_version="source-version-1",
        document_id="document-1",
        document_version_id="version-1",
        ordinal=index,
        content=content,
        embedding_text=content,
        search_text=content,
        token_count=3,
        content_hash=f"hash-{index}",
        hierarchy=ChunkHierarchy(document_title="Guide", section_path=SECTIONS[index]),
        security=ChunkSecurity(permission_set_id="public"),
    )


class Topology:
    def __init__(self, allowed_calls: int = 100) -> None:
        self.allowed_calls = allowed_calls
        self.calls = 0

    async def authorized_document_ids(self, tenant_id, document_ids, *, access):
        del tenant_id, access
        self.calls += 1
        return set(document_ids) if self.calls <= self.allowed_calls else set()


class Snapshots:
    """Serves ``versions`` in turn, repeating the last one."""

    def __init__(self, *versions: str | None) -> None:
        self.versions = list(versions or ("version-1",))

    async def active_snapshot(self, document_id):
        version = self.versions.pop(0) if len(self.versions) > 1 else self.versions[0]
        if document_id != "document-1" or version is None:
            return None
        return SimpleNamespace(
            document_version_id=version,
            chunk_artifact=object(),
            chunk_index_artifact=object(),
        )


class Chunks:
    def __init__(self, *chunks: ChunkRecord) -> None:
        self.values = {str(item.chunk_id): item for item in chunks}

    async def get_artifacts(self, chunks, index, *, context):
        del chunks, index, context
        return SimpleNamespace(entries=tuple(SimpleNamespace(chunk_id=key) for key in self.values))

    async def get_chunk(self, artifacts, chunk_id, *, context):
        del artifacts, context
        return self.values[chunk_id]


def _default_chunks() -> Chunks:
    return Chunks(_chunk(0, "First."), _chunk(1, "Second."), _chunk(2, "Third."))


def _reader(
    chunks: Chunks | None = None,
    topology: Topology | None = None,
    snapshots: Snapshots | None = None,
) -> DocumentContextReader:
    resources = SimpleNamespace(
        topology=topology if topology is not None else Topology(),
        snapshots=snapshots if snapshots is not None else Snapshots(),
        chunks=chunks if chunks is not None else _default_chunks(),
    )
    return DocumentContextReader(resources)  # type: ignore[arg-type]


async def _read(reader: DocumentContextReader, **options: object):
    request = DocumentContextRequest(ACCESS, "document-1", **options)  # type: ignore[arg-type]
    return await reader.read(request, "request-1", CONTEXT)


@pytest.mark.asyncio
async def test_a_reader_without_snapshots_is_not_configured() -> None:
    reader = DocumentContextReader(
        SimpleNamespace(topology=Topology(), snapshots=None, chunks=_default_chunks())  # type: ignore[arg-type]
    )

    with pytest.raises(HarborCapabilityError, match="document context reader is not configured"):
        await _read(reader)


@pytest.mark.asyncio
async def test_a_window_reads_in_order_and_outlines_its_distinct_sections() -> None:
    response = await _read(_reader(), include_outline=True)

    assert response.outcome == "ok"
    assert response.document_version_id == "version-1"
    assert [item.chunk_id for item in response.chunks] == ["chunk-0", "chunk-1", "chunk-2"]
    assert response.outline == (("Overview",), ("Details",))
    assert response.next_offset is None
    assert response.document_title == "Guide"


@pytest.mark.asyncio
async def test_an_offset_past_the_end_reads_an_empty_window() -> None:
    response = await _read(_reader(), offset=10)

    assert response.outcome == "ok"
    assert response.chunks == ()
    assert response.document_title is None


@pytest.mark.asyncio
async def test_a_chunk_anchor_starts_the_window_at_that_chunk() -> None:
    response = await _read(_reader(), anchor_chunk_id="chunk-1", limit=1)

    assert [item.chunk_id for item in response.chunks] == ["chunk-1"]
    assert response.next_offset == 2


@pytest.mark.asyncio
async def test_a_section_anchor_starts_at_the_first_chunk_of_that_section() -> None:
    response = await _read(_reader(), anchor_section_path=("Details",))

    assert [item.chunk_id for item in response.chunks] == ["chunk-1", "chunk-2"]
    assert all(item.section_path == ("Details",) for item in response.chunks)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "options",
    [
        {"anchor_chunk_id": "chunk-missing"},
        {"anchor_section_path": ("Appendix",)},
    ],
)
async def test_an_anchor_that_is_not_in_the_version_is_unavailable(
    options: dict[str, object],
) -> None:
    response = await _read(_reader(), **options)

    assert response.outcome == "unavailable"
    assert response.chunks == ()
    assert response.document_version_id is None


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("topology", "snapshots"),
    [
        # The caller may not read the document at all.
        (Topology(allowed_calls=0), Snapshots()),
        # No active publication for the document.
        (Topology(), Snapshots(None)),
        # Access is revoked while the window is read.
        (Topology(allowed_calls=1), Snapshots()),
        # The active version moves on while the window is read.
        (Topology(), Snapshots("version-1", "version-2")),
        # The document is unpublished while the window is read.
        (Topology(), Snapshots("version-1", None)),
    ],
)
async def test_a_window_fails_closed_when_access_or_publication_does_not_hold(
    topology: Topology, snapshots: Snapshots
) -> None:
    response = await _read(_reader(topology=topology, snapshots=snapshots))

    assert response.outcome == "unavailable"
    assert response.chunks == ()


@pytest.mark.asyncio
async def test_a_reader_without_topology_never_releases_a_document() -> None:
    reader = DocumentContextReader(
        SimpleNamespace(topology=None, snapshots=Snapshots(), chunks=_default_chunks())  # type: ignore[arg-type]
    )

    response = await _read(reader)

    assert response.outcome == "unavailable"


@pytest.mark.asyncio
async def test_a_chunk_from_another_tenant_makes_the_window_unavailable() -> None:
    chunks = Chunks(_chunk(0, "First."), _chunk(1, "Leaked.", tenant_id="tenant-2"))

    response = await _read(_reader(chunks=chunks))

    assert response.outcome == "unavailable"
    assert response.chunks == ()


@pytest.mark.asyncio
async def test_the_content_budget_ends_a_window_before_the_overflowing_chunk(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(document_context, "_CONTENT_BUDGET_BYTES", 10)

    response = await _read(_reader())

    assert [item.chunk_id for item in response.chunks] == ["chunk-0"]
    assert response.next_offset == 1
    assert response.outcome == "ok"


@pytest.mark.asyncio
async def test_a_single_chunk_over_budget_is_skipped_as_an_output_limit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(document_context, "_CONTENT_BUDGET_BYTES", 3)

    response = await _read(_reader())

    assert response.outcome == "output_limit"
    assert response.chunks == ()
    assert response.next_offset == 1
    # The skipped chunk still supplies the document title.
    assert response.document_title == "Guide"
