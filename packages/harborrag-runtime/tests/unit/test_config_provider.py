"""CachedConfigProvider over a fake store and change feed."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable, Sequence

import pytest

from harborrag_core.config_store import (
    RevisionSource,
    SettingChange,
    SettingsLevel,
    SettingsRegistry,
    SettingsRevision,
    SettingsSnapshot,
)
from harborrag_core.contracts.errors import HarborUnavailableError
from harborrag_runtime.config_store import CachedConfigProvider

pytestmark = pytest.mark.unit


class FakeSettingsStore:
    """In-memory store counting loads; ``fail`` makes every call raise."""

    def __init__(self) -> None:
        self.revisions: dict[str, int] = {}
        self.top_k: dict[str, int] = {}
        self.loads: list[str] = []
        self.fail = False

    def set_top_k(self, tenant_id: str, value: int) -> None:
        self.top_k[tenant_id] = value
        self.revisions[tenant_id] = self.revisions.get(tenant_id, 0) + 1

    async def load(self, tenant_id: str) -> SettingsSnapshot:
        if self.fail:
            raise ConnectionError("store down")
        self.loads.append(tenant_id)
        overrides = (
            {"tenant": {"retrieval.top_k": self.top_k[tenant_id]}}
            if tenant_id in self.top_k
            else {}
        )
        return SettingsSnapshot(tenant_id, self.revisions.get(tenant_id, 0), overrides)

    async def latest_revision(self, tenant_id: str) -> int:
        if self.fail:
            raise ConnectionError("store down")
        return self.revisions.get(tenant_id, 0)

    async def revision(self, tenant_id: str, revision: int) -> SettingsRevision | None:
        return None

    async def write(
        self,
        tenant_id: str,
        changes: Sequence[SettingChange],
        *,
        expected_revision: int,
        source: RevisionSource,
        created_by: str,
    ) -> int:
        raise NotImplementedError

    async def ping(self) -> None:
        if self.fail:
            raise ConnectionError("store down")


class FakeFeed:
    """Change feed a test drives by hand."""

    def __init__(self) -> None:
        self.on_change: Callable[[str | None], None] | None = None
        self.started = asyncio.Event()

    async def run(self, on_change: Callable[[str | None], None]) -> None:
        self.on_change = on_change
        self.started.set()
        await asyncio.Event().wait()


def _provider(store: FakeSettingsStore, **kwargs: object) -> CachedConfigProvider:
    return CachedConfigProvider(store, SettingsRegistry.default(), **kwargs)  # type: ignore[arg-type]


async def _top_k(provider: CachedConfigProvider, tenant_id: str) -> object:
    effective = await provider.effective(tenant_id, SettingsLevel("tenant"))
    return effective.values["retrieval.top_k"].value


@pytest.mark.asyncio
async def test_first_read_loads_once() -> None:
    store = FakeSettingsStore()
    provider = _provider(store)

    await provider.snapshot("acme")
    await provider.snapshot("acme")

    assert store.loads == ["acme"]


@pytest.mark.asyncio
async def test_concurrent_first_reads_coalesce() -> None:
    store = FakeSettingsStore()
    provider = _provider(store)

    await asyncio.gather(*(provider.snapshot("acme") for _ in range(5)))

    assert store.loads == ["acme"]


@pytest.mark.asyncio
async def test_invalidate_reloads_on_next_read() -> None:
    store = FakeSettingsStore()
    provider = _provider(store)
    assert await _top_k(provider, "acme") == 5

    store.set_top_k("acme", 9)
    provider.invalidate("acme")

    assert await _top_k(provider, "acme") == 9


@pytest.mark.asyncio
async def test_notification_invalidates_one_tenant() -> None:
    store = FakeSettingsStore()
    feed = FakeFeed()
    provider = _provider(store, feed=feed, poll_seconds=3600)
    await provider.start()
    await feed.started.wait()
    await provider.snapshot("acme")
    await provider.snapshot("globex")
    assert feed.on_change is not None

    feed.on_change("acme")
    await provider.snapshot("acme")
    await provider.snapshot("globex")
    await provider.aclose()

    assert store.loads == ["acme", "globex", "acme"]


@pytest.mark.asyncio
async def test_reconnect_invalidates_all() -> None:
    store = FakeSettingsStore()
    feed = FakeFeed()
    provider = _provider(store, feed=feed, poll_seconds=3600)
    await provider.start()
    await feed.started.wait()
    await provider.snapshot("acme")
    await provider.snapshot("globex")
    assert feed.on_change is not None

    feed.on_change(None)
    await provider.snapshot("acme")
    await provider.snapshot("globex")
    await provider.aclose()

    assert store.loads == ["acme", "globex", "acme", "globex"]


@pytest.mark.asyncio
async def test_poll_detects_new_revision() -> None:
    store = FakeSettingsStore()
    provider = _provider(store, poll_seconds=0.01)
    await provider.start()
    assert await _top_k(provider, "acme") == 5

    store.set_top_k("acme", 9)
    try:
        async with asyncio.timeout(0.5):
            while await _top_k(provider, "acme") != 9:
                await asyncio.sleep(0.005)
    finally:
        await provider.aclose()

    assert store.loads.count("acme") == 2


@pytest.mark.asyncio
async def test_stale_snapshot_served_when_reload_fails(caplog: pytest.LogCaptureFixture) -> None:
    store = FakeSettingsStore()
    provider = _provider(store)
    assert await _top_k(provider, "acme") == 5
    store.fail = True
    provider.invalidate("acme")

    with caplog.at_level(logging.WARNING):
        assert await _top_k(provider, "acme") == 5

    assert "settings_cache_stale tenant_id=acme" in caplog.text


@pytest.mark.asyncio
async def test_stale_snapshot_reloads_after_store_recovers() -> None:
    store = FakeSettingsStore()
    provider = _provider(store)
    await provider.snapshot("acme")
    store.fail = True
    provider.invalidate("acme")
    await provider.snapshot("acme")

    store.fail = False
    store.set_top_k("acme", 9)

    assert await _top_k(provider, "acme") == 9


@pytest.mark.asyncio
async def test_first_load_failure_raises_unavailable() -> None:
    store = FakeSettingsStore()
    store.fail = True
    provider = _provider(store)

    with pytest.raises(HarborUnavailableError):
        await provider.snapshot("acme")


@pytest.mark.asyncio
async def test_start_fails_when_store_unreachable() -> None:
    store = FakeSettingsStore()
    store.fail = True
    provider = _provider(store)

    with pytest.raises(HarborUnavailableError):
        await provider.start()
