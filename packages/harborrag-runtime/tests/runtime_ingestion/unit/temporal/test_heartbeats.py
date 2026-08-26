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
    second_pulse = asyncio.Event()
    monkeypatch.setattr(heartbeats.activity, "in_activity", lambda: True)

    def _record(detail: object) -> None:
        recorded.append(detail)
        if len(recorded) >= 2:
            second_pulse.set()

    monkeypatch.setattr(heartbeats.activity, "heartbeat", _record)
    monkeypatch.setattr(
        heartbeats.activity,
        "info",
        lambda: type(
            "Info",
            (),
            {
                "start_to_close_timeout": None,
                "heartbeat_timeout": None,
                "heartbeat_details": [],
            },
        )(),
    )

    async def operation() -> str:
        await second_pulse.wait()
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

    for invalid in (0, -1, float("nan"), float("inf"), float("-inf")):
        with pytest.raises(ValueError, match="finite and positive"):
            done = loop.create_future()
            done.set_result(None)
            await heartbeats.heartbeat_while(done, detail="x", interval_seconds=invalid)


@pytest.mark.asyncio
@pytest.mark.whitebox
async def test_heartbeat_while_rejects_interval_shorter_than_start_to_close(
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
            {
                "start_to_close_timeout": timedelta(seconds=60),
                "heartbeat_timeout": None,
                "heartbeat_details": [],
            },
        )(),
    )

    async def _noop() -> None:
        pass

    coro = _noop()
    with pytest.raises(ValueError, match="start_to_close_timeout"):
        await heartbeats.heartbeat_while(coro, detail="x", interval_seconds=120)
    # Close any unawaited coroutine to silence ResourceWarning.
    coro.close()


@pytest.mark.asyncio
@pytest.mark.whitebox
async def test_heartbeat_while_rejects_interval_equal_to_heartbeat_timeout(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Interval must be shorter than heartbeat_timeout, boundary included."""
    from datetime import timedelta as _timedelta

    monkeypatch.setattr(heartbeats.activity, "in_activity", lambda: True)
    monkeypatch.setattr(heartbeats.activity, "heartbeat", lambda _detail: None)
    monkeypatch.setattr(
        heartbeats.activity,
        "info",
        lambda: type(
            "Info",
            (),
            {
                "start_to_close_timeout": None,
                "heartbeat_timeout": _timedelta(seconds=120),
                "heartbeat_details": [],
            },
        )(),
    )

    async def _noop() -> None:
        pass

    coro = _noop()
    with pytest.raises(ValueError, match="heartbeat_timeout"):
        await heartbeats.heartbeat_while(coro, detail="x", interval_seconds=120)
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


@pytest.mark.whitebox
def test_last_heartbeat_detail_returns_none_on_first_attempt(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Last_heartbeat_detail returns None when no prior attempt heartbeated."""
    monkeypatch.setattr(heartbeats.activity, "in_activity", lambda: True)
    monkeypatch.setattr(
        heartbeats.activity,
        "info",
        lambda: type("Info", (), {"heartbeat_details": []})(),
    )
    assert heartbeats.last_heartbeat_detail() is None
