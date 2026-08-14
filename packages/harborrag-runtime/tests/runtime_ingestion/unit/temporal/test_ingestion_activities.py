"""Unit coverage for the thin Temporal document and retry activity boundaries."""

from __future__ import annotations

from contextlib import contextmanager
from types import SimpleNamespace
from typing import Any, cast

import pytest
from temporalio.exceptions import ApplicationError

from harborrag_core.ingestion import DocumentIngestionOutcome
from harborrag_runtime.temporal import ingestion_activities as activity_module
from harborrag_runtime.temporal import retry_activities as retry_module
from harborrag_runtime.temporal.dispatch import DocumentDispatchSummary
from harborrag_runtime.temporal.ingestion_activities import IngestionActivities
from harborrag_runtime.temporal.plan_resolver import PlanDocumentResolver
from harborrag_runtime.temporal.schemas import (
    DocumentFailureInput,
    DocumentIngestionInput,
    PreparedDocument,
    RawCaptureResult,
    RetryDocumentFailureInput,
    RetryDocumentInput,
    RetryFailuresInput,
    RetryFinalizationInput,
    RetryTaskFailureInput,
    WorkflowArtifactReference,
)

pytestmark = pytest.mark.whitebox


def _artifact() -> WorkflowArtifactReference:
    return WorkflowArtifactReference(
        bucket="artifacts",
        key="plans/task.json",
        sha256="a" * 64,
        byte_size=10,
        media_type="application/json",
    )


class RecordingOperations:
    def __init__(self, **results: object) -> None:
        self.results = results
        self.calls: list[tuple[str, tuple[object, ...], dict[str, object]]] = []

    def __getattr__(self, name: str) -> Any:
        async def operation(*args: object, **kwargs: object) -> object:
            self.calls.append((name, args, kwargs))
            connector_factory = kwargs.get("connector_factory")
            if callable(connector_factory):
                connector_factory()
            return self.results.get(name)

        return operation


class RecordingObservability:
    def __init__(self) -> None:
        self.boundaries: list[str] = []
        self.records: list[tuple[str, tuple[object, ...]]] = []

    @contextmanager
    def boundary(self, name: str):  # type: ignore[no-untyped-def]
        self.boundaries.append(name)
        yield

    def record_capture(self, *args: object) -> None:
        self.records.append(("capture", args))

    def record_prepared(self, *args: object) -> None:
        self.records.append(("prepared", args))

    def record_chunking(self, *args: object) -> None:
        self.records.append(("chunking", args))

    def record_publication(self, *args: object) -> None:
        self.records.append(("publication", args))

    def record_document_failure(self, *args: object) -> None:
        self.records.append(("document_failure", args))

    def record_subprocess_outcome(self, *args: object) -> None:
        self.records.append(("subprocess_outcome", args))

    def record_subprocess_fallback(self, *args: object) -> None:
        self.records.append(("subprocess_fallback", args))


class FixedDocumentResolver:
    def __init__(self, planned: object) -> None:
        self.planned = planned

    async def get(self, request: object) -> object:
        del request
        return self.planned


class RecordingPlans:
    def __init__(self, planned: tuple[object, ...]) -> None:
        self.planned = planned
        self.find_result: object | None = object()
        self.put_reference = object()
        self.calls: list[str] = []

    async def find(self, **kwargs: object) -> object | None:
        del kwargs
        self.calls.append("find")
        return self.find_result

    async def get(self, reference: object, **kwargs: object) -> tuple[object, ...]:
        del reference, kwargs
        self.calls.append("get")
        return self.planned

    async def put(self, **kwargs: object) -> object:
        del kwargs
        self.calls.append("put")
        return self.put_reference


def _planned_document() -> SimpleNamespace:
    connector_type = SimpleNamespace(value="jira")
    request = SimpleNamespace(
        connector_name="jira-main",
        configuration_fingerprint="config-v1",
        source_identity=SimpleNamespace(connector_type=connector_type),
    )
    return SimpleNamespace(document_id="doc-1", request=request)


