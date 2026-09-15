"""Trusted graph registry and bounded client factory; no query-time graph proxies."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from hashlib import sha256
from typing import Protocol

from harborrag_core.storage import StorageOperationContext

from .client import FalkorDBClient
from .config import FalkorDBGraphConfig


@dataclass(frozen=True)
class TenantGraphRegistry:
    prefix: str

    def graph_for(self, context: StorageOperationContext) -> str:
        tenant = str(context.tenant_id)
        if not tenant or tenant != tenant.strip():
            raise ValueError("graph selection requires trusted nonempty tenant scope")
        return f"{self.prefix}_{sha256(tenant.encode('utf-8')).hexdigest()}"


class GraphClientFactory(Protocol):
    def create(self, graph_name: str, *, readonly: bool) -> FalkorDBClient: ...


@dataclass(frozen=True)
class ConfiguredGraphClientFactory:
    config: FalkorDBGraphConfig

    def create(self, graph_name: str, *, readonly: bool) -> FalkorDBClient:
        separate_reader = readonly and (
            self.config.read_username is not None or self.config.read_password is not None
        )
        username = self.config.read_username if separate_reader else self.config.username
        password = self.config.read_password if separate_reader else self.config.password
        return FalkorDBClient(
            host=self.config.host,
            port=self.config.port,
            username=username,
            password=password.get_secret_value() if password else None,
            graph_name=graph_name,
            ssl=self.config.ssl,
            max_connections=self.config.max_connections,
            connect_timeout_seconds=self.config.connect_timeout_seconds,
            operation_timeout_seconds=self.config.operation_timeout_seconds,
        )


class TenantGraphClientPool:
    """Cache bounded tenant readers/writers without evicting in-use connections.

    Capacity exhaustion fails closed until the owning runtime closes/replaces its
    pool. This deliberately avoids unsafe LRU eviction of an active graph client.
    Legacy single-graph behavior is retained only when tenant_isolation is false.
    """

    def __init__(
        self,
        config: FalkorDBGraphConfig,
        *,
        provisioner: Callable[[FalkorDBClient], Awaitable[None]],
        client: FalkorDBClient | None = None,
        factory: GraphClientFactory | None = None,
    ) -> None:
        if client is not None and config.tenant_isolation:
            raise ValueError("isolated graph routing requires a tenant-aware client factory")
        self._config = config
        self._factory = factory or ConfiguredGraphClientFactory(config)
        self._registry = TenantGraphRegistry(config.tenant_graph_prefix)
        self._provisioner = provisioner
        self._legacy = (
            client
            if client is not None
            else (
                None
                if config.tenant_isolation
                else self._factory.create(config.graph_name, readonly=False)
            )
        )
        self._clients: dict[tuple[str, bool], FalkorDBClient] = {}
        self._lock = asyncio.Lock()
        self._closed = False

    async def connect(self, *, provision: bool = True) -> None:
        if self._closed:
            raise RuntimeError("graph client pool is closed")
        if self._legacy is not None:
            await self._legacy.connect()
            if provision:
                await self._provisioner(self._legacy)

    async def provision(self, context: StorageOperationContext | None = None) -> None:
        if self._legacy is not None:
            await self._provisioner(self._legacy)
        elif context is not None:
            await self.database_for(context, write=True)

    async def database_for(
        self, context: StorageOperationContext, *, write: bool = False
    ) -> FalkorDBClient:
        if self._closed:
            raise RuntimeError("graph client pool is closed")
        if self._legacy is not None:
            return self._legacy
        graph_name = self._registry.graph_for(context)
        key = (graph_name, write)
        async with self._lock:
            if self._closed:
                raise RuntimeError("graph client pool is closed")
            if key in self._clients:
                return self._clients[key]
            names = {name for name, _ in self._clients}
            if graph_name not in names and len(names) >= self._config.max_cached_tenants:
                raise RuntimeError("tenant graph client cache capacity exhausted")
            client = self._factory.create(graph_name, readonly=not write)
            try:
                await client.connect()
                if write:
                    await self._provisioner(client)
            except BaseException:
                await client.close()
                raise
            self._clients[key] = client
            return client

    async def close(self) -> None:
        async with self._lock:
            if self._closed:
                return
            self._closed = True
            clients = [self._legacy] if self._legacy is not None else list(self._clients.values())
            self._clients.clear()
            results = await asyncio.gather(
                *(client.close() for client in clients), return_exceptions=True
            )
            for result in results:
                if isinstance(result, BaseException):
                    raise result
