"""The cancel activity returns every skip of the run, at discovery and per document."""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import pytest

from harborrag_core.ingestion import IngestionTaskState
from harborrag_core.ingestion.connector_contracts import ConnectorSkip
from harborrag_runtime.ingestion import SourceIngestionService
from harborrag_runtime.temporal.ingestion_activities import IngestionActivities
from harborrag_runtime.temporal.schemas import SourceCancellationInput

from ...fixtures.connectors import DescriptorConnector
from ...fixtures.release import (
    build_control_plane,
    build_release_resources,
    build_release_service,
    source_request,
)

pytestmark = pytest.mark.unit


class _SkippingConnector(DescriptorConnector):
    """A connector whose discovery left one file out for its extension."""

    @property
    def skipped(self) -> tuple[ConnectorSkip, ...]:
        """The file the extension filters dropped."""
        return (
            ConnectorSkip(
                path="notes.txt",
                reason="unsupported_extension",
                detail="extension '.txt' is not allowed",
            ),
        )


class _Observability:
    @contextmanager
    def boundary(self, stage: str) -> Iterator[None]:
        del stage
        yield


@pytest.mark.asyncio
async def test_cancel_returns_discovery_and_document_skips(tmp_path: Path) -> None:
    control = build_control_plane(tmp_path)
    resources = build_release_resources(control)
    async with control, resources.store:
        sources = SourceIngestionService(
            control=control, documents=build_release_service(resources)
        )
        discovery = await sources.discover(source_request("task-cancel"), _SkippingConnector())
        await sources.record_failed_document(
            "task-cancel",
            discovery.planned[0],
            error_type="source_forbidden",
            skip_reason="ACCESS_DENIED",
        )
        runtime = SimpleNamespace(sources=sources, control=control, source_plans=None)
        activities = IngestionActivities(cast(Any, runtime))
        activities._observability = cast(Any, _Observability())

        result = await activities.cancel_source_ingestion(SourceCancellationInput("task-cancel"))
        task = await control.tasks.get("task-cancel")

    assert result.skipped_by_reason == {"ACCESS_DENIED": 1, "unsupported_extension": 1}
    assert task is not None
    assert task.status is IngestionTaskState.CANCELLED
