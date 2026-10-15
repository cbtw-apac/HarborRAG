"""The production workflow sandbox runner decodes real workflow inputs."""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import AsyncIterator
from types import ModuleType
from typing import Any

import pytest
import pytest_asyncio
from temporalio.api.enums.v1 import EventType
from temporalio.client import WorkflowHandle
from temporalio.testing import WorkflowEnvironment
from temporalio.worker import Worker

from harborrag_runtime.temporal.document_workflow import DocumentIngestionWorkflow
from harborrag_runtime.temporal.sandbox import workflow_sandbox_runner
from harborrag_runtime.temporal.schemas import DocumentIngestionInput
from harborrag_runtime.temporal.source_workflow import SourceIngestionWorkflow

from .test_client import _source
from .test_ingestion_activities import _artifact

pytestmark = pytest.mark.graybox


def test_runner_keeps_workflow_modules_and_their_parents_sandboxed() -> None:
    loaded = {
        name: ModuleType(name)
        for name in (
            "harborrag_core",
            "harborrag_core.ingestion",
            "harborrag_runtime",
            "harborrag_runtime.ingestion_contracts",
            "harborrag_runtime.temporal",
            "harborrag_runtime.temporal.schemas",
            "harborrag_runtime.temporal.document_workflow",
            "pydantic",
        )
    }

    runner = workflow_sandbox_runner([DocumentIngestionWorkflow], loaded_modules=loaded)

    assert runner.restrictions.passthrough_modules >= {
        "harborrag_core",
        "harborrag_core.ingestion",
        "harborrag_runtime.ingestion_contracts",
        "harborrag_runtime.temporal.schemas",
    }
    for sandboxed in (
        "harborrag_runtime",
        "harborrag_runtime.temporal",
        "harborrag_runtime.temporal.document_workflow",
    ):
        assert sandboxed not in runner.restrictions.passthrough_modules


@pytest_asyncio.fixture
async def environment() -> AsyncIterator[WorkflowEnvironment]:
    try:
        env = await WorkflowEnvironment.start_time_skipping()
    except Exception as error:  # pragma: no cover - depends on the test-server download
        pytest.skip(f"Temporal test server unavailable: {error}")
    try:
        yield env
    finally:
        await env.shutdown()


async def _first_workflow_task(handle: WorkflowHandle[Any, Any]) -> EventType.ValueType:
    for _ in range(100):
        for event in (await handle.fetch_history()).events:
            if event.event_type in (
                EventType.EVENT_TYPE_WORKFLOW_TASK_COMPLETED,
                EventType.EVENT_TYPE_WORKFLOW_TASK_FAILED,
            ):
                return event.event_type
        await asyncio.sleep(0.1)
    raise AssertionError("the first workflow task never finished")


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("workflow_type", "request_factory"),
    [
        # SourceIngestionInput inherits fields annotated in a module no workflow
        # imports by name; decoding it is what in-module passthrough broke.
        (SourceIngestionWorkflow, _source),
        (
            DocumentIngestionWorkflow,
            lambda: DocumentIngestionInput("task-1", "tenant-1", "jira-main", _artifact(), 0),
        ),
    ],
)
async def test_production_runner_decodes_workflow_input_inside_the_sandbox(
    environment: WorkflowEnvironment,
    workflow_type: type,
    request_factory: Any,
) -> None:
    task_queue = f"sandbox-{uuid.uuid4()}"
    async with Worker(
        environment.client,
        task_queue=task_queue,
        workflows=[workflow_type],
        workflow_runner=workflow_sandbox_runner([workflow_type]),
    ):
        handle = await environment.client.start_workflow(
            workflow_type.run,  # type: ignore[attr-defined]
            request_factory(),
            id=str(uuid.uuid4()),
            task_queue=task_queue,
        )
        try:
            # The workflow only gets as far as scheduling its first activity, which
            # no worker here serves; a completed first task means the input decoded.
            assert (
                await _first_workflow_task(handle) == EventType.EVENT_TYPE_WORKFLOW_TASK_COMPLETED
            )
        finally:
            await handle.terminate()
