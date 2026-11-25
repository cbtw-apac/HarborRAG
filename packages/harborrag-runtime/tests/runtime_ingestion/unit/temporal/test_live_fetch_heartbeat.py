"""A fetch heartbeats before it touches storage, so a stalled store fails as itself."""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import replace
from datetime import timedelta
from types import SimpleNamespace
from typing import Any, cast

import pytest
from temporalio.testing import ActivityEnvironment

from harborrag_core.ingestion import SourceAdmissionDecision
from harborrag_engine.ingestion.release.stage_models import RawCaptureStageResult
from harborrag_runtime.temporal.ingestion_activities import IngestionActivities
from harborrag_runtime.temporal.schemas import DocumentIngestionInput, WorkflowArtifactReference

pytestmark = pytest.mark.unit

_PLAN = WorkflowArtifactReference(
    bucket="artifacts",
    key="source-plans/task-1.json",
    sha256="a" * 64,
    byte_size=10,
    media_type="application/json",
)


class _Observability:
    @contextmanager
    def boundary(self, stage: str) -> Iterator[None]:
        del stage
        yield

    def record_capture(self, *args: object) -> None:
        del args


@pytest.mark.asyncio
async def test_fetch_heartbeats_before_the_plan_lookup_and_the_connector() -> None:
    heartbeats: list[object] = []
    heartbeats_at: dict[str, int] = {}

    async def plan_lookup(request: object) -> object:
        del request
        heartbeats_at["plan"] = len(heartbeats)
        return SimpleNamespace(
            request=SimpleNamespace(
                configuration_fingerprint="config-v1",
                source_identity=SimpleNamespace(connector_type=SimpleNamespace(value="local")),
            )
        )

    async def connector(**kwargs: object) -> object:
        del kwargs
        heartbeats_at["connector"] = len(heartbeats)
        return object()

    async def fetch_and_capture(request: object, connector: object) -> RawCaptureStageResult:
        del request, connector
        return RawCaptureStageResult(
            document_id="doc-1",
            document_version_id="version-1",
            decision=SourceAdmissionDecision.UNCHANGED,
        )

    runtime = SimpleNamespace(
        connector=connector,
        source_plans=None,
        stages=SimpleNamespace(preparation=SimpleNamespace(fetch_and_capture=fetch_and_capture)),
    )
    activities = IngestionActivities(cast(Any, runtime))
    activities._observability = cast(Any, _Observability())
    activities._documents = cast(Any, SimpleNamespace(get=plan_lookup))
    environment = ActivityEnvironment()
    environment.info = replace(
        environment.info,
        start_to_close_timeout=timedelta(minutes=60),
        heartbeat_timeout=timedelta(minutes=2),
    )
    environment.on_heartbeat = lambda *details: heartbeats.append(details)

    result = await environment.run(
        activities.fetch_and_capture_raw,
        DocumentIngestionInput("task-1", "tenant-1", "local-docs", _PLAN, 0),
    )

    assert result.document_version_id == "version-1"
    assert heartbeats_at == {"plan": 1, "connector": 1}
