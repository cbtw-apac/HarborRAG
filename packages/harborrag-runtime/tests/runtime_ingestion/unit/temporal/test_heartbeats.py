"""Temporal heartbeats wrap long operations without changing their result."""

from __future__ import annotations

import asyncio

import pytest

from harborrag_runtime.temporal import heartbeats


@pytest.mark.asyncio
@pytest.mark.whitebox
async def test_heartbeat_while_awaits_directly_outside_an_activity(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(heartbeats.activity, "in_activity", lambda: False)

    async def operation() -> str:
        return "complete"

    assert await heartbeats.heartbeat_while(operation(), detail="work") == "complete"


@pytest.mark.asyncio
@pytest.mark.whitebox
async def test_heartbeat_while_pulses_and_cleans_up_inside_an_activity(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    recorded: list[object] = []
    monkeypatch.setattr(heartbeats.activity, "in_activity", lambda: True)
    monkeypatch.setattr(heartbeats.activity, "heartbeat", recorded.append)
    monkeypatch.setattr(
        heartbeats.activity,
        "info",
        lambda: type("Info", (), {"start_to_close_timeout": None, "heartbeat_details": []})(),
    )

    async def operation() -> str:
        await asyncio.sleep(0.05)  # long enough for the 0.001s pulse to fire
        return "complete"

    result = await heartbeats.heartbeat_while(
        operation(),
        detail="long-work",
        interval_seconds=0.001,
    )

    assert result == "complete"
    assert recorded[0] == "long-work"
    assert len(recorded) >= 2


@pytest.mark.asyncio
@pytest.mark.whitebox
async def test_heartbeat_while_rejects_non_positive_interval(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Interval_seconds must be positive."""
    monkeypatch.setattr(heartbeats.activity, "in_activity", lambda: False)
    loop = asyncio.get_running_loop()

    with pytest.raises(ValueError, match="positive"):
        done = loop.create_future()
        done.set_result(None)
        await heartbeats.heartbeat_while(done, detail="x", interval_seconds=0)

    with pytest.raises(ValueError, match="positive"):
        done = loop.create_future()
        done.set_result(None)
        await heartbeats.heartbeat_while(done, detail="x", interval_seconds=-1)


@pytest.mark.asyncio
@pytest.mark.whitebox
async def test_heartbeat_while_asserts_interval_shorter_than_start_to_close(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Interval must be shorter than start_to_close_timeout."""
    from datetime import timedelta

    recorded: list[object] = []
    monkeypatch.setattr(heartbeats.activity, "in_activity", lambda: True)
    monkeypatch.setattr(heartbeats.activity, "heartbeat", recorded.append)
    monkeypatch.setattr(
        heartbeats.activity,
        "info",
        lambda: type(
            "Info",
            (),
            {"start_to_close_timeout": timedelta(seconds=60), "heartbeat_details": []},
        )(),
    )

    async def _noop() -> None:
        pass

    coro = _noop()
    with pytest.raises(AssertionError, match="start_to_close_timeout"):
        await heartbeats.heartbeat_while(coro, detail="x", interval_seconds=120)
    # Close any unawaited coroutine to silence ResourceWarning.
    coro.close()


@pytest.mark.whitebox
def test_last_heartbeat_detail_returns_none_outside_activity(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Last_heartbeat_detail returns None when not inside an activity."""
    monkeypatch.setattr(heartbeats.activity, "in_activity", lambda: False)
    assert heartbeats.last_heartbeat_detail() is None


@pytest.mark.whitebox
def test_last_heartbeat_detail_returns_first_element_from_prior_attempt(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Last_heartbeat_detail exposes prior-attempt progress for retry resume."""
    monkeypatch.setattr(heartbeats.activity, "in_activity", lambda: True)
    monkeypatch.setattr(
        heartbeats.activity,
        "info",
        lambda: type("Info", (), {"heartbeat_details": [{"page": 3, "stage": "discovery"}]})(),
    )

    detail = heartbeats.last_heartbeat_detail()
    assert detail == {"page": 3, "stage": "discovery"}