def _build_activities() -> tuple[
    IngestionActivities,
    RecordingOperations,
    RecordingOperations,
    RecordingOperations,
    RecordingObservability,
    list[tuple[str, str]],
]:
    preparation = RecordingOperations(
        fetch_and_capture="capture-result",
        parse_and_normalize="prepared-stage",
        chunk_and_validate="chunk-statistics",
    )
    projections = RecordingOperations(
        publish_version=DocumentIngestionOutcome.PUBLISHED,
    )
    sources = RecordingOperations(
        record_published_document=DocumentIngestionOutcome.PUBLISHED,
        retry_one=DocumentIngestionOutcome.UNCHANGED,
    )
    connector_calls: list[tuple[str, str]] = []

    def connector(name: str, *, configuration_fingerprint: str) -> object:
        connector_calls.append((name, configuration_fingerprint))
        return "connector"

    runtime = SimpleNamespace(
        stages=SimpleNamespace(preparation=preparation, projections=projections),
        sources=sources,
        source_plans=RecordingPlans((_planned_document(),)),
        connector=connector,
    )
    activities = IngestionActivities(cast(Any, runtime))
    observability = RecordingObservability()
    activities._observability = cast(Any, observability)
    activities._documents = cast(Any, FixedDocumentResolver(_planned_document()))
    return activities, preparation, projections, sources, observability, connector_calls


@pytest.mark.asyncio
async def test_document_activities_delegate_every_stage_and_record_outcomes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    heartbeat_details: list[object] = []

    async def heartbeat(operation: Any, *, detail: object, **_kw: object) -> object:
        heartbeat_details.append(detail)
        return await operation

    async def isolated_subprocess(
        fn: Any, *args: Any, heartbeat_detail: object = "", **_kw: Any
    ) -> object:
        heartbeat_details.append(heartbeat_detail)
        return await args[0].parse_and_normalize(args[1], args[2])

    monkeypatch.setattr(activity_module, "heartbeat_while", heartbeat)
    monkeypatch.setattr(activity_module, "run_in_isolated_subprocess", isolated_subprocess)
    monkeypatch.setattr(activity_module, "last_heartbeat_detail", lambda: None)
    monkeypatch.setattr(activity_module, "to_capture_stage", lambda request: "capture-stage")
    monkeypatch.setattr(activity_module, "to_prepared_stage", lambda request: "prepared-stage")
    monkeypatch.setattr(
        activity_module,
        "to_raw_capture_result",
        lambda request, result: ("raw-result", request, result),
    )
    monkeypatch.setattr(
        activity_module,
        "to_prepared_document",
        lambda request, result: ("prepared-result", request, result),
    )

    activities, preparation, projections, sources, observer, connector_calls = _build_activities()
    document = DocumentIngestionInput("task-1", "tenant-1", "jira-main", _artifact(), 0)
    raw = RawCaptureResult(document, "doc-1", "version-1", "METADATA_ONLY")
    prepared = PreparedDocument(document, "doc-1", "version-1", "METADATA_ONLY")

    capture_result: object = await activities.fetch_and_capture_raw(document)
    assert capture_result == (
        "raw-result",
        document,
        "capture-result",
    )
    parse_result: object = await activities.parse_and_normalize(raw)
    assert parse_result == (
        "prepared-result",
        document,
        "prepared-stage",
    )
    for method_name in (
        "sync_content_units",
        "persist_canonical",
        "chunk_and_validate",
        "encode_chunks",
        "build_relations",
        "build_projections",
        "write_vector_projection",
        "write_graph_projection",
        "verify_projections",
    ):
        assert await getattr(activities, method_name)(prepared) is prepared
    assert await activities.publish_version(prepared) is DocumentIngestionOutcome.PUBLISHED
    await activities.record_document_failure(
        DocumentFailureInput(document, None, "parse_and_normalize", "ParserError")
    )
    await activities.record_document_failure(
        DocumentFailureInput(document, prepared, "chunk_and_validate", "ChunkError")
    )

    assert connector_calls == [("jira-main", "config-v1")]
    assert heartbeat_details == [
        "fetch-and-capture",
        {
            "stage": "parse-and-normalize",
            "task_id": "task-1",
            "document_id": "doc-1",
            "document_index": 0,
            "mode": "subprocess",
            "resumed": False,
            "prior": None,
        },
        "sync-content-units",
        "persist-canonical",
        "chunk-and-validate",
        "encode-chunks",
        "build-relations",
        "build-projections",
        "write-vector-projection",
        "write-graph-projection",
        "verify-projections",
    ]
    assert {name for name, _, _ in preparation.calls} == {
        "fetch_and_capture",
        "parse_and_normalize",
        "sync_content_units",
        "persist_canonical",
        "chunk_and_validate",
        "encode_chunks",
        "record_failure",
    }
    assert {name for name, _, _ in projections.calls} == {
        "build_relations",
        "build_projections",
        "write_vector_projection",
        "write_graph_projection",
        "verify_projections",
        "publish_version",
    }
    assert [name for name, _, _ in sources.calls] == [
        "record_published_document",
        "record_failed_document",
        "record_failed_document",
    ]
    assert observer.boundaries == [
        "FetchAndCaptureRaw",
        "ParseAndNormalize",
        "SyncContentUnits",
        "PersistCanonical",
        "ChunkAndValidate",
        "EncodeChunks",
        "BuildRelations",
        "BuildProjections",
        "WriteVectorProjection",
        "WriteGraphProjection",
        "VerifyProjections",
        "PublishVersion",
        "RecordDocumentFailure",
        "RecordDocumentFailure",
    ]
    assert [name for name, _ in observer.records] == [
        "capture",
        "subprocess_outcome",
        "prepared",
        "chunking",
        "publication",
        "document_failure",
        "document_failure",
    ]


