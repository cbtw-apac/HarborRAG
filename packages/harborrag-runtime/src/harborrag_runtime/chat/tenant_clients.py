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
one rebuilds and retires the old client. The staleness window is therefore
one request, not one deployment.

Builds are single-flight per tenant, never across tenants: one tenant's slow
catalog read or secret resolve must not hold up a cold turn for any other. A
retired client -- replaced or evicted -- is closed only once no turn is still
using it, so a key rotation or LRU pressure never cuts a stream off mid-answer.

Every failure degrades to the process-wide client with an ERROR log naming the
tenant. A tenant whose own configuration is broken loses its own models, never
its ability to chat. A configuration failure is cached like a client, because
its cause lives in the fingerprinted rows. A failing catalog or secrets port
is remembered only for a few seconds -- long enough that a hanging port costs
one timeout, not one per queued turn, and short enough that the tenant is back
on its own models soon after the port recovers rather than when its rows
happen to change.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections import OrderedDict
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from harborrag_core.ports.model_catalog import TenantChatCatalog
from harborrag_core.ports.model_clients import AsyncHarborChatClientProtocol
from harborrag_runtime.config.settings import RuntimeSettings

from .tenant_config import TenantChatConfigError, build_tenant_chat_config
from .tenant_models import TenantModelSources

if TYPE_CHECKING:
    from harborrag_adapters.models.chat import HarborChatClientConfig
    from harborrag_core.models.chat import HarborChatStreamChunk

logger = logging.getLogger("harborrag.runtime.chat.tenant")

type TenantClientBuilder = Callable[[HarborChatClientConfig], AsyncHarborChatClientProtocol]

# How long a tenant whose catalog or secrets port just failed is answered by
# the shared client before the next turn tries its own configuration again.
_TRANSIENT_FAILURE_RETRY_SECONDS = 10.0


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


