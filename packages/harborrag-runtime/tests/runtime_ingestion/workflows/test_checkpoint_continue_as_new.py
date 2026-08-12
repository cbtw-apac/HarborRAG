"""Continue-as-new behavior for source ingestion checkpoints."""

from __future__ import annotations

import asyncio
from dataclasses import replace

import pytest

from harborrag_runtime.temporal.schemas import (
    DocumentDispatchSummary,
    SourceDiscoveryResult,
)
from harborrag_runtime.temporal.source_workflow import SourceIngestionWorkflow

from .fixtures import plan_reference as _plan_reference
from .fixtures import source_input as _source


class _WorkflowInfoStub:
    def __init__(self, *, suggested: bool = False) -> None:
        self._suggested = suggested

    def is_continue_as_new_suggested(self) -> bool:
        return self._suggested


class _ChildHandle:
    def __init__(self, awaitable) -> None:
        self._task = asyncio.create_task(awaitable)

    def __await__(self):
        return self._task.__await__()

    async def signal(self, name: str) -> None:
        del name


def _start_child(child):
    async def start(name, request, **options):
        return _ChildHandle(child(name, request, **options))

    return start


@pytest.fixture(autouse=True)
def _workflow_test_shims(monkeypatch):
    async def wait_condition(predicate):
        while not predicate():
            await asyncio.sleep(3600)

    monkeypatch.setattr(
        "harborrag_runtime.temporal.source_workflow.workflow.wait_condition",
        wait_condition,
    )
    monkeypatch.setattr(
        "harborrag_runtime.temporal.source_workflow.workflow.info",
        lambda: _WorkflowInfoStub(suggested=False),
    )


@pytest.mark.asyncio
async def test_continuation_preserves_summary_state(monkeypatch) -> None:
    """Workflow summary should carry across continue-as-new."""
    source = replace(
        _source(),
        batch_size=1,
        continue_after_batches=1,
    )
    plan = _plan_reference()
    continued = []

    async def execute_activity(name, request, **options):
        if name == "harborrag.discover_source_items":
            return SourceDiscoveryResult(
                scan_id="scan-1",
                plan_reference=plan,
                document_count=3,
            )
        return request

    async def child(name, request, **options):
        return DocumentDispatchSummary(published=1, unchanged=1, failed=0)

    def continue_as_new(request):
        continued.append(request)
        raise RuntimeError("continued")

    monkeypatch.setattr(
        "harborrag_runtime.temporal.source_workflow.workflow.execute_activity",
        execute_activity,
    )
    monkeypatch.setattr(
        "harborrag_runtime.temporal.source_workflow.workflow.start_child_workflow",
        _start_child(child),
    )
    monkeypatch.setattr(
        "harborrag_runtime.temporal.source_workflow.workflow.continue_as_new",
        continue_as_new,
    )

    with pytest.raises(RuntimeError, match="continued"):
        await SourceIngestionWorkflow().run(source)

    # Verify summary was preserved in continuation
    assert len(continued) == 1
    continuation = continued[0].continuation
    assert continuation.summary.published == 1
    assert continuation.summary.unchanged == 1
    assert continuation.summary.failed == 0


@pytest.mark.asyncio
async def test_source_workflow_continue_as_new_when_sdk_suggests(monkeypatch) -> None:
    """SDK history suggestion should also trigger continue-as-new."""
    source = replace(
        _source(),
        batch_size=1,
        continue_after_batches=100,
    )
    plan = _plan_reference()
    continued = []

    async def execute_activity(name, request, **options):
        if name == "harborrag.discover_source_items":
            return SourceDiscoveryResult(
                scan_id="scan-1",
                plan_reference=plan,
                document_count=3,
            )
        return request

    async def child(name, request, **options):
        assert name == "harborrag.source_batch"
        return DocumentDispatchSummary(published=1)

    def continue_as_new(request):
        continued.append(request)
        raise RuntimeError("continued")

    monkeypatch.setattr(
        "harborrag_runtime.temporal.source_workflow.workflow.execute_activity",
        execute_activity,
    )
    monkeypatch.setattr(
        "harborrag_runtime.temporal.source_workflow.workflow.start_child_workflow",
        _start_child(child),
    )
    monkeypatch.setattr(
        "harborrag_runtime.temporal.source_workflow.workflow.info",
        lambda: _WorkflowInfoStub(suggested=True),
    )
    monkeypatch.setattr(
        "harborrag_runtime.temporal.source_workflow.workflow.continue_as_new",
        continue_as_new,
    )

    with pytest.raises(RuntimeError, match="continued"):
        await SourceIngestionWorkflow().run(source)

    assert len(continued) == 1
    continuation = continued[0].continuation
    assert continuation is not None
    assert continuation.next_document_index == 1
    assert continuation.batch_number == 1
