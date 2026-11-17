"""The per-document plan JSON carries the run's pinned settings."""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import replace

import pytest

from harborrag_adapters.repositories.object_store import (
    ImmutableArtifactReader,
    ImmutableArtifactWriter,
    MemoryObjectStore,
)
from harborrag_core.schemas.storage import StorageOperationContext
from harborrag_runtime.ingestion.source.plan import SourcePlanRepository
from harborrag_runtime.ingestion_contracts import RunSettings
from runtime_ingestion.unit.source.test_plan import _planned

pytestmark = pytest.mark.unit

CONTEXT = StorageOperationContext.system(tenant_id="tenant-1")


@asynccontextmanager
async def _repository() -> AsyncIterator[SourcePlanRepository]:
    store = MemoryObjectStore()
    async with store:
        yield SourcePlanRepository(ImmutableArtifactWriter(store), ImmutableArtifactReader(store))


@pytest.mark.asyncio
async def test_plan_round_trip_keeps_run_settings() -> None:
    run = RunSettings("rev_4", pdf_parser="docling", chunk_max_tokens=256, document_concurrency=2)
    planned = _planned()
    planned = replace(planned, request=replace(planned.request, run_settings=run))
    async with _repository() as repository:
        reference = await repository.put(
            task_id="task-1", scan_id="scan-1", planned=(planned,), context=CONTEXT
        )
        (loaded,) = await repository.get(reference, context=CONTEXT)

    assert loaded.request.run_settings == run


@pytest.mark.asyncio
async def test_plan_without_run_settings_loads_none() -> None:
    async with _repository() as repository:
        reference = await repository.put(
            task_id="task-1", scan_id="scan-1", planned=(_planned(),), context=CONTEXT
        )
        payload = json.loads(await repository._reader.get(reference, context=CONTEXT))
    payload[0]["request"].pop("run_settings", None)

    (loaded,) = tuple(repository._load(item) for item in payload)

    assert loaded.request.run_settings is None
