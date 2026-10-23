"""A pause delivered during discovery or finalization is accepted, not an outage."""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

from harborrag_runtime.config.temporal import TemporalRuntimeConfig
from harborrag_runtime.errors import WorkflowOperationError
from harborrag_runtime.temporal import client as client_module
from harborrag_runtime.temporal.client import IngestionTemporalClient
from harborrag_runtime.temporal.schemas import SourceIngestionStatus

from .test_client import _SdkClient


def _pending(*, cancel_requested: bool = False) -> SourceIngestionStatus:
    # The signal landed (`paused`), but no batch boundary or relay has applied it.
    return SourceIngestionStatus(
        task_id="task-1",
        status="CANCELLING" if cancel_requested else "PAUSED",
        paused=not cancel_requested,
        cancel_requested=cancel_requested,
        pause_applied=False,
    )


@pytest.mark.asyncio
async def test_a_pause_pending_until_the_next_batch_boundary_is_accepted(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sdk = _SdkClient()
    sdk.source_handle.query.side_effect = lambda *_args, **_kwargs: _pending()
    monkeypatch.setattr(client_module.asyncio, "sleep", AsyncMock())
    client = IngestionTemporalClient(sdk, TemporalRuntimeConfig())

    await client.pause("task-1")

    assert [call.args[0] for call in sdk.source_handle.signal.await_args_list] == ["pause"]
    # Natively pausing the run now would keep it from reaching the boundary that
    # persists PAUSED, so only the signal is sent.
    sdk.workflow_service.pause_workflow_execution.assert_not_awaited()


@pytest.mark.asyncio
async def test_a_pause_overtaken_by_cancellation_still_reports_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sdk = _SdkClient()
    sdk.source_handle.query.side_effect = lambda *_args, **_kwargs: _pending(cancel_requested=True)
    monkeypatch.setattr(client_module.asyncio, "sleep", AsyncMock())
    client = IngestionTemporalClient(sdk, TemporalRuntimeConfig())

    with pytest.raises(WorkflowOperationError, match="pause relay did not settle"):
        await client.pause("task-1")

    sdk.workflow_service.pause_workflow_execution.assert_not_awaited()
