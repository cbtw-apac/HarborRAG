"""The tenant client cache under concurrency, rotation, and transient failure.

Three properties a single global lock and an eager close used to break: a
retired client must outlive the turns still using it, one tenant's slow build
must not hold up another tenant, and a port that failed once must be retried
rather than pinning the tenant to the process-wide client.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from typing import Any

import pytest
from tenant_chat_fixtures import (
    SHARED_MODEL_CONFIG,
    FakeCatalog,
    FakeSecrets,
    FakeTenantChatClient,
    empty_catalog,
    tenant_catalog,
)

from harborrag_adapters.models.chat import HarborChatClientConfig
from harborrag_core.ports.model_catalog import TenantChatCatalog
from harborrag_runtime.chat.tenant_clients import TenantChatClients
from harborrag_runtime.chat.tenant_models import TenantModelSources
from harborrag_runtime.config.settings import RuntimeSettings


class GatedClient(FakeTenantChatClient):
    """A client whose calls block until the test opens ``gate``."""

    def __init__(self, label: str = "tenant") -> None:
        super().__init__(label)
        self.gate = asyncio.Event()
        self.started = asyncio.Event()

    async def achat(self, messages=None, *, request=None, model=None, **kwargs):  # type: ignore[no-untyped-def]
        self.started.set()
        await self.gate.wait()
        return await super().achat(messages, request=request, model=model, **kwargs)

    async def astream(self, *args: Any, **kwargs: Any) -> AsyncIterator[str]:
        del args, kwargs
        yield "first"
        self.started.set()
        await self.gate.wait()
        yield "second"


class SlowCatalog(FakeCatalog):
    """A catalog whose ``chat_catalog`` blocks for one tenant until released."""

    def __init__(self, *args: Any, slow_tenant: str, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.slow_tenant = slow_tenant
        self.release = asyncio.Event()
        self.entered = asyncio.Event()

    async def chat_catalog(self, tenant_id: str) -> TenantChatCatalog:
        if tenant_id == self.slow_tenant:
            self.entered.set()
            await self.release.wait()
        return await super().chat_catalog(tenant_id)


class Clock:
    """A monotonic clock the test advances by hand."""

    def __init__(self) -> None:
        self.now = 1_000.0

    def __call__(self) -> float:
        return self.now


def _clients(
    catalog: FakeCatalog,
    secrets: FakeSecrets | None = None,
    *,
    size: int = 32,
    clock: Clock | None = None,
) -> tuple[TenantChatClients, list[GatedClient]]:
    made: list[GatedClient] = []

    def build(config: HarborChatClientConfig) -> GatedClient:
        client = GatedClient(config.default_model)
        made.append(client)
        return client

    settings = RuntimeSettings(chat_tenant_client_cache_size=size).model_copy(
        update={"model_config_path": SHARED_MODEL_CONFIG}
    )
    clients = TenantChatClients(
        settings,
        TenantModelSources(catalog=catalog, secrets=secrets or FakeSecrets()),  # type: ignore[arg-type]
        builder=build,  # type: ignore[arg-type]
        clock=clock or Clock(),
    )
    return clients, made


@pytest.mark.asyncio
async def test_an_evicted_client_stays_open_until_its_in_flight_turn_ends() -> None:
    names = ("t0", "t1", "t2")
    catalog = FakeCatalog(
        {name: tenant_catalog(name) for name in names}, fingerprints=dict.fromkeys(names, "fp-1")
    )
    clients, built = _clients(catalog, size=2)

    in_use = (await clients.resolve("t0")).client
    assert in_use is not None
    turn = asyncio.create_task(in_use.achat())
    await built[0].started.wait()

    await clients.resolve("t1")
    await clients.resolve("t2")  # evicts t0 while its turn is still running

    assert built[0].closes == 0, "an in-flight turn's client must not be closed under it"
    built[0].gate.set()
    await turn
    assert built[0].closes == 1, "the retired client closes once its last turn ends"


@pytest.mark.asyncio
async def test_a_rotated_client_stays_open_until_its_stream_finishes() -> None:
    catalog = FakeCatalog({"acme": tenant_catalog("acme")}, fingerprints={"acme": "fp-1"})
    clients, built = _clients(catalog)

    first = (await clients.resolve("acme")).client
    assert first is not None
    stream = first.astream()
    assert await anext(stream) == "first"

    catalog.fingerprints["acme"] = "fp-2"  # an operator rotated the key
    second = await clients.resolve("acme")

    assert getattr(second.client, "wrapped", None) is built[1]
    assert built[0].closes == 0, "the stream still running must keep its pool"
    built[0].gate.set()
    assert [chunk async for chunk in stream] == ["second"]
    assert built[0].closes == 1


@pytest.mark.asyncio
async def test_a_slow_build_for_one_tenant_does_not_block_another() -> None:
    catalog = SlowCatalog(
        {"slow": tenant_catalog("slow"), "fast": tenant_catalog("fast")},
        fingerprints={"slow": "fp-1", "fast": "fp-1"},
        slow_tenant="slow",
    )
    clients, _built = _clients(catalog)

    slow = asyncio.create_task(clients.resolve("slow"))
    await catalog.entered.wait()

    fast = await asyncio.wait_for(clients.resolve("fast"), timeout=2)

    assert fast.client is not None
    assert not slow.done()
    catalog.release.set()
    assert (await slow).client is not None


@pytest.mark.asyncio
async def test_concurrent_first_requests_for_one_tenant_build_once() -> None:
    catalog = SlowCatalog(
        {"acme": tenant_catalog("acme")}, fingerprints={"acme": "fp-1"}, slow_tenant="acme"
    )
    clients, built = _clients(catalog)

    turns = [asyncio.create_task(clients.resolve("acme")) for _ in range(3)]
    await catalog.entered.wait()
    catalog.release.set()
    resolutions = await asyncio.gather(*turns)

    assert len(built) == 1
    assert catalog.catalog_calls == ["acme"]
    assert len({id(resolution.client) for resolution in resolutions}) == 1


@pytest.mark.asyncio
async def test_a_transient_catalog_failure_is_retried_after_a_short_pause() -> None:
    catalog = FakeCatalog({"acme": tenant_catalog("acme")}, fingerprints={"acme": "fp-1"})
    clock = Clock()
    clients, built = _clients(catalog, clock=clock)
    catalog.catalog_failure = TimeoutError("control database timed out")

    degraded = await clients.resolve("acme")
    catalog.catalog_failure = None  # the store recovered
    assert (await clients.resolve("acme")).client is None, "retried only after the pause"
    clock.now += 60
    recovered = await clients.resolve("acme")

    assert degraded.client is None
    assert getattr(recovered.client, "wrapped", None) is built[0]
    assert recovered.catalog is not None
    assert catalog.catalog_calls == ["acme", "acme"]


@pytest.mark.asyncio
async def test_a_transient_secret_failure_is_retried_after_a_short_pause() -> None:
    secrets = FakeSecrets()
    secrets.values = {}  # ref-1 does not resolve yet, as during an outage
    catalog = FakeCatalog({"acme": tenant_catalog("acme")}, fingerprints={"acme": "fp-1"})
    clock = Clock()
    clients, built = _clients(catalog, secrets, clock=clock)

    degraded = await clients.resolve("acme")
    secrets.values["ref-1"] = "sk-tenant"
    clock.now += 60
    recovered = await clients.resolve("acme")

    assert degraded.client is None
    assert getattr(recovered.client, "wrapped", None) is built[0]


@pytest.mark.asyncio
async def test_a_tenant_with_no_catalog_is_still_cached() -> None:
    catalog = FakeCatalog({"acme": empty_catalog("acme", "fp-1")}, fingerprints={"acme": "fp-1"})
    clients, built = _clients(catalog)

    await clients.resolve("acme")
    await clients.resolve("acme")

    assert built == []
    assert catalog.catalog_calls == ["acme"], "an unconfigured tenant must not re-read every turn"


@pytest.mark.asyncio
async def test_a_build_finishing_after_aclose_is_disposed_not_cached() -> None:
    catalog = SlowCatalog(
        {"acme": tenant_catalog("acme")}, fingerprints={"acme": "fp-1"}, slow_tenant="acme"
    )
    clients, built = _clients(catalog)

    pending = asyncio.create_task(clients.resolve("acme"))
    await catalog.entered.wait()
    await asyncio.wait_for(clients.aclose(), timeout=2)
    catalog.release.set()

    assert (await pending).client is None
    assert built[0].closes == 1


@pytest.mark.asyncio
async def test_turns_queued_behind_a_failing_port_share_its_one_failure() -> None:
    catalog = SlowCatalog(
        {"acme": tenant_catalog("acme")}, fingerprints={"acme": "fp-1"}, slow_tenant="acme"
    )
    catalog.catalog_failure = TimeoutError("control database timed out")
    clients, built = _clients(catalog)

    turns = [asyncio.create_task(clients.resolve("acme")) for _ in range(3)]
    await catalog.entered.wait()
    catalog.release.set()
    resolutions = await asyncio.gather(*turns)

    assert catalog.catalog_calls == ["acme"], "one timeout, not one per queued turn"
    assert all(resolution.client is None for resolution in resolutions)
    assert built == []


@pytest.mark.asyncio
async def test_a_failure_never_evicts_the_tenants_working_client() -> None:
    catalog = FakeCatalog({"acme": tenant_catalog("acme")}, fingerprints={"acme": "fp-1"})
    clients, built = _clients(catalog)
    await clients.resolve("acme")

    catalog.fingerprint_failure = RuntimeError("control database is unreachable")
    assert (await clients.resolve("acme")).client is None
    catalog.fingerprint_failure = None

    assert getattr((await clients.resolve("acme")).client, "wrapped", None) is built[0]
    assert built[0].closes == 0


@pytest.mark.asyncio
async def test_a_transient_failure_on_a_full_cache_evicts_nobody() -> None:
    names = ("t0", "t1", "t2")
    catalog = FakeCatalog(
        {name: tenant_catalog(name) for name in names}, fingerprints=dict.fromkeys(names, "fp-1")
    )
    clients, built = _clients(catalog, size=2)
    await clients.resolve("t0")
    await clients.resolve("t1")

    catalog.catalog_failure = TimeoutError("control database timed out")
    assert (await clients.resolve("t2")).client is None
    catalog.catalog_failure = None

    assert [client.closes for client in built] == [0, 0], "nothing was inserted to make room for"
    await clients.resolve("t0")
    await clients.resolve("t1")
    assert len(built) == 2, "both working clients are still cached"


@pytest.mark.asyncio
async def test_a_transient_failure_after_a_rotation_keeps_serving_the_old_client() -> None:
    catalog = FakeCatalog({"acme": tenant_catalog("acme")}, fingerprints={"acme": "fp-1"})
    clock = Clock()
    clients, built = _clients(catalog, clock=clock)
    await clients.resolve("acme")

    catalog.fingerprints["acme"] = "fp-2"
    catalog.catalog_failure = TimeoutError("control database timed out")
    during = [await clients.resolve("acme") for _ in range(2)]

    assert all(getattr(r.client, "wrapped", None) is built[0] for r in during)
    assert built[0].closes == 0
    assert catalog.catalog_calls == ["acme", "acme"], "the retry waits out the pause"

    catalog.catalog_failure = None
    clock.now += 60
    rebuilt = await clients.resolve("acme")
    assert getattr(rebuilt.client, "wrapped", None) is built[1]
    assert built[0].closes == 1
