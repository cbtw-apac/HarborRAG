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
    recorded: list[str] = []
    monkeypatch.setattr(heartbeats.activity, "in_activity", lambda: True)
    monkeypatch.setattr(heartbeats.activity, "heartbeat", recorded.append)

    async def operation() -> str:
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        return "complete"

    result = await heartbeats.heartbeat_while(
        operation(),
        detail="long-work",
        interval_seconds=0,
    )

    assert result == "complete"
    assert recorded[0] == "long-work"
    assert len(recorded) >= 2
