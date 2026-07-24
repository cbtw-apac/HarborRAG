from __future__ import annotations

import os
from dataclasses import replace

import pytest
from temporalio import activity
from temporalio.testing import WorkflowEnvironment
from temporalio.worker import Worker

from harborrag_runtime.temporal.schemas import (
    ArtifactActivityInput,
    ArtifactActivityResult,
    ArtifactReference,
    ArtifactStage,
    ArtifactStatus,
    DiscoveryInput,
    DiscoveryResult,
    IngestionRunInput,
    IngestionSummary,
    ReconciliationInput,
    ReconciliationResult,
    RunStatus,
)
from harborrag_runtime.temporal.workflows import IngestionRunWorkflow


@activity.defn(name="harborrag.discover_artifacts")
async def _empty_discovery(request: DiscoveryInput) -> DiscoveryResult:
    return DiscoveryResult((), None, "checkpoint://empty", True)


@activity.defn(name="harborrag.reconcile_ingestion")
async def _reconcile(request: ReconciliationInput) -> ReconciliationResult:
    return ReconciliationResult("reconcile://empty", RunStatus.COMPLETED)


@activity.defn(name="harborrag.preflight_artifact")
async def _preflight(request: ArtifactActivityInput) -> ArtifactActivityResult:
    state = replace(
        request.state,
        stage=ArtifactStage.FETCH,
        artifact_revision_id="revision-1",
    )
    return ArtifactActivityResult(ArtifactStatus.RUNNING, state)


@activity.defn(name="harborrag.fetch_artifact")
async def _fetch(request: ArtifactActivityInput) -> ArtifactActivityResult:
    state = replace(request.state, stage=ArtifactStage.PARSE, snapshot_ref="snapshot://1")
    return ArtifactActivityResult(ArtifactStatus.RUNNING, state)


@activity.defn(name="harborrag.parse_artifact")
async def _parse(request: ArtifactActivityInput) -> ArtifactActivityResult:
    state = replace(
        request.state,
        stage=ArtifactStage.CHUNK,
        parsed_document_ref="parsed://1",
    )
    return ArtifactActivityResult(ArtifactStatus.RUNNING, state)


@activity.defn(name="harborrag.chunk_artifact")
async def _chunk(request: ArtifactActivityInput) -> ArtifactActivityResult:
    state = replace(
        request.state,
        stage=ArtifactStage.INDEX,
        chunking_result_ref="chunks://1",
    )
    return ArtifactActivityResult(ArtifactStatus.RUNNING, state)


@activity.defn(name="harborrag.index_artifact")
async def _index(request: ArtifactActivityInput) -> ArtifactActivityResult:
    state = replace(
        request.state,
        stage=ArtifactStage.VALIDATE,
        indexing_result_ref="index://1",
    )
    return ArtifactActivityResult(ArtifactStatus.RUNNING, state)


@activity.defn(name="harborrag.validate_artifact")
async def _validate(request: ArtifactActivityInput) -> ArtifactActivityResult:
    state = replace(request.state, stage=ArtifactStage.FINALIZE)
    return ArtifactActivityResult(ArtifactStatus.RUNNING, state)


@activity.defn(name="harborrag.finalize_artifact")
async def _finalize(request: ArtifactActivityInput) -> ArtifactActivityResult:
    return ArtifactActivityResult(ArtifactStatus.SUCCEEDED, request.state)


@activity.defn(name="harborrag.discover_artifacts")
async def _one_artifact(request: DiscoveryInput) -> DiscoveryResult:
    artifact = ArtifactReference(
        artifact_id="artifact-1",
        source_ref="source://1",
        source_kind="local",
        connector_name="local",
    )
    return DiscoveryResult((artifact,), None, "checkpoint://one", True)


@pytest.mark.asyncio
@pytest.mark.integration
async def test_empty_ingestion_on_temporal_test_server() -> None:
    """Exercise real replay/serialization when a test-server binary is supplied."""

    server = os.environ.get("HARBORRAG_TEMPORAL_TEST_SERVER")
    if server is None:
        pytest.skip("HARBORRAG_TEMPORAL_TEST_SERVER is not configured")
    async with await WorkflowEnvironment.start_time_skipping(
        test_server_existing_path=server
    ) as environment:
        request = IngestionRunInput(
            run_id="smoke-run",
            tenant_id="tenant-1",
            connector_name="local",
            manifest_id="manifest-1",
            generation_id="generation-1",
        )
        queues = request.options.task_queues
        async with (
            Worker(
                environment.client,
                task_queue=queues.discovery,
                workflows=(IngestionRunWorkflow,),
                activities=(_empty_discovery,),
            ),
            Worker(
                environment.client,
                task_queue=queues.maintenance,
                activities=(_reconcile,),
            ),
        ):
            result = await environment.client.execute_workflow(
                "harborrag.ingestion_run",
                request,
                id="smoke-empty-ingestion",
                task_queue=queues.discovery,
                result_type=IngestionSummary,
            )

    assert result.status is RunStatus.COMPLETED
    assert result.progress.discovered == 0


@pytest.mark.asyncio
@pytest.mark.integration
async def test_artifact_hierarchy_on_temporal_test_server() -> None:
    server = os.environ.get("HARBORRAG_TEMPORAL_TEST_SERVER")
    if server is None:
        pytest.skip("HARBORRAG_TEMPORAL_TEST_SERVER is not configured")
    from harborrag_runtime.temporal.workflows import (
        ArtifactIngestionWorkflow,
        IngestionPartitionWorkflow,
    )

    async with await WorkflowEnvironment.start_time_skipping(
        test_server_existing_path=server
    ) as environment:
        request = IngestionRunInput(
            run_id="artifact-smoke-run",
            tenant_id="tenant-1",
            connector_name="local",
            manifest_id="manifest-1",
            generation_id="generation-1",
        )
        queues = request.options.task_queues
        async with (
            Worker(
                environment.client,
                task_queue=queues.discovery,
                workflows=(IngestionRunWorkflow,),
                activities=(_one_artifact, _preflight),
            ),
            Worker(
                environment.client,
                task_queue=queues.chunking,
                workflows=(IngestionPartitionWorkflow, ArtifactIngestionWorkflow),
                activities=(_chunk,),
            ),
            Worker(
                environment.client,
                task_queue=queues.connectors,
                activities=(_fetch,),
            ),
            Worker(
                environment.client,
                task_queue=queues.parsers,
                activities=(_parse,),
            ),
            Worker(
                environment.client,
                task_queue=queues.vector_index,
                activities=(_index,),
            ),
            Worker(
                environment.client,
                task_queue=queues.graph_index,
                activities=(_validate,),
            ),
            Worker(
                environment.client,
                task_queue=queues.maintenance,
                activities=(_finalize, _reconcile),
            ),
        ):
            result = await environment.client.execute_workflow(
                "harborrag.ingestion_run",
                request,
                id="smoke-artifact-ingestion",
                task_queue=queues.discovery,
                result_type=IngestionSummary,
            )

    assert result.status is RunStatus.COMPLETED
    assert result.progress.discovered == 1
    assert result.progress.succeeded == 1
    assert result.progress.partitions == 1
