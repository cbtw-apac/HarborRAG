"""Per-tenant chat clients, cached against the catalog fingerprint.

A tenant that configured nothing keeps using the process-wide client, so every
existing deployment behaves exactly as it did before. A tenant that configured
its own models gets its own client, built from its own catalog with its own
keys.

Why a fingerprint rather than an invalidation channel: a rotated key or a new
model must take effect without a restart, and the API runs as more than one
process, so there is nobody to tell. Instead every request asks the catalog
port for a cheap stamp over the tenant's stored rows and compares it with the
stamp the cached client was built from. Same stamp, same client; a different
one rebuilds and disposes the old client. The staleness window is therefore
one request, not one deployment.

Every failure degrades to the process-wide client with an ERROR log naming the
tenant. A tenant whose own configuration is broken loses its own models, never
its ability to chat.
"""

from __future__ import annotations

import asyncio
import logging
from collections import OrderedDict
from collections.abc import Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING

from harborrag_core.ports.model_catalog import TenantChatCatalog
from harborrag_core.ports.model_clients import AsyncHarborChatClientProtocol
from harborrag_runtime.config.settings import RuntimeSettings

from .tenant_config import build_tenant_chat_config
from .tenant_models import TenantModelSources

if TYPE_CHECKING:
    from harborrag_adapters.models.chat import HarborChatClientConfig

logger = logging.getLogger("harborrag.runtime.chat.tenant")

type TenantClientBuilder = Callable[[HarborChatClientConfig], AsyncHarborChatClientProtocol]


@dataclass(frozen=True, slots=True)
class TenantChatResolution:
    """Which client answers this tenant, and the catalog that bounds it.

    ``client`` is ``None`` when the tenant uses the process-wide client, and
    ``catalog`` is ``None`` whenever the process-wide catalog is the authority
    on which model names are allowed -- including when a tenant's own
    configuration failed to build, because the shared client is what will
    actually serve the turn.
    """

    client: AsyncHarborChatClientProtocol | None = None
    catalog: TenantChatCatalog | None = None


_SHARED = TenantChatResolution()


@dataclass(slots=True)
class _Entry:
    fingerprint: str
    resolution: TenantChatResolution


class TenantChatClients:
    """Resolve, cache, and dispose one chat client per configured tenant."""

    def __init__(
        self,
        settings: RuntimeSettings,
        sources: TenantModelSources,
        *,
        builder: TenantClientBuilder,
    ) -> None:
        self._settings = settings
        self._sources = sources
        self._builder = builder
        self._entries: OrderedDict[str, _Entry] = OrderedDict()
        self._max_entries = settings.chat_tenant_client_cache_size
        self._lock = asyncio.Lock()

    async def resolve(self, tenant_id: str | None) -> TenantChatResolution:
        """Return the client for ``tenant_id``, rebuilding on a changed fingerprint.

        The fingerprint is read outside the lock so a warm cache costs one
        cheap port call and no contention; the lock is taken only to build,
        and re-checks under it so concurrent first requests for one tenant
        build a single client.
        """

        if not tenant_id:
            return _SHARED
        try:
            fingerprint = await self._sources.catalog.fingerprint(tenant_id)
        except Exception:
            logger.error(
                "Reading the model-catalog fingerprint failed for tenant=%s; "
                "answering from the process-wide chat catalog",
                tenant_id,
                exc_info=True,
            )
            return _SHARED
        cached = self._fresh(tenant_id, fingerprint)
        if cached is not None:
            return cached
        async with self._lock:
            cached = self._fresh(tenant_id, fingerprint)
            if cached is not None:
                return cached
            return await self._rebuild(tenant_id, fingerprint)

    async def aclose(self) -> None:
        """Dispose every cached tenant client; the shared one is not ours."""

        async with self._lock:
            entries = list(self._entries.values())
            self._entries.clear()
        for entry in entries:
            await _dispose(entry.resolution.client)

    def _fresh(self, tenant_id: str, fingerprint: str) -> TenantChatResolution | None:
        entry = self._entries.get(tenant_id)
        if entry is None or entry.fingerprint != fingerprint:
            return None
        self._entries.move_to_end(tenant_id)
        return entry.resolution

    async def _rebuild(self, tenant_id: str, fingerprint: str) -> TenantChatResolution:
        resolution = await self._build(tenant_id)
        replaced = self._entries.pop(tenant_id, None)
        self._entries[tenant_id] = _Entry(fingerprint, resolution)
        evicted: list[_Entry] = []
        while len(self._entries) > self._max_entries:
            _, dropped = self._entries.popitem(last=False)
            evicted.append(dropped)
        if replaced is not None:
            await _dispose(replaced.resolution.client)
        for entry in evicted:
            logger.info("Evicted a cached tenant chat client to stay within the cache bound")
            await _dispose(entry.resolution.client)
        return resolution

    async def _build(self, tenant_id: str) -> TenantChatResolution:
        try:
            catalog = await self._sources.catalog.chat_catalog(tenant_id)
        except Exception:
            logger.error(
                "Loading the chat catalog failed for tenant=%s; answering from the "
                "process-wide chat catalog",
                tenant_id,
                exc_info=True,
            )
            return _SHARED
        if catalog.is_empty():
            return _SHARED
        try:
            config = await build_tenant_chat_config(
                catalog,
                secrets=self._sources.secrets,
                shared_config_path=self._settings.model_config_path,
            )
            return TenantChatResolution(self._builder(config), catalog)
        except Exception:
            # Either the stored rows do not satisfy this process's model policy
            # (an unlisted provider, an unsupported field) or a secret ref would
            # not resolve. Both are the tenant's configuration, not this turn's
            # fault, so the shared catalog answers and the operator gets the
            # detail -- including which of the two it was -- in the log.
            logger.error(
                "Building the chat client failed for tenant=%s; answering from the "
                "process-wide chat catalog",
                tenant_id,
                exc_info=True,
            )
            return _SHARED


async def _dispose(client: AsyncHarborChatClientProtocol | None) -> None:
    """Release one tenant client's connection pool; a failure is never fatal."""

    if client is None:
        return
    try:
        await client.aclose()
    except Exception:
        logger.warning("Disposing a tenant chat client failed", exc_info=True)


__all__ = ["TenantChatClients", "TenantChatResolution", "TenantClientBuilder"]