@pytest.mark.asyncio
async def test_retry_activities_cover_selection_release_failure_and_finalization(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    activities, _, _, sources, _, connector_calls = _build_activities()
    planned = _planned_document()
    plans = RecordingPlans((planned,))
    cast(Any, activities._runtime).source_plans = plans
    activities._documents = cast(Any, FixedDocumentResolver(planned))
    monkeypatch.setattr(retry_module, "to_workflow_artifact", lambda reference: _artifact())

    selection = RetryFailuresInput("retry-1", "original-1", "tenant-1", ("doc-1",))
    result = await activities.prepare_retry_failures(selection)
    assert result.plan_reference == _artifact() and result.document_count == 1

    retry_document = RetryDocumentInput("retry-1", "original-1", "tenant-1", _artifact(), 0)
    assert (
        await activities.retry_document_release(retry_document)
        is DocumentIngestionOutcome.UNCHANGED
    )
    await activities.record_retry_document_failure(
        RetryDocumentFailureInput(retry_document, "ProjectionError")
    )
    await activities.record_retry_failures_task_failure(
        RetryTaskFailureInput("retry-1", "retry_failed")
    )
    await activities.finalize_retry_failures(
        RetryFinalizationInput("retry-1", 1, DocumentDispatchSummary(unchanged=1))
    )

    assert connector_calls == [("jira-main", "config-v1")]
    assert plans.calls == ["find", "get", "put"]
    assert [name for name, _, _ in sources.calls] == [
        "begin_retry",
        "retry_one",
        "record_retry_failure",
        "fail_retry",
        "finish_retry",
    ]

    plans.find_result = None
    with pytest.raises(ValueError, match="source plan is unavailable"):
        await activities.prepare_retry_failures(selection)
    plans.find_result = object()
    outside_plan = RetryFailuresInput("retry-2", "original-1", "tenant-1", ("other",))
    with pytest.raises(ValueError, match="outside the source plan"):
        await activities.prepare_retry_failures(outside_plan)


@pytest.mark.asyncio
async def test_plan_resolver_rejects_an_out_of_range_document_index() -> None:
    plans = RecordingPlans(())
    resolver = PlanDocumentResolver(cast(Any, plans))
    request = DocumentIngestionInput("task-1", "tenant-1", "jira-main", _artifact(), 3)

    with pytest.raises(ApplicationError, match="document index is invalid") as raised:
        await resolver.get(request)
    assert raised.value.non_retryable is True


@pytest.mark.asyncio
async def test_parse_and_normalize_falls_back_when_subprocess_serialization_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def heartbeat(operation: Any, *, detail: object, **_kw: object) -> object:
        assert isinstance(detail, dict)
        assert detail["mode"] == "in-process-fallback"
        return await operation

    async def isolated_subprocess(*_args: Any, **_kwargs: Any) -> object:
        raise activity_module.SubprocessCrashError(
            "isolated subprocess serialization failed: TypeError: cannot pickle 'mappingproxy' object"
        )

    monkeypatch.setattr(activity_module, "heartbeat_while", heartbeat)
    monkeypatch.setattr(activity_module, "run_in_isolated_subprocess", isolated_subprocess)
    monkeypatch.setattr(activity_module, "last_heartbeat_detail", lambda: None)
    monkeypatch.setattr(activity_module, "to_capture_stage", lambda request: "capture-stage")
    monkeypatch.setattr(
        activity_module,
        "to_prepared_document",
        lambda request, result: ("prepared-result", request, result),
    )

    activities, _, _, _, _, _ = _build_activities()
    document = DocumentIngestionInput("task-1", "tenant-1", "jira-main", _artifact(), 0)
    raw = RawCaptureResult(document, "doc-1", "version-1", "METADATA_ONLY")

    parse_result: object = await activities.parse_and_normalize(raw)
    assert parse_result == ("prepared-result", document, "prepared-stage")