class _LeasedClient:
    """A cached tenant client that is closed only once no call is using it.

    Every call counts itself in for its whole duration -- a stream until it is
    exhausted or closed -- so retiring the client while a turn still streams
    through it defers the close to that turn's end instead of failing it with
    a closed connection pool. Anything the protocol does not route through a
    counted call is passed straight to the wrapped client.
    """

    def __init__(self, client: AsyncHarborChatClientProtocol) -> None:
        self.wrapped = client
        self._in_flight = 0
        self._retired = False
        self._closed = False

    def __getattr__(self, name: str) -> Any:
        return getattr(self.wrapped, name)

    async def achat(self, *args: Any, **kwargs: Any) -> Any:
        async with self._counted():
            return await self.wrapped.achat(*args, **kwargs)

    async def achat_structured(self, *args: Any, **kwargs: Any) -> Any:
        async with self._counted():
            return await self.wrapped.achat_structured(*args, **kwargs)

    async def achat_structured_usage(self, **kwargs: Any) -> Any:
        async with self._counted():
            return await self.wrapped.achat_structured_usage(**kwargs)

    async def astream(self, *args: Any, **kwargs: Any) -> AsyncIterator[HarborChatStreamChunk]:
        # Counted inside the generator body, so a stream that is created but
        # never iterated holds nothing open.
        async with self._counted():
            async for chunk in self.wrapped.astream(*args, **kwargs):
                yield chunk

    async def aclose(self) -> None:
        """Close now, in use or not; only shutdown calls this directly."""

        if self._closed:
            return
        self._closed = True
        await _dispose(self.wrapped)

    async def retire(self) -> None:
        """Stop handing this client out; close it now if idle, else on release."""

        self._retired = True
        await self._close_if_idle()

    @asynccontextmanager
    async def _counted(self) -> AsyncIterator[None]:
        self._in_flight += 1
        try:
            yield
        finally:
            self._in_flight -= 1
            await self._close_if_idle()

    async def _close_if_idle(self) -> None:
        if self._retired and self._in_flight == 0:
            await self.aclose()


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
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._settings = settings
        self._sources = sources
        self._builder = builder
        self._entries: OrderedDict[str, _Entry] = OrderedDict()
        self._max_entries = settings.chat_tenant_client_cache_size
        # One lock per tenant, never pruned: dropping a lock somebody still
        # waits on would let a second build for that tenant start beside it.
        self._locks: dict[str, asyncio.Lock] = {}
        # Bumped by ``aclose`` so a build already in flight then is disposed
        # rather than installed into a cache nobody will close again.
        self._generation = 0
        # tenant -> (fingerprint, retry deadline) after a transient failure.
        # Kept apart from ``_entries`` so a failure never evicts a good client.
        self._retry_after: dict[str, tuple[str, float]] = {}
        self._clock = clock

    async def resolve(self, tenant_id: str | None) -> TenantChatResolution:
        """Return the client for ``tenant_id``, rebuilding on a changed fingerprint.

        The fingerprint is read outside any lock so a warm cache costs one
        cheap port call and no contention. A miss takes only this tenant's
        lock, and re-checks under it so concurrent first requests for one
        tenant build a single client while other tenants build in parallel.
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
        async with self._locks.setdefault(tenant_id, asyncio.Lock()):
            cached = self._fresh(tenant_id, fingerprint)
            if cached is not None:
                return cached
            return await self._rebuild(tenant_id, fingerprint)

    def _fresh(self, tenant_id: str, fingerprint: str) -> TenantChatResolution | None:
        entry = self._entries.get(tenant_id)
        if entry is not None and entry.fingerprint == fingerprint:
            self._entries.move_to_end(tenant_id)
            return entry.resolution
        failed = self._retry_after.get(tenant_id)
        if failed is not None and failed[0] == fingerprint and self._clock() < failed[1]:
            return self._fallback(tenant_id)
        return None

    def _fallback(self, tenant_id: str) -> TenantChatResolution:
        """What answers while a transient failure waits out its retry pause.

        The client this tenant already had, if any -- its rows changed, but
        nothing newer could be built yet -- and the process-wide one otherwise.
        """

        entry = self._entries.get(tenant_id)
        return _SHARED if entry is None else entry.resolution

    async def aclose(self) -> None:
        """Dispose every cached tenant client; the shared one is not ours."""

        self._generation += 1
        entries = list(self._entries.values())
        self._entries.clear()
        for entry in entries:
            await _dispose(entry.resolution.client)

    async def _rebuild(self, tenant_id: str, fingerprint: str) -> TenantChatResolution:
        generation = self._generation
        resolution, cacheable = await self._build(tenant_id)
        if generation != self._generation:
            # Closed while this build was in flight: nobody would close it.
            await _dispose(resolution.client)
            return _SHARED
        if not cacheable:
            # Transient: nothing is inserted, so nothing is evicted or retired.
            deadline = self._clock() + _TRANSIENT_FAILURE_RETRY_SECONDS
            self._retry_after[tenant_id] = (fingerprint, deadline)
            return self._fallback(tenant_id)
        self._retry_after.pop(tenant_id, None)
        # Make room and retire first, then insert: nothing awaits between the
        # insert and the return, so no concurrent eviction can retire this
        # client before the caller has started using it. Concurrent rebuilds
        # can therefore overshoot the bound briefly; the next one trims it.
        retired = [self._entries.pop(tenant_id, None)]
        while len(self._entries) >= self._max_entries:
            _, dropped = self._entries.popitem(last=False)
            logger.info("Evicted a cached tenant chat client to stay within the cache bound")
            retired.append(dropped)
        for entry in retired:
            if entry is not None:
                await _retire(entry.resolution.client)
        self._entries[tenant_id] = _Entry(fingerprint, resolution)
        return resolution

    async def _build(self, tenant_id: str) -> tuple[TenantChatResolution, bool]:
        """Build this tenant's resolution, and whether it may be cached."""

        try:
            catalog = await self._sources.catalog.chat_catalog(tenant_id)
        except Exception:
            logger.error(
                "Loading the chat catalog failed for tenant=%s; answering from the "
                "process-wide chat catalog for now; a later turn retries",
                tenant_id,
                exc_info=True,
            )
            return _SHARED, False
        if catalog.is_empty():
            return _SHARED, True
        try:
            config = await build_tenant_chat_config(
                catalog,
                secrets=self._sources.secrets,
                shared_config_path=self._settings.model_config_path,
            )
        except TenantChatConfigError:
            # The stored rows do not satisfy this process's model policy (an
            # unlisted provider, an unsupported field). That is the tenant's
            # configuration, not this turn's fault, so it is cached until the
            # rows -- and with them the fingerprint -- change.
            logger.error(
                "Building the chat client failed for tenant=%s; answering from the "
                "process-wide chat catalog",
                tenant_id,
                exc_info=True,
            )
            return _SHARED, True
        except Exception:
            # A secret ref that would not resolve, or the shared model config
            # would not load: possibly transient, so the next turn retries.
            logger.error(
                "Resolving the chat configuration failed for tenant=%s; answering "
                "from the process-wide chat catalog for now; a later turn retries",
                tenant_id,
                exc_info=True,
            )
            return _SHARED, False
        try:
            client = self._builder(config)
        except Exception:
            # The operator's provider and endpoint policy is enforced when the
            # client is constructed; a rejection there is configuration too.
            logger.error(
                "Building the chat client failed for tenant=%s; answering from the "
                "process-wide chat catalog",
                tenant_id,
                exc_info=True,
            )
            return _SHARED, True
        return TenantChatResolution(_LeasedClient(client), catalog), True


async def _retire(client: AsyncHarborChatClientProtocol | None) -> None:
    """Retire one cached client: closed now when idle, else when its last call ends."""

    if isinstance(client, _LeasedClient):
        await client.retire()
    else:
        await _dispose(client)


async def _dispose(client: AsyncHarborChatClientProtocol | None) -> None:
    """Release one tenant client's connection pool; a failure is never fatal."""

    if client is None:
        return
    try:
        await client.aclose()
    except Exception:
        logger.warning("Disposing a tenant chat client failed", exc_info=True)


__all__ = ["TenantChatClients", "TenantChatResolution", "TenantClientBuilder"]
