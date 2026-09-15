"""Bounded, publication-safe reader workflows for MCP and agent clients."""

from __future__ import annotations

import asyncio
from uuid import uuid4

from harborrag_core.contracts.errors import HarborCapabilityError
from harborrag_core.ingestion import GraphNodeRecord, GraphOwnershipScope, SourceCatalogQuery
from harborrag_core.retrieval import GraphNodeResolutionQuery
from harborrag_core.security import AccessContext
from harborrag_core.storage import StorageOperationContext
from harborrag_engine.retrieval import graph_access_scope

from ..contracts import (
    DocumentContextRequest,
    DocumentContextResponse,
    EvidenceReadRequest,
    EvidenceReadResponse,
    GraphNodeResolveRequest,
    GraphNodeResolveResponse,
    SourceListRequest,
    SourceListResponse,
)
from ..reader_contracts import (
    DocumentListRequest,
    DocumentListResponse,
    DocumentMetadataRequest,
    DocumentMetadataResponse,
)
from .document_catalog import DocumentCatalogReader
from .document_context import DocumentContextReader
from .immutable_evidence import ImmutableEvidenceReader
from .reader_resources import ReaderResources
from .summary_views import apply_summary_views

_READ_DEADLINE_SECONDS = 10
_RESOLUTION_CANDIDATE_LIMIT = 100


class ReaderRetrieval:
    """Compose canonical reader services and permission-filtered metadata views."""

    def __init__(self, resources: ReaderResources) -> None:
        self._resources = resources
        self._evidence = ImmutableEvidenceReader(resources)
        self._documents = DocumentContextReader(resources)
        self._catalog = DocumentCatalogReader(resources)

    async def document_metadata(self, request: DocumentMetadataRequest) -> DocumentMetadataResponse:
        request_id = f"document-{uuid4().hex}"
        context = _context(request.access, request_id, "document-metadata")
        async with asyncio.timeout(_READ_DEADLINE_SECONDS):
            document = await self._catalog.metadata(request.document_id, request.access, context)
        return DocumentMetadataResponse(request_id, document)

    async def list_documents(self, request: DocumentListRequest) -> DocumentListResponse:
        request_id = f"documents-{uuid4().hex}"
        context = _context(request.access, request_id, "document-list")
        async with asyncio.timeout(_READ_DEADLINE_SECONDS):
            return await self._catalog.list_documents(request, request_id, context)

    async def read_evidence(self, request: EvidenceReadRequest) -> EvidenceReadResponse:
        request_id = f"evidence-{uuid4().hex}"
        context = _context(request.access, request_id, "evidence-read")
        async with asyncio.timeout(_READ_DEADLINE_SECONDS):
            items = await self._evidence.read(request, context)
        return EvidenceReadResponse(request_id, items)

    async def document_context(self, request: DocumentContextRequest) -> DocumentContextResponse:
        request_id = f"document-{uuid4().hex}"
        context = _context(request.access, request_id, "document-context")
        async with asyncio.timeout(_READ_DEADLINE_SECONDS):
            return await self._documents.read(request, request_id, context)

    async def list_sources(self, request: SourceListRequest) -> SourceListResponse:
        if self._resources.sources is None:
            raise HarborCapabilityError("source catalog is not configured")
        request_id = f"sources-{uuid4().hex}"
        async with asyncio.timeout(_READ_DEADLINE_SECONDS):
            rows = await self._resources.sources.list_readable_sources(
                SourceCatalogQuery(
                    tenant_id=str(request.access.tenant_id),
                    access=request.access,
                    source_scope_ids=request.source_scope_ids,
                    connector_types=request.connector_types,
                    after_source_scope_id=request.after_source_scope_id,
                    limit=request.limit + 1,
                )
            )
        return SourceListResponse(request_id, rows[: request.limit], len(rows) > request.limit)

    async def resolve_graph_nodes(
        self, request: GraphNodeResolveRequest
    ) -> GraphNodeResolveResponse:
        if self._resources.graph is None:
            raise HarborCapabilityError("graph retrieval is not configured")
        request_id = f"nodes-{uuid4().hex}"
        context = _context(request.access, request_id, "graph-node-resolution")
        async with asyncio.timeout(_READ_DEADLINE_SECONDS):
            query = await self._authorized_resolution_query(request, context)
            if query is None:
                return GraphNodeResolveResponse(request_id, ())
            raw = await self._resources.graph.resolve_nodes(query, context=context)
            visible = await self._visible_nodes(raw.candidates, request.access)
            visible = await apply_summary_views(visible, self._resources.summaries, request.access)
        candidates = visible[: request.query.limit]
        return GraphNodeResolveResponse(
            request_id,
            candidates,
            raw.truncated or len(visible) > request.query.limit,
        )

    async def _authorized_resolution_query(
        self,
        request: GraphNodeResolveRequest,
        context: StorageOperationContext,
    ) -> GraphNodeResolutionQuery | None:
        topology = self._resources.topology
        query = request.query
        access_scope = await graph_access_scope(topology, context)
        if access_scope is not None and not access_scope.tenant_visible:
            return None
        if access_scope is not None and query.source_scope_ids:
            allowed = set(query.source_scope_ids) & set(access_scope.source_scope_ids)
            if not allowed:
                return None
            query = query.model_copy(update={"source_scope_ids": tuple(sorted(allowed))})
        return query.model_copy(
            update={
                "access_scope": access_scope,
                "limit": _RESOLUTION_CANDIDATE_LIMIT,
            }
        )

    async def _visible_nodes(
        self, nodes: tuple[GraphNodeRecord, ...], access: AccessContext
    ) -> tuple[GraphNodeRecord, ...]:
        topology = self._resources.topology
        if topology is None:
            return nodes
        document_ids = tuple(
            dict.fromkeys(str(node.document_id) for node in nodes if node.document_id is not None)
        )
        source_ids = tuple(
            dict.fromkeys(
                node.source_scope_id for node in nodes if node.source_scope_id is not None
            )
        )
        documents = await topology.authorized_document_ids(
            str(access.tenant_id), document_ids, access=access
        )
        sources = await topology.authorized_source_scope_ids(
            str(access.tenant_id), source_ids, access=access
        )
        tenant_visible = bool(documents or sources)
        return tuple(
            node for node in nodes if _node_visible(node, documents, sources, tenant_visible)
        )


def _node_visible(
    node: GraphNodeRecord,
    documents: set[str],
    sources: set[str],
    tenant_visible: bool,
) -> bool:
    if node.ownership_scope == GraphOwnershipScope.TENANT:
        return tenant_visible
    if node.document_id is not None:
        return str(node.document_id) in documents
    return node.source_scope_id in sources


def _context(
    access: AccessContext, request_id: str, operation_kind: str
) -> StorageOperationContext:
    return StorageOperationContext.for_access(
        access, operation_kind=operation_kind, idempotency_key=request_id
    )


__all__ = ["ReaderResources", "ReaderRetrieval"]
