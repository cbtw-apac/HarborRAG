from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest
from harborrag_adapters.repositories.shared import redis as redis_module
from harborrag_adapters.repositories.shared.redis import RedisDBClient


class FakeAsyncRedisClient:
    def __init__(self, *, ping_error: Exception | None = None) -> None:
        self.ping_calls = 0
        self.aclose_calls = 0
        self._ping_error = ping_error

    async def ping(self) -> None:
        self.ping_calls += 1
        if self._ping_error is not None:
            raise self._ping_error

    async def aclose(self) -> None:
        self.aclose_calls += 1


def _install_fake_redis(
    monkeypatch: pytest.MonkeyPatch, client: FakeAsyncRedisClient
) -> list[tuple[str, dict[str, Any]]]:
    calls: list[tuple[str, dict[str, Any]]] = []

    def from_url(url: str, **kwargs: Any) -> FakeAsyncRedisClient:
        calls.append((url, kwargs))
        return client

    monkeypatch.setattr(redis_module, "redis", SimpleNamespace(from_url=from_url))
    return calls


def test_missing_redis_dependency_raises_import_error(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(redis_module, "redis", None)

    with pytest.raises(ImportError):
        RedisDBClient(
            url="redis://localhost:6379/0",
            connect_timeout_seconds=1.0,
            operation_timeout_seconds=1.0,
        )


def test_backend_and_is_connected_before_connect() -> None:
    client = RedisDBClient(
        url="redis://localhost:6379/0",
        connect_timeout_seconds=1.0,
        operation_timeout_seconds=1.0,
    )

    assert client.backend == "redis"
    assert client.is_connected is False


def test_raw_property_raises_when_not_connected() -> None:
    client = RedisDBClient(
        url="redis://localhost:6379/0",
        connect_timeout_seconds=1.0,
        operation_timeout_seconds=1.0,
    )

    with pytest.raises(RuntimeError, match="not connected"):
        _ = client.raw


@pytest.mark.asyncio
async def test_ping_raises_when_not_connected() -> None:
    client = RedisDBClient(
        url="redis://localhost:6379/0",
        connect_timeout_seconds=1.0,
        operation_timeout_seconds=1.0,
    )

    with pytest.raises(RuntimeError, match="not connected"):
        await client.ping()


@pytest.mark.asyncio
async def test_connect_creates_client_and_pings_once(monkeypatch: pytest.MonkeyPatch) -> None:
    fake_client = FakeAsyncRedisClient()
    calls = _install_fake_redis(monkeypatch, fake_client)
    client = RedisDBClient(
        url="redis://localhost:6379/0",
        connect_timeout_seconds=1.0,
        operation_timeout_seconds=1.0,
    )

    await client.connect()
    await client.connect()  # second call must be a no-op, not reconnect

    assert len(calls) == 1
    assert fake_client.ping_calls == 1
    assert client.is_connected is True
    assert client.raw is fake_client


@pytest.mark.asyncio
async def test_connect_closes_and_reraises_on_ping_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake_client = FakeAsyncRedisClient(ping_error=ConnectionError("boom"))
    _install_fake_redis(monkeypatch, fake_client)
    client = RedisDBClient(
        url="redis://localhost:6379/0",
        connect_timeout_seconds=1.0,
        operation_timeout_seconds=1.0,
    )

    with pytest.raises(ConnectionError, match="boom"):
        await client.connect()

    assert fake_client.aclose_calls == 1
    assert client.is_connected is False


@pytest.mark.asyncio
async def test_ping_delegates_to_raw_client(monkeypatch: pytest.MonkeyPatch) -> None:
    fake_client = FakeAsyncRedisClient()
    _install_fake_redis(monkeypatch, fake_client)
    client = RedisDBClient(
        url="redis://localhost:6379/0",
        connect_timeout_seconds=1.0,
        operation_timeout_seconds=1.0,
    )
    await client.connect()

    await client.ping()

    assert fake_client.ping_calls == 2  # once during connect, once explicitly


@pytest.mark.asyncio
async def test_close_when_never_connected_is_a_noop() -> None:
    client = RedisDBClient(
        url="redis://localhost:6379/0",
        connect_timeout_seconds=1.0,
        operation_timeout_seconds=1.0,
    )

    await client.close()  # must not raise

    assert client.is_connected is False


@pytest.mark.asyncio
async def test_close_releases_connected_client(monkeypatch: pytest.MonkeyPatch) -> None:
    fake_client = FakeAsyncRedisClient()
    _install_fake_redis(monkeypatch, fake_client)
    client = RedisDBClient(
        url="redis://localhost:6379/0",
        connect_timeout_seconds=1.0,
        operation_timeout_seconds=1.0,
    )
    await client.connect()

    await client.close()

    assert fake_client.aclose_calls == 1
    assert client.is_connected is False
    with pytest.raises(RuntimeError, match="not connected"):
        _ = client.raw
