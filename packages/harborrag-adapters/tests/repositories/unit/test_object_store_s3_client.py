from __future__ import annotations

import types
from typing import Any

import pytest

from harborrag_adapters.repositories.object_store.s3 import client as client_module
from harborrag_adapters.repositories.object_store.s3.client import S3DBClient


class FakeRawClient:
    def __init__(self) -> None:
        self.list_buckets_calls = 0

    async def list_buckets(self) -> dict[str, Any]:
        self.list_buckets_calls += 1
        return {"Buckets": []}


class FakeClientManager:
    def __init__(self, client: FakeRawClient) -> None:
        self._client = client
        self.entered = False
        self.exited = False

    async def __aenter__(self) -> FakeRawClient:
        self.entered = True
        return self._client

    async def __aexit__(self, *exc_info: object) -> None:
        self.exited = True


class FakeSession:
    def __init__(self) -> None:
        self.client_kwargs: dict[str, Any] | None = None
        self.manager: FakeClientManager | None = None

    def client(self, service_name: str, **kwargs: Any) -> FakeClientManager:
        assert service_name == "s3"
        self.client_kwargs = kwargs
        self.manager = FakeClientManager(FakeRawClient())
        return self.manager


class FailingEnterClientManager(FakeClientManager):
    """A client manager whose __aenter__ fails, like a transient handshake error."""

    async def __aenter__(self) -> FakeRawClient:
        self.entered = True
        raise ConnectionError("simulated handshake failure")


class FakeFailingSession(FakeSession):
    def client(self, service_name: str, **kwargs: Any) -> FakeClientManager:
        assert service_name == "s3"
        self.client_kwargs = kwargs
        self.manager = FailingEnterClientManager(FakeRawClient())
        return self.manager


def make_client() -> S3DBClient:
    return S3DBClient(
        endpoint_url="http://localhost:9000",
        region="us-east-1",
        access_key_id="ak",
        secret_access_key="sk",
        session_token=None,
    )


def test_missing_aioboto3_raises_import_error(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(client_module, "aioboto3", None)

    with pytest.raises(ImportError):
        make_client()


def test_raw_before_connect_raises_runtime_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(client_module, "aioboto3", types.SimpleNamespace(Session=FakeSession))
    db = make_client()

    assert db.backend == "s3"
    with pytest.raises(RuntimeError, match="not connected"):
        _ = db.raw


@pytest.mark.asyncio
async def test_connect_is_idempotent_and_filters_none_settings(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake_session = FakeSession()
    monkeypatch.setattr(
        client_module, "aioboto3", types.SimpleNamespace(Session=lambda: fake_session)
    )
    db = S3DBClient(
        endpoint_url="http://localhost:9000",
        region=None,
        access_key_id=None,
        secret_access_key=None,
        session_token=None,
    )

    await db.connect()
    assert fake_session.client_kwargs == {"endpoint_url": "http://localhost:9000"}
    assert db.raw is not None

    # A second connect() is a no-op and must not build a new client.
    await db.connect()
    assert fake_session.manager is not None
    assert fake_session.manager.entered is True


@pytest.mark.asyncio
async def test_ping_calls_list_buckets(monkeypatch: pytest.MonkeyPatch) -> None:
    fake_session = FakeSession()
    monkeypatch.setattr(
        client_module, "aioboto3", types.SimpleNamespace(Session=lambda: fake_session)
    )
    db = make_client()
    await db.connect()

    await db.ping()

    assert fake_session.manager is not None
    assert fake_session.manager._client.list_buckets_calls == 1


@pytest.mark.asyncio
async def test_close_after_connect_exits_the_client_manager(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake_session = FakeSession()
    monkeypatch.setattr(
        client_module, "aioboto3", types.SimpleNamespace(Session=lambda: fake_session)
    )
    db = make_client()
    await db.connect()

    await db.close()

    assert fake_session.manager is not None
    assert fake_session.manager.exited is True
    with pytest.raises(RuntimeError):
        _ = db.raw


@pytest.mark.asyncio
async def test_close_without_connect_is_a_no_op(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(client_module, "aioboto3", types.SimpleNamespace(Session=FakeSession))
    db = make_client()

    await db.close()

    with pytest.raises(RuntimeError):
        _ = db.raw


@pytest.mark.asyncio
async def test_connect_cleans_up_client_manager_when_aenter_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A failed handshake must not leak the underlying client-manager/session."""
    fake_session = FakeFailingSession()
    monkeypatch.setattr(
        client_module, "aioboto3", types.SimpleNamespace(Session=lambda: fake_session)
    )
    db = make_client()

    with pytest.raises(ConnectionError, match="simulated handshake failure"):
        await db.connect()

    # connect() must have called close() on itself so the manager reference
    # (and whatever aiohttp session it opened internally) isn't retained.
    assert db._client_manager is None
    assert db._client is None
    with pytest.raises(RuntimeError, match="not connected"):
        _ = db.raw

    # A subsequent connect() attempt must be possible (not stuck thinking a
    # stale manager is still live) and should succeed against a healthy session.
    fake_session.manager = None
    healthy_manager = FakeClientManager(FakeRawClient())
    fake_session.client = lambda service_name, **kwargs: healthy_manager  # type: ignore[method-assign]
    await db.connect()
    assert db.raw is healthy_manager._client
