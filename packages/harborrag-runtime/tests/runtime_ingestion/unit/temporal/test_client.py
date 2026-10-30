"""Durable ingestion client behavior."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
from temporalio.client import (
    Schedule,
    ScheduleActionStartWorkflow,
    SchedulePolicy,
    ScheduleSpec,
    ScheduleState,
    WorkflowFailureError,
)
from temporalio.exceptions import ActivityError, ApplicationError
from temporalio.service import RPCError, RPCStatusCode

from harborrag_runtime.config.temporal import (
    TemporalConnectionConfig,
    TemporalRuntimeConfig,
    TemporalTLSConfig,
)
from harborrag_runtime.errors import (
    RuntimeConnectionError,
    WorkflowNotFoundError,
    WorkflowNotRunningError,
    WorkflowOperationError,
)
from harborrag_runtime.scheduling.errors import ScheduleNotFoundError, ScheduleValidationError
from harborrag_runtime.scheduling.models import (
    ScheduleDefinition,
    ScheduledWorkflow,
    ScheduleOverlap,
    ScheduleOwner,
    SourceScheduleTarget,
)
from harborrag_runtime.temporal import client as client_module
from harborrag_runtime.temporal import connection as connection_module
from harborrag_runtime.temporal import schedules as schedules_module
from harborrag_runtime.temporal.client import IngestionTemporalClient
from harborrag_runtime.temporal.maintenance_schemas import (
    ReindexInput,
    ReindexResult,
)
from harborrag_runtime.temporal.schemas import (
    ProcessingProfileInput,
    ScheduledSourceIngestionInput,
    SourceIngestionInput,
    SourceIngestionResult,
    SourceIngestionStatus,
)
from harborrag_runtime.temporal_models import (
    ActivityRetryConfig,
    RetryPolicyConfig,
    TaskQueueConfig,
)


def _processing() -> ProcessingProfileInput:
    return ProcessingProfileInput(
        parser_profile="parser-v1",
        normalizer_version="canonical-v1",
        chunk_strategy="chunks-v1",
        dense_encoder_profile="dense-v1",
        sparse_encoder_profile="sparse-v1",
        graph_projection_version="graph-v1",
        vector_projection_schema="vector-v2",
    )


@pytest.mark.asyncio
async def test_schedule_list_describes_concurrently_with_a_bound(monkeypatch) -> None:
    entries = [
        SimpleNamespace(
            id=str(index),
            memo=AsyncMock(return_value={"harborrag_schedule": {}} if index != 0 else {}),
        )
        for index in range(25)
    ]

    async def iterator():
        for entry in entries:
            yield entry

    sdk = _SdkClient()
    sdk.list_schedules = AsyncMock(return_value=iterator())
    backend = schedules_module.TemporalScheduleBackend(sdk, TemporalRuntimeConfig())
    active = 0
    peak = 0
    described = []

    async def describe(schedule_id):
        nonlocal active, peak
        described.append(schedule_id)
        active += 1
        peak = max(peak, active)
        try:
            await asyncio.sleep(0)
            if schedule_id == "1":
                raise ScheduleNotFoundError("deleted during collection")
            return schedule_id
        finally:
            active -= 1

    monkeypatch.setattr(backend, "describe", describe)

    assert await backend.list_all() == [str(index) for index in range(2, 25)]
    assert "0" not in described
    assert 1 < peak <= 10


def _source() -> SourceIngestionInput:
    return SourceIngestionInput(
        task_id="task-1",
        tenant_id="tenant-1",
        connector_name="local-docs",
        connector_type="local",
        connection_id="local-docs",
        source_scope_id="docs",
        configuration_fingerprint="config-v1",
        processing=_processing(),
    )


def _source_result() -> SourceIngestionResult:
    return SourceIngestionResult(
        task_id="task-1",
        scan_id="scan-1",
        discovered=1,
        published=1,
        unchanged=0,
        failed=0,
        removal_candidates=(),
        unresolved_relations=0,
    )


def _reindex() -> ReindexInput:
    return ReindexInput(
        reindex_job_id="reindex-1",
        tenant_id="tenant-1",
        processing=_processing(),
    )


def _reindex_result() -> ReindexResult:
    return ReindexResult(
        reindex_job_id="reindex-1",
        status="COMPLETED",
        connector_call_count=0,
        scanned_count=1,
        processed_count=1,
        published_count=1,
        skipped_count=0,
        failure_count=0,
    )


def _status(
    *,
    status: str = "RUNNING",
    pause_applied: bool = False,
) -> SourceIngestionStatus:
    return SourceIngestionStatus(
        task_id="task-1",
        status=status,
        paused=status == "PAUSED",
        cancel_requested=False,
        pause_applied=pause_applied,
    )


class _Handle:
    def __init__(self, result: object, *, status: object | None) -> None:
        self.first_execution_run_id = "execution-1"
        self.result = AsyncMock(return_value=result)
        self.query = AsyncMock(side_effect=self._query)
        self.describe = AsyncMock(return_value=SimpleNamespace(status=status))
        self.signal = AsyncMock()
        self.cancel = AsyncMock()

    @staticmethod
    def _query(name: str, **_options: object) -> object:
        if name == "get_status":
            return _status(pause_applied=True)
        return {"published": 1}


class _SdkClient:
    def __init__(self) -> None:
        self.source_handle = _Handle(
            _source_result(),
            status=SimpleNamespace(name="COMPLETED"),
        )
        self.reindex_handle = _Handle(_reindex_result(), status=None)
        self.start_workflow = AsyncMock(side_effect=self._start)
        self.get_workflow_handle = Mock(side_effect=self._handle)
        self.service_client = SimpleNamespace(check_health=AsyncMock(return_value=True))
        self.create_schedule = AsyncMock()
        self.schedule_handle = SimpleNamespace(
            pause=AsyncMock(),
            unpause=AsyncMock(),
            trigger=AsyncMock(),
            delete=AsyncMock(),
        )
        self.get_schedule_handle = Mock(return_value=self.schedule_handle)
        self.workflow_service = SimpleNamespace(
            pause_workflow_execution=AsyncMock(),
            unpause_workflow_execution=AsyncMock(),
        )

    def _start(self, workflow_name: str, *_args: object, **_kwargs: object) -> _Handle:
        return self.reindex_handle if workflow_name == "harborrag.reindex" else self.source_handle

    def _handle(self, workflow_id: str, **_kwargs: object) -> _Handle:
        return (
            self.reindex_handle
            if workflow_id.startswith("harborrag-reindex:")
            else self.source_handle
        )


@pytest.mark.asyncio
async def test_client_connects_with_plaintext_and_tls(monkeypatch) -> None:
    connect = AsyncMock(return_value=_SdkClient())
    monkeypatch.setattr(connection_module.Client, "connect", connect)

    plaintext = TemporalRuntimeConfig()
    assert isinstance(
        await IngestionTemporalClient.connect(plaintext),
        IngestionTemporalClient,
    )
    assert connect.await_args.kwargs["tls"] is None

    secure = TemporalRuntimeConfig(
        connection=TemporalConnectionConfig(
            target="temporal.example:7233",
            tls=TemporalTLSConfig(
                enabled=True,
                domain="temporal.example",
                server_root_ca_cert=b"ca",
                client_cert=b"cert",
                client_private_key=b"key",
            ),
        )
    )
    await IngestionTemporalClient.connect(secure)
    tls = connect.await_args.kwargs["tls"]
    assert tls.domain == "temporal.example"
    assert tls.client_cert == b"cert"


@pytest.mark.asyncio
async def test_client_translates_native_transport_errors(monkeypatch) -> None:
    low_level = RuntimeError("InvalidMessage(InvalidContentType)")
    monkeypatch.setattr(
        connection_module.Client,
        "connect",
        AsyncMock(side_effect=low_level),
    )

    with pytest.raises(RuntimeConnectionError, match=r"gRPC frontend.*7233.*8080") as captured:
        await IngestionTemporalClient.connect(TemporalRuntimeConfig())

    assert captured.value.__cause__ is low_level


@pytest.mark.asyncio
async def test_client_submits_source_and_reindex_with_bounded_options() -> None:
    sdk = _SdkClient()
    config = TemporalRuntimeConfig(
        workflow_execution_timeout_seconds=123,
        workflow_task_timeout_seconds=7,
    )
    client = IngestionTemporalClient(sdk, config)

    assert await client.start(_source()) is sdk.source_handle
    source_call = sdk.start_workflow.await_args_list[0]
    assert source_call.args[0] == "harborrag.source_ingestion"
    assert source_call.kwargs["id"] == "harborrag-source:task-1"
    assert source_call.kwargs["execution_timeout"].total_seconds() == 123

    reference = await client.start_ingestion(_source())
    assert reference.run_id == "task-1"
    assert reference.workflow_id == "harborrag-source:task-1"
    assert reference.first_execution_run_id == "execution-1"

    assert await client.start_reindex(_reindex()) is sdk.reindex_handle
    reindex_call = sdk.start_workflow.await_args_list[2]
    assert reindex_call.args[0] == "harborrag.reindex"
    assert reindex_call.kwargs["id"] == "harborrag-reindex:reindex-1"
    assert reindex_call.kwargs["task_timeout"].total_seconds() == 7


def _schedule_definition(**overrides: object) -> ScheduleDefinition:
    values: dict[str, object] = {
        "schedule_id": "source-sync-1",
        "workflow": ScheduledWorkflow.SOURCE_INGESTION,
        "cron": "0 1 * * *",
        "target": SourceScheduleTarget(
            tenant_id="tenant-1",
            connection_id="local-docs",
            source_scope_id="docs",
            force_reprocess=True,
        ),
    }
    values.update(overrides)
    return ScheduleDefinition(**values)  # type: ignore[arg-type]


def test_schedule_backend_maps_interval_and_policy_fields() -> None:
    backend = schedules_module.TemporalScheduleBackend(_SdkClient(), TemporalRuntimeConfig())
    definition = _schedule_definition(
        cron=None,
        interval_seconds=3600,
        overlap=ScheduleOverlap.BUFFER_ONE,
        catchup_window_seconds=1800,
        jitter_seconds=60,
        pause_on_failure=True,
        owner=ScheduleOwner.API,
    )

    schedule = backend._schedule(definition, desired_paused=False)

    assert schedule.spec.cron_expressions == []
    assert schedule.spec.intervals[0].every.total_seconds() == 3600
    assert schedule.spec.jitter.total_seconds() == 60
    assert schedule.policy.overlap.name == "BUFFER_ONE"
    assert schedule.policy.catchup_window.total_seconds() == 1800
    assert schedule.policy.pause_on_failure is True
    assert schedule.action.id == "harborrag-scheduled-source:source-sync-1"


def test_schedule_backend_defaults_to_one_hour_catchup_and_skip() -> None:
    backend = schedules_module.TemporalScheduleBackend(_SdkClient(), TemporalRuntimeConfig())

    schedule = backend._schedule(_schedule_definition(), desired_paused=False)

    assert schedule.policy.overlap.name == "SKIP"
    assert schedule.policy.catchup_window.total_seconds() == 3600


def test_schedule_action_carries_only_the_target() -> None:
    backend = schedules_module.TemporalScheduleBackend(_SdkClient(), TemporalRuntimeConfig())

    schedule = backend._schedule(_schedule_definition(), desired_paused=True)

    assert schedule.action.workflow == "harborrag.scheduled_source_ingestion"
    assert schedule.action.task_queue == "harborrag-discovery"
    assert schedule.action.args[0] == ScheduledSourceIngestionInput(
        schedule_id="source-sync-1",
        tenant_id="tenant-1",
        connection_id="local-docs",
        source_scope_id="docs",
        force_reprocess=True,
    )
    assert schedule.action.memo["harborrag_schedule"]["desired_paused"] is True


@pytest.mark.asyncio
async def test_schedule_backend_update_keeps_live_pause_state() -> None:
    sdk = _SdkClient()
    sdk.schedule_handle.update = AsyncMock()
    backend = schedules_module.TemporalScheduleBackend(sdk, TemporalRuntimeConfig())
    existing = Schedule(
        action=ScheduleActionStartWorkflow("old.workflow", id="old-id", task_queue="old-queue"),
        spec=ScheduleSpec(cron_expressions=["0 1 * * *"]),
        policy=SchedulePolicy(),
        state=ScheduleState(paused=True, note="incident"),
    )

    await backend.update(_schedule_definition(cron="0 2 * * *"), desired_paused=True)

    update = sdk.schedule_handle.update.await_args.args[0](
        SimpleNamespace(description=SimpleNamespace(schedule=existing))
    )
    assert update.schedule.spec.cron_expressions == ["0 2 * * *"]
    assert update.schedule.action.workflow == "harborrag.scheduled_source_ingestion"
    assert update.schedule.state.paused is True
    assert update.schedule.state.note == "incident"


@pytest.mark.asyncio
async def test_schedule_backend_pause_records_intent_before_pausing() -> None:
    sdk = _SdkClient()
    order: list[str] = []
    updaters = []

    async def update(updater):
        order.append("update")
        updaters.append(updater)

    async def pause(*, note):
        order.append("pause")

    sdk.schedule_handle.update = update
    sdk.schedule_handle.pause = pause
    backend = schedules_module.TemporalScheduleBackend(sdk, TemporalRuntimeConfig())
    memo = _schedule_definition().to_memo()
    existing = Schedule(
        action=ScheduleActionStartWorkflow(
            "harborrag.scheduled_source_ingestion",
            id="harborrag-scheduled-source:source-sync-1",
            task_queue="harborrag-discovery",
            memo={"harborrag_schedule": memo},
        ),
        spec=ScheduleSpec(cron_expressions=["0 1 * * *"]),
    )

    await backend.pause("source-sync-1", note="incident")

    result = await updaters[0](SimpleNamespace(description=SimpleNamespace(schedule=existing)))
    assert order == ["update", "pause"]
    assert result.schedule.action.memo["harborrag_schedule"]["desired_paused"] is True
    assert result.schedule.action.memo["harborrag_schedule"]["cron"] == "0 1 * * *"


@pytest.mark.asyncio
async def test_schedule_backend_maps_rejected_definitions_to_validation_errors() -> None:
    sdk = _SdkClient()
    sdk.create_schedule.side_effect = RPCError("invalid cron", RPCStatusCode.INVALID_ARGUMENT, b"")
    backend = schedules_module.TemporalScheduleBackend(sdk, TemporalRuntimeConfig())

    with pytest.raises(ScheduleValidationError, match="invalid cron"):
        await backend.create(_schedule_definition())


@pytest.mark.asyncio
async def test_schedule_view_skips_unreadable_definitions() -> None:
    description = SimpleNamespace(
        id="broken",
        schedule=SimpleNamespace(
            action=SimpleNamespace(memo={"harborrag_schedule": {"owner": "api"}}),
            state=ScheduleState(),
        ),
    )

    assert await schedules_module._view(description) is None


@pytest.mark.asyncio
async def test_client_embeds_configured_queues_and_retries_in_root_input() -> None:
    sdk = _SdkClient()
    queues = TaskQueueConfig(
        discovery="test-discovery",
        transform="test-transform",
        io="test-io",
        parser="test-parser",
        model="test-model",
        index="test-index",
    )
    retries = ActivityRetryConfig(
        discovery=RetryPolicyConfig(maximum_attempts=3),
        document=RetryPolicyConfig(maximum_attempts=2),
    )
    client = IngestionTemporalClient(
        sdk,
        TemporalRuntimeConfig(task_queues=queues, retries=retries),
    )

    await client.start(_source())

    call = sdk.start_workflow.await_args
    submitted = call.args[1]
    assert call.kwargs["task_queue"] == "test-discovery"
    assert submitted.workflow_options.task_queues == queues
    assert submitted.workflow_options.retries == retries


@pytest.mark.asyncio
async def test_client_reads_results_progress_and_execution_status() -> None:
    sdk = _SdkClient()
    client = IngestionTemporalClient(sdk, TemporalRuntimeConfig())

    assert (await client.result("task-1")).published == 1
    assert await client.progress("task-1") == {"published": 1}
    assert (await client.get_status("task-1")).status == "RUNNING"
    assert await client.execution_status("task-1") == "completed"
    sdk.source_handle.describe.return_value = SimpleNamespace(status=None)
    assert await client.execution_status("task-1") == "unknown"
    assert (await client.reindex_result("reindex-1")).status == "COMPLETED"


@pytest.mark.asyncio
async def test_client_health_and_controls_target_source_workflow() -> None:
    sdk = _SdkClient()
    client = IngestionTemporalClient(sdk, TemporalRuntimeConfig())

    assert await client.health() is True
    await client.pause("task-1")
    await client.resume("task-1")
    await client.cancel("task-1")

    assert [call.args[0] for call in sdk.source_handle.signal.await_args_list] == [
        "pause",
        "resume",
        "request_graceful_cancel",
    ]
    sdk.source_handle.cancel.assert_not_awaited()
    pause_request = sdk.workflow_service.pause_workflow_execution.await_args.args[0]
    assert pause_request.workflow_id == "harborrag-source:task-1"
    assert pause_request.namespace == "harborrag"
    assert pause_request.request_id


@pytest.mark.asyncio
async def test_controls_keep_signaling_when_native_pause_is_unavailable() -> None:
    sdk = _SdkClient()
    unavailable = RPCError("not implemented", RPCStatusCode.UNIMPLEMENTED, b"")
    sdk.workflow_service.pause_workflow_execution.side_effect = unavailable
    sdk.workflow_service.unpause_workflow_execution.side_effect = unavailable
    client = IngestionTemporalClient(sdk, TemporalRuntimeConfig())

    await client.pause("task-1")
    await client.resume("task-1")
    await client.cancel("task-1")

    assert [call.args[0] for call in sdk.source_handle.signal.await_args_list] == [
        "pause",
        "resume",
        "request_graceful_cancel",
    ]


@pytest.mark.asyncio
async def test_pause_relay_rejects_finished_workflow() -> None:
    sdk = _SdkClient()
    sdk.source_handle.query.side_effect = lambda *_args, **_kwargs: _status(status="COMPLETED")
    client = IngestionTemporalClient(sdk, TemporalRuntimeConfig())

    with pytest.raises(WorkflowNotRunningError, match=r"already finished \(COMPLETED\)"):
        await client.pause("task-1")

    sdk.workflow_service.pause_workflow_execution.assert_not_awaited()


@pytest.mark.asyncio
async def test_pause_relay_times_out_without_acknowledgement(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sdk = _SdkClient()
    sdk.source_handle.query.side_effect = lambda *_args, **_kwargs: _status()
    sleep = AsyncMock()
    monkeypatch.setattr(client_module.asyncio, "sleep", sleep)
    client = IngestionTemporalClient(sdk, TemporalRuntimeConfig())

    with pytest.raises(WorkflowOperationError, match="pause relay did not settle"):
        await client.pause("task-1")

    assert sleep.await_count == 300
    sdk.workflow_service.pause_workflow_execution.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("status", "expected"),
    [
        (RPCStatusCode.NOT_FOUND, WorkflowNotFoundError),
        (RPCStatusCode.INTERNAL, WorkflowOperationError),
    ],
)
async def test_native_pause_control_maps_rpc_errors(
    status: RPCStatusCode,
    expected: type[Exception],
) -> None:
    sdk = _SdkClient()
    sdk.workflow_service.pause_workflow_execution.side_effect = RPCError(
        "native pause failed",
        status,
        b"",
    )
    client = IngestionTemporalClient(sdk, TemporalRuntimeConfig())

    with pytest.raises(expected):
        await client._pause_execution(
            "harborrag-source:task-1",
            reason="test",
            best_effort=False,
        )


@pytest.mark.asyncio
async def test_native_pause_control_ignores_failed_precondition() -> None:
    sdk = _SdkClient()
    sdk.workflow_service.pause_workflow_execution.side_effect = RPCError(
        "workflow is already paused",
        RPCStatusCode.FAILED_PRECONDITION,
        b"",
    )
    client = IngestionTemporalClient(sdk, TemporalRuntimeConfig())

    await client._pause_execution(
        "harborrag-source:task-1",
        reason="test",
        best_effort=False,
    )


@pytest.mark.asyncio
async def test_client_operations_use_stable_workflow_id_for_source_controls() -> None:
    """Source controls and queries resolve by workflow ID."""
    sdk = _SdkClient()
    client = IngestionTemporalClient(sdk, TemporalRuntimeConfig())

    await client.get_status("task-1")
    await client.get_progress("task-1")
    await client.pause("task-1")

    workflow_ids = [call.args[0] for call in sdk.get_workflow_handle.call_args_list]
    assert workflow_ids == [
        "harborrag-source:task-1",
        "harborrag-source:task-1",
        "harborrag-source:task-1",
        "harborrag-source:task-1",
    ]


@pytest.mark.asyncio
async def test_schedule_backend_retries_an_update_that_lost_a_race() -> None:
    sdk = _SdkClient()
    conflict = RPCError("conflict token mismatch", RPCStatusCode.FAILED_PRECONDITION, b"")
    sdk.schedule_handle.update = AsyncMock(side_effect=[conflict, None])
    backend = schedules_module.TemporalScheduleBackend(sdk, TemporalRuntimeConfig())

    await backend.update(_schedule_definition(), desired_paused=False)

    assert sdk.schedule_handle.update.await_count == 2


@pytest.mark.asyncio
async def test_schedule_backend_gives_up_after_repeated_conflicts() -> None:
    sdk = _SdkClient()
    conflict = RPCError("conflict token mismatch", RPCStatusCode.FAILED_PRECONDITION, b"")
    sdk.schedule_handle.update = AsyncMock(side_effect=conflict)
    backend = schedules_module.TemporalScheduleBackend(sdk, TemporalRuntimeConfig())

    with pytest.raises(WorkflowOperationError):
        await backend.update(_schedule_definition(), desired_paused=False)
    assert sdk.schedule_handle.update.await_count == 3


def _workflow_failure(code: str) -> WorkflowFailureError:
    application = ApplicationError("hunter2", type=code)
    activity = ActivityError(
        "activity failed",
        scheduled_event_id=1,
        started_event_id=2,
        identity="worker",
        activity_type="harborrag.discover_source_items",
        activity_id="1",
        retry_state=None,
    )
    activity.__cause__ = application
    failure = WorkflowFailureError(cause=activity)
    failure.__cause__ = activity
    return failure


@pytest.mark.asyncio
async def test_result_translates_workflow_failure_to_error_code() -> None:
    sdk = _SdkClient()
    sdk.source_handle.result.side_effect = _workflow_failure("parser_rejected_document")
    client = IngestionTemporalClient(sdk, TemporalRuntimeConfig())

    with pytest.raises(WorkflowOperationError) as raised:
        await client.result("task-1")

    message = str(raised.value)
    assert "parser_rejected_document" in message
    assert "task-1" in message
    assert "hunter2" not in message


@pytest.mark.asyncio
async def test_result_points_a_missing_connection_at_the_connection_api() -> None:
    sdk = _SdkClient()
    sdk.source_handle.result.side_effect = _workflow_failure("discoversourceitems_keyerror")
    client = IngestionTemporalClient(sdk, TemporalRuntimeConfig())

    with pytest.raises(WorkflowOperationError) as raised:
        await client.result("task-1")

    message = str(raised.value)
    assert "connection is missing or invalid" in message
    assert "GET /v1/admin/connections" in message
    assert "connectors.yaml" not in message
    assert "hunter2" not in message


@pytest.mark.asyncio
async def test_reindex_result_translates_workflow_failure() -> None:
    sdk = _SdkClient()
    sdk.reindex_handle.result.side_effect = _workflow_failure("boom")
    client = IngestionTemporalClient(sdk, TemporalRuntimeConfig())

    with pytest.raises(WorkflowOperationError) as raised:
        await client.reindex_result("reindex-1")

    assert "boom" in str(raised.value)
    assert "hunter2" not in str(raised.value)
