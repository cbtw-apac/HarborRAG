"""Each per-document activity heartbeats before its plan lookup, so a stalled store fails as itself."""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import replace
from datetime import timedelta
from types import SimpleNamespace
from typing import Any, cast

import pytest
from temporalio.testing import ActivityEnvironment

from harborrag_core.ingestion import DocumentIngestionOutcome, SourceAdmissionDecision
from harborrag_engine.ingestion.release.stage_models import PreparedDocumentStage
from harborrag_runtime.document_stage_catalog import DOCUMENT_STAGE_CATALOG
from harborrag_runtime.temporal.ingestion_activities import IngestionActivities
from harborrag_runtime.temporal.schemas import (
    DocumentIngestionInput,
    PreparedDocument,
    RawCaptureResult,
    RetryDocumentInput,
    WorkflowArtifactReference,
)

pytestmark = pytest.mark.unit

_PLAN = WorkflowArtifactReference(
    bucket="artifacts",
    key="source-plans/task-1.json",
    sha256="a" * 64,
    byte_size=10,
    media_type="application/json",
)
_DOCUMENT = DocumentIngestionInput("task-1", "tenant-1", "local-docs", _PLAN, 0)
_CAPTURED = RawCaptureResult(_DOCUMENT, "doc-1", "version-1", SourceAdmissionDecision.UNCHANGED)
_PREPARED = PreparedDocument(_DOCUMENT, "doc-1", "version-1", SourceAdmissionDecision.NEW)
_RETRY = RetryDocumentInput("retry-1", "task-1", "tenant-1", _PLAN, 0)
_PARSE_DETAIL = {
    "stage": "parse-and-normalize",
    "task_id": "task-1",
    "document_id": "doc-1",
    "document_index": 0,
    "mode": "in-process",
    "resumed": False,
    "prior_attempt_count": 0,
}
_CASES = [
    pytest.param("parse_and_normalize", _CAPTURED, _PARSE_DETAIL, id="parse_and_normalize"),
    *(
        pytest.param(
            stage.method_name,
            _PREPARED,
            stage.method_name.replace("_", "-"),
            id=stage.method_name,
        )
        for stage in DOCUMENT_STAGE_CATALOG
    ),
    pytest.param(
        "retry_document_release", _RETRY, "retry-document-release", id="retry_document_release"
    ),
]


class _Observability:
    @contextmanager
    def boundary(self, stage: str) -> Iterator[None]:
        del stage
        yield

    def record_prepared(self, *args: object) -> None:
        del args

    def record_chunking(self, *args: object) -> None:
        del args


async def _parsed(request: object, capture: object) -> PreparedDocumentStage:
    del request, capture
    return PreparedDocumentStage(
        document_id="doc-1",
        document_version_id="version-1",
        decision=SourceAdmissionDecision.UNCHANGED,
    )


async def _staged(request: object, prepared: object) -> None:
    del request, prepared


async def _retried(**kwargs: object) -> DocumentIngestionOutcome:
    del kwargs
    return DocumentIngestionOutcome.PUBLISHED


def _runtime() -> Any:
    stages = {stage.method_name: _staged for stage in DOCUMENT_STAGE_CATALOG}
    return SimpleNamespace(
        source_plans=None,
        stages=SimpleNamespace(
            preparation=SimpleNamespace(parse_and_normalize=_parsed, **stages),
            projections=SimpleNamespace(**stages),
        ),
        sources=SimpleNamespace(retry_one=_retried),
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(("activity_name", "payload", "detail"), _CASES)
async def test_document_activity_heartbeats_before_its_plan_lookup(
    activity_name: str, payload: object, detail: object
) -> None:
    heartbeats: list[tuple[object, ...]] = []
    heartbeats_at_lookup: list[int] = []

    async def plan_lookup(request: object) -> object:
        del request
        heartbeats_at_lookup.append(len(heartbeats))
        return SimpleNamespace(request=SimpleNamespace(configuration_fingerprint="config-v1"))

    activities = IngestionActivities(cast(Any, _runtime()))
    activities._observability = cast(Any, _Observability())
    activities._documents = cast(Any, SimpleNamespace(get=plan_lookup))
    environment = ActivityEnvironment()
    environment.info = replace(
        environment.info,
        start_to_close_timeout=timedelta(minutes=60),
        heartbeat_timeout=timedelta(minutes=2),
    )
    environment.on_heartbeat = lambda *details: heartbeats.append(details)

    await environment.run(getattr(activities, activity_name), payload)

    assert heartbeats_at_lookup == [1]
    assert heartbeats[0] == (detail,)
