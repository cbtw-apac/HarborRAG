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
from harborrag_core.indexing import VectorIndexRecord
from harborrag_core.ingestion import (
    ActiveDocumentVersion,
    DocumentIdentityBuilder,
    GraphEntityType,
    GraphNodeRecord,
    GraphOwnershipScope,
    KnowledgeNodeKind,
)
from harborrag_core.retrieval import (
    GraphNodeResolutionQuery,
    GraphNodeResolutionResult,
    GraphNodeSelectorKind,
)
from harborrag_core.security import AccessContext
from harborrag_engine.retrieval import ActiveVersionCandidateValidator
from harborrag_runtime.contracts import (
    DocumentContextRequest,
    EvidenceReadRequest,
    EvidenceReadSelector,
    GraphNodeResolveRequest,
)
from harborrag_runtime.retrieval.permissions import RetrievalPermissions
from harborrag_runtime.retrieval.reader_resources import ReaderResources
from harborrag_runtime.retrieval.readers import ReaderRetrieval

ACCESS = AccessContext.system("tenant-1")


def _chunk(chunk_id: str, ordinal: int, content: str) -> ChunkRecord:
    return ChunkRecord(
        strategy_version="strategy-1",
        logical_chunk_id=f"logical-{chunk_id}",
        chunk_id=chunk_id,
        connector_type=ConnectorType.LOCAL,
        document_kind=DocumentKind.LOCAL_FILE,
        record_kind=RecordKind.EVIDENCE,
        chunk_kind=ChunkKind.TEXT,
        tenant_id="tenant-1",
        connection_id="connection-1",
        source_scope_id="source-1",
        source_item_id="guide.md",
        source_version="source-version-1",
        document_id="document-1",
        document_version_id="version-1",
        ordinal=ordinal,
        content=content,
        embedding_text=content,
        search_text=content,
        token_count=3,
        content_hash=f"hash-{chunk_id}",
        hierarchy=ChunkHierarchy(
            document_title="Guide",
            section_path=("Overview",) if ordinal == 0 else ("Details",),
        ),
        security=ChunkSecurity(permission_set_id="public"),
    )


class Vectors:
    def __init__(self) -> None:
        identity = DocumentIdentityBuilder()
        self.records = {
            item: VectorIndexRecord(
                id=identity.point_id(chunk_id=item),
                tenant_id="tenant-1",
                vector=[1.0],
                payload={
                    "chunk_id": item,
                    "document_id": "document-1",
                    "document_version_id": "version-1",
                    "content": "stale projection text",
                },
            )
            for item in ("chunk-1", "chunk-2")
        }

    async def get_records(self, collection, ids, *, context):
        del collection, context
        return [record for record in self.records.values() if record.id in ids]


class Authority:
    def __init__(self) -> None:
        self.allowed = True

    async def active_versions(self, document_ids):
        return {
            item: ActiveDocumentVersion(document_id=item, document_version_id="version-1")
            for item in document_ids
        }

    async def active_snapshot(self, document_id):
        if document_id != "document-1":
            return None
        return SimpleNamespace(
            document_id=document_id,
            document_version_id="version-1",
            chunk_artifact=object(),
            chunk_index_artifact=object(),
        )


class Permissions:
    def __init__(self, source_ids: tuple[str, ...] = ("source-1",)) -> None:
        self.calls = 0
        self.revoke_after_first = False
        self.source_ids = set(source_ids)

    async def authorized_document_ids(self, tenant_id, document_ids, *, access):
        del tenant_id, access
        self.calls += 1
        if self.revoke_after_first and self.calls > 1:
            return set()
        return set(document_ids)

    async def authorized_source_scope_ids(self, tenant_id, source_scope_ids, *, access):
        del tenant_id, access
        return set(source_scope_ids) & self.source_ids


class Chunks:
    def __init__(self) -> None:
        self.values = {
            "chunk-1": _chunk("chunk-1", 0, "Canonical first chunk."),
            "chunk-2": _chunk("chunk-2", 1, "Canonical second chunk."),
        }

    async def get_artifacts(self, chunks, index, *, context):
        del chunks, index, context
        return SimpleNamespace(
            entries=tuple(SimpleNamespace(chunk_id=item) for item in self.values)
        )

    async def get_chunk(self, artifacts, chunk_id, *, context):
        del artifacts, context
        return self.values[chunk_id]


