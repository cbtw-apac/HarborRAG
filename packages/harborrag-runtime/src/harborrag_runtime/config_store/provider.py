"""Per-process cache of tenant settings, refreshed by change notifications and a revision poll."""

from __future__ import annotations

import asyncio
import contextlib
import logging

from harborrag_core.config_store import (
    EffectiveSettings,
    SettingsLevel,
    SettingsRegistry,
    SettingsSnapshot,
    resolve_effective,
)
from harborrag_core.contracts.errors import HarborUnavailableError
from harborrag_core.ports.config_store import SettingsChangeFeed, SettingsStore

logger = logging.getLogger("harborrag.runtime.config_store")


class CachedConfigProvider:
    """Serve tenant settings from memory and keep them fresh.

    A tenant is loaded on first read. A feed notification marks one tenant (or all) for reload on
    the next read; a poll of ``latest_revision`` covers a missing feed and missed notifications.
    A reload that fails serves the last snapshot and logs ``settings_cache_stale``.

    Args:
        store: Durable settings store.
        registry: Keys and defaults to resolve over.
        feed: Change notifications; ``None`` leaves the poll as the only refresh.
        poll_seconds: Interval of the revision poll.
    """

    def __init__(
        self,
        store: SettingsStore,
        registry: SettingsRegistry,
        *,
        feed: SettingsChangeFeed | None = None,
        poll_seconds: float = 5.0,
    ) -> None:
        self._store = store
        self._registry = registry
        self._feed = feed
        self._poll_seconds = poll_seconds
        self._snapshots: dict[str, SettingsSnapshot] = {}
        self._marked_for_reload: set[str] = set()
        self._locks: dict[str, asyncio.Lock] = {}
        self._tasks: list[asyncio.Task[None]] = []

    @property
    def has_feed(self) -> bool:
        """Whether change notifications are attached."""
        return self._feed is not None

    async def start(self) -> None:
        """Ping the store, then start the feed and the revision poll.

        Raises:
            HarborUnavailableError: The store is unreachable.
        """
        try:
            await self._store.ping()
        except Exception as error:  # noqa: BLE001 - any store failure fails startup the same way
            raise HarborUnavailableError("settings store is unreachable") from error
        if self._feed is not None:
            self._tasks.append(asyncio.create_task(self._feed.run(self.invalidate)))
        self._tasks.append(asyncio.create_task(self._poll()))

    async def aclose(self) -> None:
        """Stop the feed and the poll."""
        tasks, self._tasks = self._tasks, []
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)

    async def snapshot(self, tenant_id: str) -> SettingsSnapshot:
        """Return the tenant's overrides.

        Raises:
            HarborUnavailableError: The first load of the tenant failed.
        """
        cached = self._fresh(tenant_id)
        if cached is not None:
            return cached
        async with self._lock(tenant_id):
            cached = self._fresh(tenant_id)
            if cached is not None:
                return cached
            return await self._reload(tenant_id)

    async def effective(self, tenant_id: str, level: SettingsLevel) -> EffectiveSettings:
        """Return every key resolved at ``level``."""
        return resolve_effective(self._registry, await self.snapshot(tenant_id), level)

    def invalidate(self, tenant_id: str | None = None) -> None:
        """Reload the tenant, or every cached tenant when ``None``, on its next read."""
        if tenant_id is None:
            self._marked_for_reload.update(self._snapshots)
        elif tenant_id in self._snapshots:
            self._marked_for_reload.add(tenant_id)

    def _fresh(self, tenant_id: str) -> SettingsSnapshot | None:
        if tenant_id in self._marked_for_reload:
            return None
        return self._snapshots.get(tenant_id)

    def _lock(self, tenant_id: str) -> asyncio.Lock:
        return self._locks.setdefault(tenant_id, asyncio.Lock())

    async def _reload(self, tenant_id: str) -> SettingsSnapshot:
        """Load the tenant; fall back to the cached snapshot when it exists."""
        self._marked_for_reload.discard(tenant_id)
        try:
            loaded = await self._store.load(tenant_id)
        except Exception as error:  # noqa: BLE001 - any store failure is handled the same way
            return self._stale_or_unavailable(tenant_id, error)
        self._snapshots[tenant_id] = loaded
        return loaded

    def _stale_or_unavailable(self, tenant_id: str, error: Exception) -> SettingsSnapshot:
        cached = self._snapshots.get(tenant_id)
        if cached is None:
            raise HarborUnavailableError(
                f"settings of tenant {tenant_id} cannot be loaded"
            ) from error
        self._marked_for_reload.add(tenant_id)
        logger.warning(
            "settings_cache_stale tenant_id=%s error_type=%s", tenant_id, type(error).__name__
        )
        return cached

    async def _poll(self) -> None:
        while True:
            await asyncio.sleep(self._poll_seconds)
            for tenant_id in tuple(self._snapshots):
                await self._refresh_if_revision_moved(tenant_id)

    async def _refresh_if_revision_moved(self, tenant_id: str) -> None:
        cached = self._snapshots.get(tenant_id)
        try:
            latest = await self._store.latest_revision(tenant_id)
        except Exception as error:  # noqa: BLE001 - a failed poll keeps serving the snapshot
            if cached is not None:
                self._stale_or_unavailable(tenant_id, error)
            return
        if cached is not None and latest == cached.revision:
            return
        async with self._lock(tenant_id):
            with contextlib.suppress(HarborUnavailableError):
                await self._reload(tenant_id)
