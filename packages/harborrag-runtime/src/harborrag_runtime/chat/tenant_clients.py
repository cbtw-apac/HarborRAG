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
from collections.abc import AsyncIterator, Callable, Sequence
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import TYPE_CHECKING

from harborrag_core.observability.log_events import FailureKind, log_event, log_failure
from harborrag_core.ports.model_catalog import TenantChatCatalog
from harborrag_core.ports.model_clients import AsyncHarborChatClientProtocol
from harborrag_runtime.config.settings import RuntimeSettings

from .tenant_config import TenantChatConfigError, build_tenant_chat_config
from .tenant_models import TenantModelSources

if TYPE_CHECKING:
    from harborrag_adapters.models.chat import HarborChatClientConfig
    from harborrag_core.models.chat import (
        HarborChatMessage,
        HarborChatRequest,
        HarborChatResponse,
        HarborChatStreamChunk,
    )
    from harborrag_core.ports.model_clients import StructuredResponseT, StructuredUsageResult

logger = logging.getLogger("harborrag.runtime.chat.tenant")

type TenantClientBuilder = Callable[[HarborChatClientConfig], AsyncHarborChatClientProtocol]

_TRANSIENT_FAILURE_RETRY_SECONDS = 10.0
"""How long the shared client answers a tenant whose catalog or secrets port just failed."""


@dataclass(frozen=True, slots=True)
class TenantChatResolution:
    """Which client answers this tenant, and the catalog that bounds it.

    Attributes:
        client: The tenant's own client, or ``None`` for the process-wide one.
        catalog: The tenant's catalog of allowed model names, or ``None`` when the
            process-wide catalog is the authority. That includes a tenant whose
            own configuration failed to build, because the shared client is what
            will actually serve the turn.
    """

    client: AsyncHarborChatClientProtocol | None = None
    catalog: TenantChatCatalog | None = None


_SHARED = TenantChatResolution()


class _LeasedClient:
    """A cached tenant client that is closed only once no call is using it.

    Every call counts itself in for its whole duration, a stream until it is
    exhausted or closed, so retiring the client while a turn still streams
    through it defers the close to that turn's end instead of failing it with
    a closed connection pool. Anything the protocol does not route through a
    counted call is passed straight to the wrapped client.

    Args:
        client: The tenant's chat client this lease owns and closes.
    """

    def __init__(self, client: AsyncHarborChatClientProtocol) -> None:
        self.wrapped = client
        self._in_flight = 0
        self._retired = False
        self._closed = False

    def __getattr__(self, name: str) -> object:
        """Return any attribute no counted call covers from the wrapped client."""
        return getattr(self.wrapped, name)

    async def achat(
        self,
        messages: Sequence[HarborChatMessage] | None = None,
        *,
        request: HarborChatRequest | None = None,
        model: str | None = None,
        **kwargs: object,
    ) -> HarborChatResponse:
        """Generate one chat response, counted until it returns."""
        async with self._counted():
            return await self.wrapped.achat(messages, request=request, model=model, **kwargs)

    async def achat_structured(
        self,
        messages: Sequence[HarborChatMessage] | None = None,
        *,
        response_model: type[StructuredResponseT],
        request: HarborChatRequest | None = None,
        model: str | None = None,
        max_repair_attempts: int | None = None,
        **kwargs: object,
    ) -> StructuredResponseT:
        """Generate one typed response, counted until it returns."""
        async with self._counted():
            return await self.wrapped.achat_structured(
                messages,
                response_model=response_model,
                request=request,
                model=model,
                max_repair_attempts=max_repair_attempts,
                **kwargs,
            )

    async def achat_structured_usage(
        self,
        *,
        response_model: type[StructuredResponseT],
        request: HarborChatRequest | None = None,
        messages: Sequence[HarborChatMessage] | None = None,
        max_repair_attempts: int | None = None,
    ) -> StructuredUsageResult[StructuredResponseT]:
        """Generate one typed response with its usage, counted until it returns."""
        async with self._counted():
            return await self.wrapped.achat_structured_usage(
                response_model=response_model,
                request=request,
                messages=messages,
                max_repair_attempts=max_repair_attempts,
            )

    async def astream(
        self,
        messages: Sequence[HarborChatMessage] | None = None,
        *,
        request: HarborChatRequest | None = None,
        model: str | None = None,
        **kwargs: object,
    ) -> AsyncIterator[HarborChatStreamChunk]:
        """Stream one chat response, counted until the stream ends or is closed.

        The count starts inside the generator body, so a stream that is created
        but never iterated holds nothing open.
        """
        async with self._counted():
            async for chunk in self.wrapped.astream(
                messages, request=request, model=model, **kwargs
            ):
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
        """Count one call in for the block, then close if the client was retired."""
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
    """One cached resolution and the catalog fingerprint it was built from."""

    fingerprint: str
    resolution: TenantChatResolution