def _reader(permissions: Permissions | None = None, graph: object | None = None) -> ReaderRetrieval:
    authority = Authority()
    topology = permissions or Permissions()
    return ReaderRetrieval(
        ReaderResources(
            vectors=Vectors(),  # type: ignore[arg-type]
            validator=ActiveVersionCandidateValidator(authority),
            permissions=RetrievalPermissions(topology),  # type: ignore[arg-type]
            topology=topology,  # type: ignore[arg-type]
            snapshots=authority,  # type: ignore[arg-type]
            chunks=Chunks(),  # type: ignore[arg-type]
            sources=None,
            graph=graph,  # type: ignore[arg-type]
        )
    )


@pytest.mark.asyncio
async def test_evidence_read_uses_canonical_artifact_and_never_substitutes_version() -> None:
    reader = _reader()
    response = await reader.read_evidence(
        EvidenceReadRequest(
            ACCESS,
            (
                EvidenceReadSelector("chunk-1", "document-1", "version-1"),
                EvidenceReadSelector("chunk-2", "document-1", "old-version"),
            ),
        )
    )

    assert response.items[0].text == "Canonical first chunk."
    assert response.items[0].availability == "available"
    assert response.items[1].availability == "unavailable"
    assert response.items[1].text is None


@pytest.mark.asyncio
async def test_evidence_read_fails_closed_when_access_changes_before_return() -> None:
    permissions = Permissions()
    permissions.revoke_after_first = True
    response = await _reader(permissions).read_evidence(
        EvidenceReadRequest(ACCESS, (EvidenceReadSelector("chunk-1"),))
    )

    assert response.items[0].availability == "unavailable"
    assert response.items[0].text is None


@pytest.mark.asyncio
async def test_document_context_is_ordered_paged_and_version_bound() -> None:
    reader = _reader()
    first = await reader.document_context(
        DocumentContextRequest(ACCESS, "document-1", limit=1, include_outline=True)
    )
    second = await reader.document_context(
        DocumentContextRequest(
            ACCESS,
            "document-1",
            expected_document_version_id="version-1",
            offset=first.next_offset or 0,
            limit=1,
        )
    )
    changed = await reader.document_context(
        DocumentContextRequest(
            ACCESS,
            "document-1",
            expected_document_version_id="old-version",
        )
    )

    assert [item.chunk_id for item in first.chunks] == ["chunk-1"]
    assert first.next_offset == 1
    assert [item.chunk_id for item in second.chunks] == ["chunk-2"]
    assert changed.outcome == "version_changed"
    assert changed.chunks == ()


def _graph_node(key: str, scope: str) -> GraphNodeRecord:
    return GraphNodeRecord(
        node_key=key,
        node_kind=KnowledgeNodeKind.SOURCE_ENTITY,
        entity_type=GraphEntityType.GITHUB_REPOSITORY,
        logical_id=key,
        ownership_scope=GraphOwnershipScope.SOURCE_SCOPE,
        owner_id="tenant-1",
        source_scope_id=scope,
        title="Payments",
    )


class ResolutionGraph:
    def __init__(self, candidates: tuple[GraphNodeRecord, ...]) -> None:
        self.candidates = candidates
        self.query = None

    async def resolve_nodes(self, query, *, context):
        del context
        self.query = query
        return GraphNodeResolutionResult(candidates=self.candidates)


@pytest.mark.asyncio
async def test_graph_resolution_overfetches_then_removes_denied_title_collisions() -> None:
    graph = ResolutionGraph((_graph_node("hidden", "source-2"), _graph_node("visible", "source-1")))
    response = await _reader(Permissions(), graph).resolve_graph_nodes(
        GraphNodeResolveRequest(
            ACCESS,
            GraphNodeResolutionQuery(
                selector_kind=GraphNodeSelectorKind.EXACT_TITLE,
                value="Payments",
                limit=1,
            ),
        )
    )

    assert graph.query.limit == 100
    assert [item.node_key for item in response.candidates] == ["visible"]


@pytest.mark.asyncio
async def test_graph_resolution_filters_requested_scopes_before_graph_selection() -> None:
    graph = ResolutionGraph((_graph_node("visible", "source-1"),))
    await _reader(Permissions(), graph).resolve_graph_nodes(
        GraphNodeResolveRequest(
            ACCESS,
            GraphNodeResolutionQuery(
                selector_kind=GraphNodeSelectorKind.PROVIDER_ID,
                value="visible",
                source_scope_ids=("source-2", "source-1"),
            ),
        )
    )

    assert graph.query.source_scope_ids == ("source-1",)
