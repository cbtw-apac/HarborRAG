"""Source-level activities log the source's tenant, scope and connector type on each line."""

from __future__ import annotations

import logging
from dataclasses import replace
from datetime import timedelta
from types import SimpleNamespace
from typing import Any, cast

import pytest
from temporalio.exceptions import ApplicationError
from temporalio.testing import ActivityEnvironment

from harborrag_core.contracts.errors import HarborConflictError
from harborrag_core.ingestion import IngestionTaskState
from harborrag_core.ingestion.source_query import ProcessingProfileInput
from harborrag_core.observability.log_events import log_event
from harborrag_engine.ingestion.source.models import SourceIngestionOutcome
from harborrag_runtime.ingestion.connections import ConnectionHealth
from harborrag_runtime.temporal.dispatch import DocumentDispatchSummary
from harborrag_runtime.temporal.ingestion_activities import IngestionActivities
from harborrag_runtime.temporal.schemas import (
    SourceFinalizationInput,
    SourceIngestionInput,
    WorkflowArtifactReference,
)

pytestmark = pytest.mark.unit

_TASK = "task-log-context"
_SOURCE_FIELDS = {
    "tenant_id": "tenant-a",
    "source_scope_id": "scope-a",
    "connector_type": "local",
}
_SERVICE_LOGGER = logging.getLogger("harborrag.runtime.ingestion.source.finalization")


class _FinishingSources:
    """Finishes a run the way the source service does, logging its own finalization line."""

    async def finish(self, source: object, **kwargs: object) -> SourceIngestionOutcome:
        del source, kwargs
        log_event(_SERVICE_LOGGER, "ingestion.finalization_completed", task_id=_TASK)
        return SourceIngestionOutcome(
            task_id=_TASK,
            scan_id="scan-1",
            discovered=2,
            published=1,
            unchanged=1,
            failed=0,
            status=IngestionTaskState.COMPLETED,
        )


class _CancelledSources:
    async def is_cancelled(self, task_id: str) -> bool:
        del task_id
        return True


class _Plans:
    async def find(self, **kwargs: object) -> None:
        del kwargs

    async def documents(self, reference: object, **kwargs: object) -> tuple[object, ...]:
        del reference, kwargs
        return ()


async def _connection_being_removed(**kwargs: object) -> object:
    del kwargs
    raise HarborConflictError("connection is being removed")


def _source() -> SourceIngestionInput:
    return SourceIngestionInput(
        task_id=_TASK,
        tenant_id=_SOURCE_FIELDS["tenant_id"],
        connector_name="local-docs",
        connector_type=_SOURCE_FIELDS["connector_type"],
        connection_id="local-docs",
        source_scope_id=_SOURCE_FIELDS["source_scope_id"],
        configuration_fingerprint="local-config-v1",
        processing=ProcessingProfileInput(
            parser_profile="text-v1",
            normalizer_version="canonical-v1",
            chunk_strategy="route-evidence-v3",
            dense_encoder_profile="dense-v1",
            sparse_encoder_profile="bm25-v1",
            graph_projection_version="graph-v1",
        ),
    )


def _activities(sources: object) -> IngestionActivities:
    runtime = SimpleNamespace(
        sources=sources,
        source_plans=_Plans(),
        connector=_connection_being_removed,
        connection_health=ConnectionHealth(None),
    )
    return IngestionActivities(cast(Any, runtime))


def _environment() -> ActivityEnvironment:
    environment = ActivityEnvironment()
    environment.info = replace(
        environment.info,
        start_to_close_timeout=timedelta(hours=1),
        heartbeat_timeout=timedelta(minutes=2),
    )
    return environment


def _source_fields(caplog: pytest.LogCaptureFixture, event: str) -> dict[str, object]:
    [record] = [record for record in caplog.records if record.__dict__.get("event") == event]
    return {name: record.__dict__.get(name) for name in _SOURCE_FIELDS}


@pytest.mark.asyncio
async def test_finalization_lines_carry_the_source_fields(
    caplog: pytest.LogCaptureFixture,
) -> None:
    request = SourceFinalizationInput(
        source=_source(),
        scan_id="scan-1",
        plan_reference=WorkflowArtifactReference(
            bucket="artifacts",
            key="plans/task.json",
            sha256="a" * 64,
            byte_size=10,
            media_type="application/json",
        ),
        summary=DocumentDispatchSummary(published=1, unchanged=1),
    )

    with caplog.at_level(logging.INFO):
        await _environment().run(
            _activities(_FinishingSources()).finalize_source_ingestion, request
        )

    assert _source_fields(caplog, "ingestion.source_finalized") == _SOURCE_FIELDS
    assert _source_fields(caplog, "ingestion.finalization_completed") == _SOURCE_FIELDS


@pytest.mark.asyncio
async def test_discovery_lines_carry_the_source_fields(caplog: pytest.LogCaptureFixture) -> None:
    with caplog.at_level(logging.INFO), pytest.raises(ApplicationError):
        await _environment().run(_activities(_CancelledSources()).discover_source_items, _source())

    assert _source_fields(caplog, "ingestion.discovery_started") == _SOURCE_FIELDS
    assert _source_fields(caplog, "ingestion.discovery_stopped") == _SOURCE_FIELDS
