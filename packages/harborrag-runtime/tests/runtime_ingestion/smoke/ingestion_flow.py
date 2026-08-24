"""Run the deployed Temporal-to-retrieval ingestion smoke check.

Usage:
    .venv/bin/python \
      packages/harborrag-runtime/tests/runtime_ingestion/smoke/ingestion_flow.py
"""

from __future__ import annotations

import asyncio
import json
from dataclasses import asdict, replace
from uuid import uuid4

from configuration import TENANT_ID, load_smoke_configuration
from inspection import StoreObservation, inspect_stores
from redis.asyncio import Redis

from harborrag_engine.retrieval import RetrievalLane
from harborrag_runtime.config.temporal import TemporalRuntimeConfig
from harborrag_runtime.retrieval import (
    RetrievalOptions,
    RuntimeRetrievalService,
)
from harborrag_runtime.temporal.client import (
    IngestionTemporalClient,
)
from harborrag_runtime.temporal.maintenance_schemas import (
    ReindexInput,
)
from harborrag_runtime.temporal.schemas import (
    SourceIngestionResult,
)


async def run_smoke() -> dict[str, object]:
    configuration = load_smoke_configuration()
    run_token = uuid4().hex
    configuration = replace(
        configuration,
        processing=replace(
            configuration.processing,
            graph_projection_version=(f"structural-graph-smoke-{run_token}"),
        ),
    )
    temporal = await IngestionTemporalClient.connect(
        TemporalRuntimeConfig.from_settings(configuration.settings)
    )
    first_id = f"ingestion-smoke-{run_token}"
    first_handle = await temporal.start(configuration.source_input(first_id))
    first = await first_handle.result()
    _assert_balanced_success(first)
    _assert_first_publication(first)
    first_status = await temporal.execution_status(first_id)
    history = await first_handle.fetch_history()
    history_events = len(history.events)
    history_bytes = sum(event.ByteSize() for event in history.events)
    maximum_event_bytes = max(
        (event.ByteSize() for event in history.events),
        default=0,
    )
    stores = await inspect_stores(
        configuration.settings,
        tenant_id=TENANT_ID,
        task_id=first_id,
    )
    retrieval = await _retrieval_observations(configuration.settings)
    redis_flush = await _flush_disposable_redis(configuration.settings)

    replay_id = f"ingestion-smoke-replay-{uuid4().hex}"
    replay_handle = await temporal.start(configuration.source_input(replay_id))
    replay = await replay_handle.result()
    _assert_balanced_success(replay)
    if replay.unchanged != replay.discovered:
        raise AssertionError("an unchanged replay unexpectedly reprocessed documents")
    replay_stores = await inspect_stores(
        configuration.settings,
        tenant_id=TENANT_ID,
        task_id=replay_id,
    )
    if replay_stores.versions != stores.versions:
        raise AssertionError("unchanged replay produced different active versions")
    reindex = await _run_connector_free_reindex(
        temporal,
        configuration,
        stores,
        task_id=first_id,
    )

    return {
        "temporal": {
            "status": first_status,
            "history_events": history_events,
            "history_bytes": history_bytes,
            "maximum_event_bytes": maximum_event_bytes,
            "first": asdict(first),
            "unchanged_replay": asdict(replay),
        },
        "redis_flush": redis_flush,
        "postgres_minio_qdrant_falkor": _store_report(stores),
        "retrieval": retrieval,
        "connector_free_reindex": reindex,
    }


async def _flush_disposable_redis(settings) -> dict[str, int]:
    redis_url = settings.redis_url
    if redis_url is None:
        raise AssertionError("smoke Redis URL is not configured")
    client = Redis.from_url(
        redis_url.get_secret_value(),
        socket_connect_timeout=settings.redis_socket_timeout_seconds,
        socket_timeout=settings.redis_socket_timeout_seconds,
    )
    try:
        await client.set(
            f"harborrag:smoke:flush-probe:{uuid4().hex}",
            "disposable",
            ex=60,
        )
        removed_keys = await client.dbsize()
        await client.flushall()
        remaining_keys = await client.dbsize()
    finally:
        await client.aclose()
    if remaining_keys:
        raise AssertionError("Redis FLUSHALL left disposable keys behind")
    if removed_keys < 1:
        raise AssertionError("Redis FLUSHALL did not remove the probe key")
    return {
        "removed_keys": removed_keys,
        "remaining_keys": remaining_keys,
    }


