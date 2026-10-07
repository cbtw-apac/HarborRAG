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

A turn *leases* its client for as long as it runs. A rebuild or an eviction retires
the old entry instead of closing it, and the last lease to be released closes it, so
a rotated key never pulls a connection pool out from under a turn still streaming.
A failure to *reach* the catalog or the secrets store is not cached: the next request
retries. A configuration this process refuses is cached against its fingerprint,
because it cannot change until the rows do.
"""

from __future__ import annotations

import asyncio
import logging
from collections import OrderedDict
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import TYPE_CHECKING

from harborrag_core.ports.model_catalog import TenantChatCatalog
from harborrag_core.ports.model_clients import AsyncHarborChatClientProtocol
from harborrag_runtime.config.settings import RuntimeSettings

from .tenant_config import TenantChatConfigError, build_tenant_chat_config
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
    """One cached resolution and the turns currently using its client."""

    fingerprint: str
    resolution: TenantChatResolution
    leases: int = 0
    retired: bool = False
    disposed: bool = False

    def retire(self) -> bool:
        """Stop handing this entry out; return whether nobody is using it now."""

        self.retired = True
        return self.leases == 0


class TenantChatClients:
    """Resolve, cache, lease, and dispose one chat client per configured tenant."""

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
        """Return a snapshot of ``tenant_id``'s resolution, for its catalog.

        The client in a snapshot may be closed as soon as this returns, when a
        newer fingerprint replaces it. A caller that is going to *use* the client
        takes a ``lease`` instead.
        """

        async with self.lease(tenant_id) as resolution:
            return resolution

    @asynccontextmanager
    async def lease(self, tenant_id: str | None) -> AsyncIterator[TenantChatResolution]:
        """Hold ``tenant_id``'s resolution open for the duration of one turn.

        The fingerprint is read outside the lock so a warm cache costs one cheap
        port call and no contention; the lock is taken only to build, and
        re-checks under it so concurrent first requests build a single client.
        """

        entry = await self._acquire(tenant_id)
        if entry is None:
            yield _SHARED
            return
        try:
            yield entry.resolution
        finally:
            entry.leases -= 1
            if entry.retired and entry.leases == 0:
                await _dispose(entry)

    async def aclose(self) -> None:
        """Retire every cached tenant client; idle ones close now, leased ones on release.

        The shared client is not ours.
        """

        async with self._lock:
            entries = list(self._entries.values())
            self._entries.clear()
        for entry in entries:
            if entry.retire():
                await _dispose(entry)

    async def _acquire(self, tenant_id: str | None) -> _Entry | None:
        """Find or build the entry and count a lease on it before anything awaits.

        Selecting the entry and counting the lease happen in one synchronous step,
        so a concurrent rebuild cannot retire-and-close an entry between the two.
        """

        if not tenant_id:
            return None
        fingerprint = await self._fingerprint(tenant_id)
        if fingerprint is None:
            return None
        entry = self._fresh(tenant_id, fingerprint)
        if entry is not None:
            entry.leases += 1
            return entry
        stale: list[_Entry] = []
        async with self._lock:
            entry = self._fresh(tenant_id, fingerprint)
            if entry is None:
                entry, stale = await self._rebuild(tenant_id, fingerprint)
            if entry is not None:
                entry.leases += 1
        for dropped in stale:
            await _dispose(dropped)
        return entry

    async def _fingerprint(self, tenant_id: str) -> str | None:
        try:
            return await self._sources.catalog.fingerprint(tenant_id)
        except Exception:
            logger.error(
                "Reading the model-catalog fingerprint failed for tenant=%s; "
                "answering from the process-wide chat catalog",
                tenant_id,
                exc_info=True,
            )
            return None

    def _fresh(self, tenant_id: str, fingerprint: str) -> _Entry | None:
        entry = self._entries.get(tenant_id)
        if entry is None or entry.fingerprint != fingerprint:
            return None
        self._entries.move_to_end(tenant_id)
        return entry

    async def _rebuild(
        self, tenant_id: str, fingerprint: str
    ) -> tuple[_Entry | None, list[_Entry]]:
        """Cache a new entry, retiring the one it replaces and any it evicts.

        Returns the new entry -- ``None`` after a transient failure, which caches
        nothing and leaves the previous entry for the next request to retry -- and
        the retired entries nobody holds, which the caller closes.
        """

        resolution = await self._build(tenant_id)
        if resolution is None:
            return None, []
        entry = _Entry(fingerprint, resolution)
        retired = [self._entries.pop(tenant_id)] if tenant_id in self._entries else []
        self._entries[tenant_id] = entry
        while len(self._entries) > self._max_entries:
            _, dropped = self._entries.popitem(last=False)
            logger.info("Evicted a cached tenant chat client to stay within the cache bound")
            retired.append(dropped)
        return entry, [item for item in retired if item.retire()]

    async def _build(self, tenant_id: str) -> TenantChatResolution | None:
        """Build one tenant's resolution; ``None`` means "could not ask, retry".

        Only failures that are a property of the stored rows are worth caching: an
        unreachable catalog or secrets store says nothing about them.
        """

        try:
            catalog = await self._sources.catalog.chat_catalog(tenant_id)
        except Exception:
            logger.error(
                "Loading the chat catalog failed for tenant=%s; answering from the "
                "process-wide chat catalog and retrying on the next request",
                tenant_id,
                exc_info=True,
            )
            return None
        if catalog.is_empty():
            return _SHARED
        try:
            config = await build_tenant_chat_config(
                catalog,
                secrets=self._sources.secrets,
                shared_config_path=self._settings.model_config_path,
            )
        except TenantChatConfigError:
            logger.error(
                "The stored chat configuration was refused for tenant=%s; answering "
                "from the process-wide chat catalog",
                tenant_id,
                exc_info=True,
            )
            return _SHARED
        except Exception:
            logger.error(
                "Resolving the chat configuration's secrets failed for tenant=%s; "
                "answering from the process-wide chat catalog and retrying on the "
                "next request",
                tenant_id,
                exc_info=True,
            )
            return None
        try:
            return TenantChatResolution(self._builder(config), catalog)
        except Exception:
            # The operator's own provider and endpoint policy is enforced by the
            # client constructor: deterministic for these rows, so cached.
            logger.error(
                "Building the chat client failed for tenant=%s; answering from the "
                "process-wide chat catalog",
                tenant_id,
                exc_info=True,
            )
            return _SHARED


async def _dispose(entry: _Entry) -> None:
    """Release one retired entry's connection pool exactly once; never fatal."""

    if entry.disposed:
        return
    entry.disposed = True
    client = entry.resolution.client
    if client is None:
        return
    try:
        await client.aclose()
    except Exception:
        logger.warning("Disposing a tenant chat client failed", exc_info=True)


__all__ = ["TenantChatClients", "TenantChatResolution", "TenantClientBuilder"]
