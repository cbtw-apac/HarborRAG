"""A pause waits a bounded time for its relay, and a resume that overtakes it is no outage."""

from __future__ import annotations

import asyncio

import pytest

from harborrag_engine.ingestion.operations import ACTION_WAIT_SECONDS
from harborrag_runtime.config.temporal import TemporalRuntimeConfig
from harborrag_runtime.temporal import client as client_module
from harborrag_runtime.temporal.client import IngestionTemporalClient
from harborrag_runtime.temporal.schemas import SourceIngestionStatus

from .test_client import _SdkClient

pytestmark = pytest.mark.unit

_PROXY_TIMEOUT_SECONDS = 30


def _status(*, paused: bool) -> SourceIngestionStatus:
    return SourceIngestionStatus(
        task_id="task-1",
        status="PAUSED" if paused else "RUNNING",
        paused=paused,
        cancel_requested=False,
        pause_applied=False,
    )


@pytest.mark.asyncio
async def test_a_resume_that_overtakes_a_waiting_pause_answers_at_once() -> None:
    sdk = _SdkClient()
    answers: list[SourceIngestionStatus] = []

    async def status(*_args: object, **_kwargs: object) -> SourceIngestionStatus:
        answers.append(_status(paused=not answers))
        return answers[-1]

    sdk.source_handle.query.side_effect = status
    client = IngestionTemporalClient(sdk, TemporalRuntimeConfig())

    async with asyncio.timeout(5):
        await client.pause("task-1")

    assert [answer.paused for answer in answers] == [True, False]


@pytest.mark.asyncio
async def test_a_pending_pause_is_accepted_within_the_bound_however_slow_the_queries(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sdk = _SdkClient()

    async def slow_status(*_args: object, **_kwargs: object) -> SourceIngestionStatus:
        await asyncio.sleep(0.5)
        return _status(paused=True)

    sdk.source_handle.query.side_effect = slow_status
    monkeypatch.setattr(client_module, "_PAUSE_SETTLE_SECONDS", 1.0)
    client = IngestionTemporalClient(sdk, TemporalRuntimeConfig())
    loop = asyncio.get_running_loop()
    started = loop.time()

    async with asyncio.timeout(5):
        await client.pause("task-1")

    assert loop.time() - started < 3


def test_a_pause_request_answers_before_a_30_second_proxy_cuts_it() -> None:
    waits = client_module._PAUSE_SETTLE_SECONDS + ACTION_WAIT_SECONDS["pause"]

    assert waits < _PROXY_TIMEOUT_SECONDS