async def _run_connector_free_reindex(
    temporal: IngestionTemporalClient,
    configuration,
    stores: StoreObservation,
    *,
    task_id: str,
) -> dict[str, object]:
    target = replace(
        configuration.processing,
        graph_projection_version=(f"{configuration.processing.graph_projection_version}-reindex"),
    )
    jobs = []
    for index, document_id in enumerate(stores.document_ids):
        reindex_job_id = f"reindex-smoke-{uuid4().hex}-{index}"
        handle = await temporal.start_reindex(
            ReindexInput(
                reindex_job_id=reindex_job_id,
                tenant_id=TENANT_ID,
                processing=target,
                document_id=document_id,
                limit=1,
            )
        )
        result = await handle.result()
        if (
            result.status != "COMPLETED"
            or result.connector_call_count != 0
            or result.scanned_count != 1
            or result.processed_count != 1
            or result.published_count != 1
            or result.skipped_count != 0
            or result.failure_count != 0
        ):
            raise AssertionError("connector-free reindex returned invalid durable progress")
        history = await handle.fetch_history()
        jobs.append(
            {
                "status": result.status,
                "connector_call_count": result.connector_call_count,
                "published_count": result.published_count,
                "history_events": len(history.events),
                "history_bytes": sum(event.ByteSize() for event in history.events),
            }
        )
    reindexed = await inspect_stores(
        configuration.settings,
        tenant_id=TENANT_ID,
        task_id=task_id,
    )
    if reindexed.versions == stores.versions:
        raise AssertionError("reindex did not publish new active versions")
    if _graph_shapes(reindexed) != _graph_shapes(stores):
        raise AssertionError("reindex changed the active structural or source relation graph")
    retrieval = await _retrieval_observations(configuration.settings)
    return {
        "jobs": jobs,
        "previous_versions": stores.versions,
        "active_versions": reindexed.versions,
        "graph_traversals": [asdict(graph) for graph in reindexed.graphs],
        "retrieval": retrieval,
    }


def _graph_shapes(
    stores: StoreObservation,
) -> dict[str, tuple[int, int, tuple[str, ...]]]:
    return {
        graph.document_id: (
            graph.nodes,
            graph.relations,
            graph.relation_types,
        )
        for graph in stores.graphs
    }


async def _retrieval_observations(
    settings,
) -> dict[str, object]:
    service = await RuntimeRetrievalService.connect(settings)
    try:
        checks = (
            (
                RetrievalLane.DENSE,
                "Which database decides whether a document version is published?",
                "publication authority",
            ),
            (
                RetrievalLane.SPARSE,
                "ingestion activity timeout exactly 30 seconds",
                "30 seconds",
            ),
            (
                RetrievalLane.HYBRID,
                "HARBOR-4242 immutable artifacts",
                "HARBOR-4242",
            ),
        )
        observations: dict[str, object] = {}
        for lane, query, expected in checks:
            report = await service.retrieve(
                query,
                tenant_id=TENANT_ID,
                top_k=4,
                options=RetrievalOptions(
                    lane=lane,
                    observe_graph=True,
                ),
            )
            if not report.results:
                raise AssertionError(f"{lane.value} retrieval returned no active result")
            if not any(expected.casefold() in result.text.casefold() for result in report.results):
                raise AssertionError(f"{lane.value} retrieval missed expected evidence: {expected}")
            observations[lane.value] = {
                "results": [
                    {
                        "score": round(result.score, 6),
                        "record_kind": result.metadata["record_kind"],
                        "chunk_kind": result.metadata["chunk_kind"],
                        "preview": result.text[:220],
                    }
                    for result in report.results
                ],
                "diagnostics": asdict(report.diagnostics),
            }
        return observations
    finally:
        await service.aclose()


def _assert_balanced_success(result: SourceIngestionResult) -> None:
    if result.discovered != 2:
        raise AssertionError(f"smoke discovery expected 2 documents, found {result.discovered}")
    if result.failed:
        raise AssertionError(f"smoke ingestion failed {result.failed} document(s)")
    if result.unresolved_relations:
        raise AssertionError("smoke ingestion left unresolved source relations")
    if result.published + result.unchanged != result.discovered:
        raise AssertionError("smoke result counters do not balance")


def _assert_first_publication(result: SourceIngestionResult) -> None:
    if result.published != result.discovered:
        raise AssertionError("the first smoke run did not publish every document")


def _store_report(observation: StoreObservation) -> dict[str, object]:
    return {
        "documents": observation.documents,
        "document_ids": observation.document_ids,
        "active_versions": observation.versions,
        "artifact_keys": observation.artifact_keys,
        "evidence_chunks": observation.evidence_chunks,
        "chunks": [
            {
                "collection": chunk.collection,
                "chunk_id": chunk.chunk_id,
                "chunk_kind": chunk.chunk_kind,
                "dense_dimensions": chunk.dense_dimensions,
                "sparse_terms": chunk.sparse_terms,
                "content_preview": chunk.content[:300],
                "payload_fields": chunk.payload_fields,
                "citation_fields": chunk.citation_fields,
            }
            for chunk in observation.chunks
        ],
        "graph_traversals": [asdict(graph) for graph in observation.graphs],
    }


def main() -> int:
    try:
        report = asyncio.run(run_smoke())
    except Exception as error:
        print(f"Ingestion smoke failed: {type(error).__name__}")
        return 1
    print(json.dumps(report, indent=2, sort_keys=True))
    print("Ingestion smoke passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
