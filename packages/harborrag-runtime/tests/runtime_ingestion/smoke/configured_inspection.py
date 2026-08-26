"""Privacy-safe inspection for configured-source ingestion smoke runs."""

from __future__ import annotations

import asyncio
from collections import Counter
from dataclasses import asdict, dataclass

from configured_document_inspection import (
    ConfiguredStores,
    inspect_documents,
)
from projection_inspection import validate_vector_payload

from harborrag_core.chunking import ConnectorType
from harborrag_core.ingestion import (
    DocumentIdentityBuilder,
)
from harborrag_core.schemas.storage import StorageOperationContext
from harborrag_engine.ingestion import EVIDENCE_INDEX
from harborrag_engine.retrieval import RetrievalLane
from harborrag_runtime.config.settings import RuntimeSettings
from harborrag_runtime.retrieval import (
    RetrievalOptions,
    RuntimeRetrievalService,
)


@dataclass(frozen=True, slots=True)
class ConfiguredInspectionRequest:
    settings: RuntimeSettings
    task_id: str
    tenant_id: str
    connector_type: str
    connection_id: str
    source_item_id: str
    dense_query: str
    sparse_query: str
    expected_documents: int


async def inspect_configured_source(
    request: ConfiguredInspectionRequest,
) -> dict[str, object]:
    """Assert all stores agree while returning no source body text."""

    stores = ConfiguredStores.build(request.settings)
    await stores.connect()
    try:
        results = await stores.control.tasks.document_results(request.task_id)
        if len(results) != request.expected_documents:
            raise AssertionError("configured smoke task/document count mismatch")
        if any(result.status == "failed" for result in results):
            raise AssertionError("configured smoke contains a failed document")
        pending_cleanup = await stores.control.reliability.pending_cleanup_jobs(
            limit=1_000,
            document_ids=tuple(str(result.document_id) for result in results),
        )
        if pending_cleanup:
            raise AssertionError("configured smoke retains pending projection cleanup jobs")
        snapshots = await asyncio.gather(
            *(
                stores.control.document_versions.active_snapshot(str(result.document_id))
                for result in results
            )
        )
        if any(snapshot is None for snapshot in snapshots):
            raise AssertionError("configured smoke document has no active version")
        context = StorageOperationContext.system(tenant_id=request.tenant_id)
        document_reports, versions = await inspect_documents(
            stores,
            snapshots,
            context,
        )
        point_report = await _inspect_points(
            stores.vectors,
            versions=versions,
            context=context,
        )
        root_document_id, traversal = await _inspect_graph(
            stores,
            request,
            context,
        )
        retrieval = await _inspect_retrieval(
            request.settings,
            tenant_id=request.tenant_id,
            root_document_id=root_document_id,
            dense_query=request.dense_query,
            sparse_query=request.sparse_query,
        )
        return {
            "documents": document_reports,
            "qdrant": point_report,
            "falkordb": {
                "nodes": len(traversal.nodes),
                "relations": len(traversal.relations),
                "relation_types": sorted(
                    {relation.relation_type.value for relation in traversal.relations}
                ),
            },
            "retrieval": retrieval,
        }
    finally:
        await stores.close()


async def _inspect_graph(
    stores: ConfiguredStores,
    request: ConfiguredInspectionRequest,
    context: StorageOperationContext,
):
    identities = DocumentIdentityBuilder()
    root_document_id = identities.document_id(
        tenant_id=request.tenant_id,
        connector_type=ConnectorType(request.connector_type),
        connection_id=request.connection_id,
        source_item_id=request.source_item_id,
    )
    root = await stores.control.document_versions.active_snapshot(root_document_id)
    if root is None:
        raise AssertionError("configured smoke root document is not active")
    traversal = await stores.graph.traverse(
        identities.document_version_node_key(
            document_id=root_document_id,
            document_version_id=str(root.document_version_id),
        ),
        max_depth=3,
        max_nodes=300,
        direction="both",
        context=context,
    )
    if traversal.truncated:
        raise AssertionError("configured smoke graph traversal was truncated")
    return root_document_id, traversal


async def _inspect_points(
    repository,
    *,
    versions: set[str],
    context: StorageOperationContext,
) -> dict[str, object]:
    counts: Counter[str] = Counter()
    dimensions: set[int] = set()
    sparse_terms: list[int] = []
    for collection in (EVIDENCE_INDEX,):
        cursor = None
        while True:
            page = await repository.scan_records(
                collection,
                limit=100,
                cursor=cursor,
                context=context,
            )
            for point in page.records:
                if point.payload.get("document_version_id") not in versions:
                    continue
                validate_vector_payload(collection, point)
                assert point.sparse_vector is not None
                counts[collection] += 1
                dimensions.add(len(point.vector))
                sparse_terms.append(len(point.sparse_vector.indices))
            cursor = page.next_cursor
            if cursor is None:
                break
    if not counts[EVIDENCE_INDEX]:
        raise AssertionError("configured smoke requires evidence points")
    return {
        "evidence_points": counts[EVIDENCE_INDEX],
        "dense_dimensions": sorted(dimensions),
        "sparse_term_range": [
            min(sparse_terms),
            max(sparse_terms),
        ],
    }


async def _inspect_retrieval(
    settings: RuntimeSettings,
    *,
    tenant_id: str,
    root_document_id: str,
    dense_query: str,
    sparse_query: str,
) -> dict[str, object]:
    service = await RuntimeRetrievalService.connect(settings)
    try:
        output = {}
        for lane, query in (
            (RetrievalLane.DENSE, dense_query),
            (RetrievalLane.SPARSE, sparse_query),
            (RetrievalLane.HYBRID, dense_query),
        ):
            report = await service.retrieve(
                query,
                tenant_id=tenant_id,
                top_k=5,
                options=RetrievalOptions(
                    lane=lane,
                    observe_graph=True,
                ),
            )
            selected = [
                result
                for result in report.results
                if result.metadata.get("document_id") == root_document_id
            ]
            if not selected:
                raise AssertionError(f"{lane.value} retrieval missed the configured root")
            output[lane.value] = {
                "results": len(report.results),
                "root_hits": len(selected),
                "chunk_kinds": sorted(
                    {str(result.metadata.get("chunk_kind")) for result in selected}
                ),
                "diagnostics": asdict(report.diagnostics),
            }
        return output
    finally:
        await service.aclose()
