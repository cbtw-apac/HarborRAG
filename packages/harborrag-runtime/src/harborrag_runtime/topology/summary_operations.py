"""Explicit summary administration and graph-independent worker composition."""

from collections.abc import AsyncIterator
from contextlib import AsyncExitStack, asynccontextmanager

from harborrag_adapters.repositories.object_store import (
    ARTIFACT_BUCKET,
    ImmutableArtifactReader,
    ImmutableArtifactWriter,
)
from harborrag_runtime.composition.resources import build_object_store
from harborrag_runtime.config.graph_build import GraphBuildConfig
from harborrag_runtime.config.settings import RuntimeSettings
from harborrag_runtime.config.temporal import TemporalRuntimeConfig
from harborrag_runtime.temporal.connection import connect_temporal_client

from .composition import connect_topology_authority
from .summary_factory import SummaryRuntimeFactory
from .summary_worker import watch_summaries


@asynccontextmanager
async def connect_summaries(
    settings: RuntimeSettings, tenant_id: str
) -> AsyncIterator[SummaryRuntimeFactory]:
    config = GraphBuildConfig.from_settings(settings)
    settings = config.effective_settings(settings)
    async with AsyncExitStack() as stack:
        control = await stack.enter_async_context(connect_topology_authority(settings))
        store = build_object_store(settings)
        stack.push_async_callback(store.close)
        await store.connect()
        await store.ensure_buckets((ARTIFACT_BUCKET,))
        factory = SummaryRuntimeFactory(
            settings, control, ImmutableArtifactReader(store), ImmutableArtifactWriter(store)
        )
        await factory.initialize(tenant_id)
        yield factory


async def status(settings: RuntimeSettings, tenant_id: str) -> dict[str, object]:
    async with connect_topology_authority(settings, migrate=False) as control:
        return {"scopes": await control.summaries.status(tenant_id), "limit": 100}


async def backfill(
    settings: RuntimeSettings, tenant_id: str, scope: str | None
) -> dict[str, object]:
    async with connect_summaries(settings, tenant_id) as factory:
        await factory.synchronize(tenant_id)
        return {"enqueued": await factory.control.summaries.backfill(tenant_id, scope)}


async def run_once(settings: RuntimeSettings, tenant_id: str) -> dict[str, object]:
    async with connect_summaries(settings, tenant_id) as factory:
        await factory.synchronize(tenant_id)
        await factory.control.summaries.reconcile(tenant_id)
        return {"state": await factory.service().run_once(tenant_id)}


async def worker(settings: RuntimeSettings, tenant_id: str) -> None:
    async with connect_summaries(settings, tenant_id) as factory:
        client = await connect_temporal_client(
            TemporalRuntimeConfig.from_settings(factory.settings)
        )
        await watch_summaries(client, factory, tenant_id)


async def cleanup(
    settings: RuntimeSettings, tenant_id: str, *, retention_days: int, apply: bool
) -> dict[str, object]:
    async with connect_topology_authority(settings, migrate=False) as control:
        return await control.summaries.cleanup(
            tenant_id, retention_days=retention_days, apply=apply
        )
