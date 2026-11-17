"""PostgresSettingsChangeFeed against a fake LISTEN connection."""

from __future__ import annotations

import asyncio
from collections.abc import Callable

import pytest
import pytest_asyncio

from harborrag_adapters.repositories.database.control_plane.config_store_feed import (
    PostgresSettingsChangeFeed,
    asyncpg_dsn,
)
from harborrag_core.ports.config_store import SETTINGS_CHANGED_CHANNEL

pytestmark = pytest.mark.unit


class FakeListenConnection:
    """Records listeners and lets a test notify or terminate the connection."""

    def __init__(self) -> None:
        self.channel_listeners: dict[str, Callable[..., None]] = {}
        self.termination_listeners: list[Callable[..., None]] = []
        self.closed = False

    async def add_listener(self, channel: str, callback: Callable[..., None]) -> None:
        self.channel_listeners[channel] = callback

    def add_termination_listener(self, callback: Callable[..., None]) -> None:
        self.termination_listeners.append(callback)

    async def close(self) -> None:
        self.closed = True

    def notify(self, payload: str) -> None:
        self.channel_listeners[SETTINGS_CHANGED_CHANNEL](self, 1, SETTINGS_CHANGED_CHANNEL, payload)

    def terminate(self) -> None:
        for callback in self.termination_listeners:
            callback(self)


class Harness:
    """Feed over a connect function that hands out fake connections."""

    def __init__(self) -> None:
        self.connections: list[FakeListenConnection] = []
        self.dsns: list[str] = []
        self.changes: list[str | None] = []
        self.feed = PostgresSettingsChangeFeed(
            "postgresql+asyncpg://u:p@h:5432/db", connect=self._connect, backoff_seconds=(0.0,)
        )

    async def _connect(self, dsn: str) -> FakeListenConnection:
        self.dsns.append(dsn)
        connection = FakeListenConnection()
        self.connections.append(connection)
        return connection

    async def wait_for(self, condition: Callable[[], bool]) -> None:
        async with asyncio.timeout(2):
            while not condition():
                await asyncio.sleep(0.001)


@pytest_asyncio.fixture
async def running() -> Harness:
    harness = Harness()
    task = asyncio.create_task(harness.feed.run(harness.changes.append))
    harness.task = task  # type: ignore[attr-defined]
    await harness.wait_for(lambda: harness.changes == [None])
    yield harness
    task.cancel()
    await asyncio.gather(task, return_exceptions=True)


@pytest.mark.asyncio
async def test_notification_payload_reaches_callback(running: Harness) -> None:
    running.connections[0].notify("acme")

    assert running.changes == [None, "acme"]


@pytest.mark.asyncio
async def test_reconnect_after_termination_invalidates_all(running: Harness) -> None:
    running.connections[0].terminate()

    await running.wait_for(lambda: running.changes == [None, None])
    assert len(running.connections) == 2


@pytest.mark.asyncio
async def test_cancel_closes_connection(running: Harness) -> None:
    running.task.cancel()  # type: ignore[attr-defined]
    await asyncio.gather(running.task, return_exceptions=True)  # type: ignore[attr-defined]

    assert running.connections[0].closed


@pytest.mark.asyncio
async def test_connect_failure_backs_off_then_connects() -> None:
    attempts = 0
    connection = FakeListenConnection()
    changes: list[str | None] = []

    async def connect(dsn: str) -> FakeListenConnection:
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise OSError("down")
        return connection

    feed = PostgresSettingsChangeFeed("postgresql://h/db", connect=connect, backoff_seconds=(0.0,))
    task = asyncio.create_task(feed.run(changes.append))
    async with asyncio.timeout(2):
        while not changes:
            await asyncio.sleep(0.001)
    task.cancel()
    await asyncio.gather(task, return_exceptions=True)

    assert attempts == 2
    assert changes == [None]


def test_asyncpg_dsn_from_sqlalchemy_url() -> None:
    assert asyncpg_dsn("postgresql+asyncpg://u:p@h:5432/db") == "postgresql://u:p@h:5432/db"


@pytest.mark.asyncio
async def test_default_connect_receives_plain_dsn(running: Harness) -> None:
    assert running.dsns == ["postgresql://u:p@h:5432/db"]
