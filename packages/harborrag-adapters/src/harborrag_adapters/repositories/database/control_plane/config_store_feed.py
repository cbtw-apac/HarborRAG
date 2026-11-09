"""Postgres LISTEN feed that tells a process which tenant's settings changed."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from typing import Protocol

from harborrag_core.ports.config_store import SETTINGS_CHANGED_CHANNEL

logger = logging.getLogger("harborrag.adapters.config_store")

DEFAULT_BACKOFF_SECONDS = (0.5, 1.0, 2.0, 5.0)
_SQLALCHEMY_DRIVER_SUFFIX = "+asyncpg"


class ListenConnection(Protocol):
    """The part of an asyncpg connection the feed uses."""

    async def add_listener(self, channel: str, callback: Callable[..., None]) -> None:
        """Call ``callback(connection, pid, channel, payload)`` per notification."""
        ...

    def add_termination_listener(self, callback: Callable[..., None]) -> None:
        """Call ``callback(connection)`` when the connection is lost."""
        ...

    async def close(self) -> None:
        """Close the connection."""
        ...


def asyncpg_dsn(url: str) -> str:
    """Return ``url`` without the SQLAlchemy ``+asyncpg`` driver suffix."""
    return url.replace(_SQLALCHEMY_DRIVER_SUFFIX, "", 1)


async def _asyncpg_connect(dsn: str) -> ListenConnection:
    import asyncpg

    return await asyncpg.connect(dsn)  # type: ignore[no-any-return]


def _connect_errors() -> tuple[type[BaseException], ...]:
    """Return the errors a failed (re)connect raises, vendor errors included."""
    try:
        import asyncpg
    except ImportError:
        return (OSError,)
    return (OSError, asyncpg.PostgresError, asyncpg.InterfaceError)


class PostgresSettingsChangeFeed:
    """Listen on ``settings_changed`` over one dedicated connection and reconnect when it drops.

    Args:
        dsn: Control database URL; a ``+asyncpg`` suffix is removed.
        connect: Opens a connection from a plain DSN; defaults to ``asyncpg.connect``.
        backoff_seconds: Waits between reconnect attempts; the last one repeats.
    """

    def __init__(
        self,
        dsn: str,
        *,
        connect: Callable[[str], Awaitable[ListenConnection]] | None = None,
        backoff_seconds: tuple[float, ...] = DEFAULT_BACKOFF_SECONDS,
    ) -> None:
        self._dsn = asyncpg_dsn(dsn)
        self._connect = connect or _asyncpg_connect
        self._backoff_seconds = backoff_seconds

    async def run(self, on_change: Callable[[str | None], None]) -> None:
        """Call ``on_change(tenant_id)`` per notification and ``on_change(None)`` per (re)connect.

        Runs until cancelled; the open connection is closed on cancellation.
        """
        failures = 0
        while True:
            try:
                await self._listen_until_terminated(on_change)
                failures = 0
            except _connect_errors() as error:
                logger.warning("settings_feed_connect_failed error_type=%s", type(error).__name__)
            await asyncio.sleep(
                self._backoff_seconds[min(failures, len(self._backoff_seconds) - 1)]
            )
            failures += 1

    async def _listen_until_terminated(self, on_change: Callable[[str | None], None]) -> None:
        connection = await self._connect(self._dsn)
        terminated = asyncio.Event()
        try:
            connection.add_termination_listener(lambda _connection: terminated.set())
            await connection.add_listener(
                SETTINGS_CHANGED_CHANNEL,
                lambda _connection, _pid, _channel, payload: on_change(payload or None),
            )
            on_change(None)
            await terminated.wait()
        finally:
            await connection.close()
