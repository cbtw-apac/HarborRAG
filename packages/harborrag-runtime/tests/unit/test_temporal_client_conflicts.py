"""Caller-actionable Temporal failures stay distinguishable from upstream faults.

Temporal answers NOT_FOUND both for a run that never existed and for a signal
sent to a run that has already closed, and it reports a reused workflow ID
through a dedicated SDK exception. All three reach operators through the same
envelope, so the client has to name them apart.
"""

from __future__ import annotations

from dataclasses import dataclass

import pytest
from temporalio.exceptions import WorkflowAlreadyStartedError
from temporalio.service import RPCError, RPCStatusCode

from harborrag_runtime.config.temporal import TemporalRuntimeConfig
from harborrag_runtime.errors import (
    WorkflowNotFoundError,
    WorkflowNotRetryableError,
    WorkflowNotRunningError,
    WorkflowRunAlreadyStartedError,
    WorkflowSubmissionError,
)
from harborrag_runtime.temporal.client import TemporalRuntimeClient
from harborrag_runtime.temporal.schemas import IngestionRunInput


def _not_found() -> RPCError:
    return RPCError("workflow not found", RPCStatusCode.NOT_FOUND, b"")


@dataclass
class _ExecutionStatus:
    """Stands in for temporalio's WorkflowExecutionStatus enum member."""

    name: str


@dataclass
class _Description:
    """Stands in for the WorkflowExecutionDescription returned by describe()."""

    status: _ExecutionStatus | None


class _ClosedRunHandle:
    """Signals fail with NOT_FOUND while describe still reports the execution."""

    first_execution_run_id = "temporal-run-1"

    def __init__(self, execution_status: str | None = "COMPLETED") -> None:
        self._execution_status = execution_status

    async def signal(self, name, arg=None):
        raise _not_found()

    async def cancel(self):
        raise _not_found()

    async def query(self, name, **kwargs):
        # Queries keep working against a closed execution -- that asymmetry is
        # exactly why the raw NOT_FOUND from a signal is misleading. Report the
        # artifact as failed so a retry clears validation and reaches the signal.
        return ("artifact-1",) if name == "get_failed_artifacts" else ()

    async def describe(self):
        if self._execution_status is None:
            raise _not_found()
        return _Description(status=_ExecutionStatus(name=self._execution_status))


class _Client:
    def __init__(self, handle) -> None:
        self.handle = handle
        self.already_started = False

    def get_workflow_handle(self, workflow_id, **kwargs):
        return self.handle

    async def start_workflow(self, name, request, **kwargs):
        if self.already_started:
            raise WorkflowAlreadyStartedError("run-1", name)
        return self.handle


def _client(handle) -> TemporalRuntimeClient:
    return TemporalRuntimeClient(_Client(handle), TemporalRuntimeConfig())


def _run_input() -> IngestionRunInput:
    return IngestionRunInput(
        run_id="run-1",
        tenant_id="tenant-1",
        connector_name="local",
        manifest_id="manifest-1",
        generation_id="generation-1",
    )


@pytest.mark.asyncio
async def test_signalling_a_closed_run_reports_it_finished_not_missing() -> None:
    """The run plainly exists, so "not found" would send operators hunting it."""

    client = _client(_ClosedRunHandle("COMPLETED"))

    with pytest.raises(WorkflowNotRunningError) as raised:
        await client.pause("run-1")

    assert "already finished" in str(raised.value)
    assert "completed" in str(raised.value)


@pytest.mark.asyncio
async def test_signalling_an_absent_run_still_reports_not_found() -> None:
    """When describe also reports NOT_FOUND the run is genuinely absent."""

    client = _client(_ClosedRunHandle(execution_status=None))

    with pytest.raises(WorkflowNotFoundError) as raised:
        await client.pause("run-1")

    assert "run not found" in str(raised.value)


@pytest.mark.asyncio
async def test_closed_run_detection_covers_every_control_signal() -> None:
    client = _client(_ClosedRunHandle("TERMINATED"))

    with pytest.raises(WorkflowNotRunningError):
        await client.resume("run-1")
    with pytest.raises(WorkflowNotRunningError):
        await client.retry_failed("run-1", ("artifact-1",))
    with pytest.raises(WorkflowNotRunningError):
        await client.cancel("run-1", graceful=True)


class _RetryHandle:
    """Reports attention queues and records any signal that gets through."""

    first_execution_run_id = "temporal-run-1"

    def __init__(self, failed=(), quarantined=()) -> None:
        self._queues = {
            "get_failed_artifacts": tuple(failed),
            "get_quarantined_artifacts": tuple(quarantined),
        }
        self.signals: list[tuple[str, object]] = []

    async def signal(self, name, arg=None):
        self.signals.append((name, arg))

    async def query(self, name, **kwargs):
        return self._queues[name]


@pytest.mark.asyncio
async def test_retrying_a_non_failed_artifact_is_refused_not_silently_dropped() -> None:
    """The workflow discards these without a trace, so success would be a lie."""

    handle = _RetryHandle(failed=("artifact-failed",))
    client = _client(handle)

    with pytest.raises(WorkflowNotRetryableError) as raised:
        await client.retry_failed("run-1", ("artifact-succeeded",))

    assert "artifact-succeeded" in str(raised.value)
    assert "not failed or quarantined" in str(raised.value)
    assert handle.signals == []


@pytest.mark.asyncio
async def test_retrying_failed_and_quarantined_artifacts_is_forwarded() -> None:
    handle = _RetryHandle(failed=("artifact-failed",), quarantined=("artifact-quarantined",))
    client = _client(handle)

    await client.retry_failed("run-1", ("artifact-failed", "artifact-quarantined"))

    assert handle.signals == [
        ("retry_failed", ("artifact-failed", "artifact-quarantined")),
    ]


@pytest.mark.asyncio
async def test_a_partly_valid_retry_is_forwarded_whole() -> None:
    """An artifact still in flight is not yet retryable but may become so.

    The workflow keeps unmatched IDs queued for a later retry check instead of
    discarding them, so refusing the request would lose a retry that would
    otherwise have been honoured once the artifact's partition completed.
    """

    handle = _RetryHandle(failed=("artifact-failed",))
    client = _client(handle)

    await client.retry_failed("run-1", ("artifact-failed", "artifact-in-flight"))

    assert handle.signals == [
        ("retry_failed", ("artifact-failed", "artifact-in-flight")),
    ]


@pytest.mark.asyncio
async def test_reused_run_id_is_a_conflict_not_a_generic_submission_failure() -> None:
    handle = _ClosedRunHandle()
    client = _client(handle)
    client._client.already_started = True  # type: ignore[attr-defined]

    with pytest.raises(WorkflowRunAlreadyStartedError) as raised:
        await client.start_ingestion(_run_input())

    assert "already in use" in str(raised.value)
    # Still a submission failure, so existing handlers keep working.
    assert isinstance(raised.value, WorkflowSubmissionError)