class TenantChatClients:
    """Resolve, cache, and dispose one chat client per configured tenant.

    Each tenant has its own build lock, never pruned, because dropping a lock
    somebody still waits on would let a second build for that tenant start
    beside it. A transient failure is remembered per tenant as the fingerprint
    and the time its retry pause ends. That record is kept apart from the cached
    clients, so a failure never evicts a good client.

    Args:
        settings: Runtime settings holding the cache bound and the shared model config.
        sources: Catalog and secrets ports the tenant configuration is read from.
        builder: Builds one chat client from a tenant's resolved configuration.
        clock: Monotonic clock in seconds, replaceable in tests.
    """

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
        self._locks: dict[str, asyncio.Lock] = {}
        self._generation = 0
        self._retry_after: dict[str, tuple[str, float]] = {}
        self._clock = clock

    async def resolve(self, tenant_id: str | None) -> TenantChatResolution:
        """Return the client for ``tenant_id``, rebuilding on a changed fingerprint.

        The fingerprint is read outside any lock so a warm cache costs one
        cheap port call and no contention. A miss takes only this tenant's
        lock, and re-checks under it so concurrent first requests for one
        tenant build a single client while other tenants build in parallel.
        A fingerprint that cannot be read is logged and answered by the
        process-wide client.

        Args:
            tenant_id: Tenant asking, or ``None`` for the process-wide client.

        Returns:
            The client and catalog that answer this tenant's turn.
        """
        if not tenant_id:
            return _SHARED
        try:
            fingerprint = await self._sources.catalog.fingerprint(tenant_id)
        except Exception as error:  # noqa: BLE001 - chat falls back to the shared client
            log_failure(
                logger, "chat.tenant_catalog_fingerprint_failed", error, tenant_id=tenant_id
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
        """Return the cached or paused resolution for this fingerprint, if any."""
        entry = self._entries.get(tenant_id)
        if entry is not None and entry.fingerprint == fingerprint:
            self._entries.move_to_end(tenant_id)
            return entry.resolution
        failed = self._retry_after.get(tenant_id)
        if failed is not None and failed[0] == fingerprint and self._clock() < failed[1]:
            return self._fallback(tenant_id)
        return None

    def _fallback(self, tenant_id: str) -> TenantChatResolution:
        """Return what answers while a transient failure waits out its retry pause.

        That is the client this tenant already had, if any, because its rows
        changed but nothing newer could be built yet. Otherwise it is the
        process-wide client.
        """
        entry = self._entries.get(tenant_id)
        return _SHARED if entry is None else entry.resolution

    async def aclose(self) -> None:
        """Dispose every cached tenant client; the shared one is not ours.

        A build still in flight when this runs is disposed when it finishes,
        rather than installed into a cache nobody will close again.
        """
        self._generation += 1
        entries = list(self._entries.values())
        self._entries.clear()
        for entry in entries:
            await _dispose(entry.resolution.client)

    async def _rebuild(self, tenant_id: str, fingerprint: str) -> TenantChatResolution:
        """Build this tenant's resolution and install it in the cache.

        A build that finishes after ``aclose`` is disposed, because nobody would
        close it. A transient failure inserts nothing, so nothing is evicted or
        retired, and only starts the tenant's retry pause. Otherwise the cache
        makes room and retires first, then inserts. Nothing awaits between the
        insert and the return, so no concurrent eviction can retire this client
        before the caller has started using it. Concurrent rebuilds can
        therefore overshoot the bound briefly, and the next one trims it.
        """
        generation = self._generation
        resolution, cacheable = await self._build(tenant_id)
        if generation != self._generation:
            await _dispose(resolution.client)
            return _SHARED
        if not cacheable:
            deadline = self._clock() + _TRANSIENT_FAILURE_RETRY_SECONDS
            self._retry_after[tenant_id] = (fingerprint, deadline)
            return self._fallback(tenant_id)
        self._retry_after.pop(tenant_id, None)
        retired = [self._entries.pop(tenant_id, None)]
        while len(self._entries) >= self._max_entries:
            _, dropped = self._entries.popitem(last=False)
            log_event(logger, "chat.tenant_client_evicted")
            retired.append(dropped)
        for entry in retired:
            if entry is not None:
                await _retire(entry.resolution.client)
        self._entries[tenant_id] = _Entry(fingerprint, resolution)
        return resolution

    async def _build(self, tenant_id: str) -> tuple[TenantChatResolution, bool]:
        """Build this tenant's resolution, and whether it may be cached.

        Every failure is logged and answered by the process-wide client. A
        failure caused by the stored rows is cached until the rows, and with
        them the fingerprint, change. That covers rows that break this
        process's model policy (an unlisted provider, an unsupported field) and
        a client constructor that rejects the operator's provider or endpoint
        policy. A failure that may be transient, such as an unreachable catalog,
        a secret ref that would not resolve, or a shared model config that would
        not load, is not cached, so a later turn retries.
        """
        try:
            catalog = await self._sources.catalog.chat_catalog(tenant_id)
        except Exception as error:  # noqa: BLE001 - an unloaded catalog is retried next turn
            log_failure(
                logger,
                "chat.tenant_catalog_load_failed",
                error,
                tenant_id=tenant_id,
                retryable=True,
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
        except TenantChatConfigError as error:
            log_failure(
                logger,
                "chat.tenant_config_rejected",
                error,
                FailureKind.CLASSIFIED,
                tenant_id=tenant_id,
                retryable=False,
            )
            return _SHARED, True
        except Exception as error:  # noqa: BLE001 - chat falls back to the shared client
            log_failure(
                logger,
                "chat.tenant_config_resolve_failed",
                error,
                tenant_id=tenant_id,
                retryable=True,
            )
            return _SHARED, False
        try:
            client = self._builder(config)
        except Exception as error:  # noqa: BLE001 - chat falls back to the shared client
            log_failure(
                logger,
                "chat.tenant_client_build_failed",
                error,
                tenant_id=tenant_id,
                retryable=False,
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
    except Exception as error:
        log_failure(logger, "chat.tenant_client_dispose_failed", error, FailureKind.EXPECTED)
