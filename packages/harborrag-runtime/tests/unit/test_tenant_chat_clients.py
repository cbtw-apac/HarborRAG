"""A tenant's own chat client is built, cached by fingerprint, and disposed.

The cache is what makes this usable: without it every turn would re-read the
tenant's rows and rebuild a connection pool. The fingerprint is what keeps it
honest: a rotated key or a new model must take effect without a restart and
without anybody telling the other replicas.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import replace

import pytest
from tenant_chat_fixtures import (
    SHARED_MODEL_CONFIG,
    FakeCatalog,
    FakeSecrets,
    FakeTenantChatClient,
    tenant_catalog,
    tenant_deployment,
)

from harborrag_adapters.models.chat import HarborChatClientConfig
from harborrag_core.ports.model_catalog import TenantModelDeployment
from harborrag_runtime.chat.tenant_clients import TenantChatClients
from harborrag_runtime.chat.tenant_config import TenantChatConfigError, build_tenant_chat_config
from harborrag_runtime.chat.tenant_models import TenantModelSources
from harborrag_runtime.config.settings import RuntimeSettings


def _clients(
    catalog: FakeCatalog,
    secrets: FakeSecrets,
    *,
    settings: RuntimeSettings | None = None,
    built: list[FakeTenantChatClient] | None = None,
) -> tuple[TenantChatClients, list[FakeTenantChatClient]]:
    made = built if built is not None else []

    def build(config: HarborChatClientConfig) -> FakeTenantChatClient:
        client = FakeTenantChatClient(config.default_model)
        made.append(client)
        return client

    clients = TenantChatClients(
        (settings or RuntimeSettings()).model_copy(
            update={"model_config_path": SHARED_MODEL_CONFIG}
        ),
        TenantModelSources(catalog=catalog, secrets=secrets),
        builder=build,  # type: ignore[arg-type]
    )
    return clients, made


@pytest.mark.asyncio
async def test_an_unconfigured_tenant_uses_the_process_wide_client() -> None:
    catalog = FakeCatalog()
    clients, built = _clients(catalog, FakeSecrets())

    resolution = await clients.resolve("DEFAULT")

    assert resolution.client is None, "the caller must fall through to the shared client"
    assert resolution.catalog is None, "the shared catalog bounds this tenant's model names"
    assert built == []


@pytest.mark.asyncio
async def test_a_configured_tenant_gets_its_own_client_with_resolved_secrets() -> None:
    secrets = FakeSecrets({"ref-1": "sk-tenant"})
    catalog = FakeCatalog({"acme": tenant_catalog("acme")}, fingerprints={"acme": "fp-1"})
    clients, built = _clients(catalog, secrets)

    resolution = await clients.resolve("acme")

    assert resolution.client is built[0]
    assert resolution.catalog is not None
    assert resolution.catalog.allows("tenant-primary")
    # The key came from the encrypted store, scoped to the owning tenant --
    # never from the process environment.
    assert secrets.resolved == [("ref-1", "acme")]


@pytest.mark.asyncio
async def test_an_unchanged_fingerprint_reuses_the_cached_client() -> None:
    catalog = FakeCatalog({"acme": tenant_catalog("acme")}, fingerprints={"acme": "fp-1"})
    clients, built = _clients(catalog, FakeSecrets())

    first = await clients.resolve("acme")
    second = await clients.resolve("acme")

    assert first.client is second.client
    assert len(built) == 1
    # The stamp is re-read every request; the rows behind it are not.
    assert catalog.fingerprint_calls == 2
    assert catalog.catalog_calls == ["acme"]


@pytest.mark.asyncio
async def test_a_changed_fingerprint_rebuilds_and_disposes_the_old_client() -> None:
    catalog = FakeCatalog({"acme": tenant_catalog("acme")}, fingerprints={"acme": "fp-1"})
    clients, built = _clients(catalog, FakeSecrets())

    first = await clients.resolve("acme")
    catalog.fingerprints["acme"] = "fp-2"
    catalog.catalogs["acme"] = tenant_catalog("acme", fingerprint="fp-2")
    second = await clients.resolve("acme")

    assert second.client is not first.client
    assert len(built) == 2
    assert built[0].closes == 1, "the replaced client's connection pool must be released"
    assert built[1].closes == 0


@pytest.mark.asyncio
async def test_eviction_disposes_the_least_recently_used_client() -> None:
    catalogs = {name: tenant_catalog(name) for name in ("one", "two", "three")}
    catalog = FakeCatalog(catalogs, fingerprints=dict.fromkeys(catalogs, "fp-1"))
    clients, built = _clients(
        catalog,
        FakeSecrets(),
        settings=RuntimeSettings(chat_tenant_client_cache_size=2),
    )

    await clients.resolve("one")
    await clients.resolve("two")
    await clients.resolve("three")

    assert len(built) == 3
    assert built[0].closes == 1, "the least recently used tenant client must be disposed"
    assert [client.closes for client in built[1:]] == [0, 0]
    # Evicted, so the next turn rebuilds rather than answering on a closed pool.
    await clients.resolve("one")
    assert len(built) == 4


@pytest.mark.asyncio
async def test_a_broken_tenant_configuration_falls_back_with_an_error_log(
    caplog: pytest.LogCaptureFixture,
) -> None:
    # A stored deployment field this process does not let a tenant set. The
    # operator's own provider and endpoint policy is enforced too, by the
    # client constructor, and degrades through this same path.
    catalog = FakeCatalog(
        {
            "acme": tenant_catalog(
                "acme",
                deployment=replace(
                    tenant_deployment("acme"), extra={"allow_ambient_credentials": True}
                ),
            )
        },
        fingerprints={"acme": "fp-1"},
    )
    clients, built = _clients(catalog, FakeSecrets())

    with caplog.at_level(logging.ERROR, logger="harborrag.runtime.chat.tenant"):
        resolution = await clients.resolve("acme")

    assert resolution.client is None, "a broken tenant degrades to the shared client"
    assert resolution.catalog is None, "the shared catalog now bounds its model names"
    assert built == []
    assert "acme" in caplog.text


@pytest.mark.asyncio
async def test_a_client_the_builder_rejects_falls_back_with_an_error_log(
    caplog: pytest.LogCaptureFixture,
) -> None:
    # The operator's own policy -- `chat.security.allowed_providers`, the
    # endpoint allowlist, provider capability checks -- is enforced when the
    # client is constructed, not when the configuration validates. So the
    # builder raising is the shape those rejections actually take, and it has
    # to degrade like any other broken tenant configuration.
    catalog = FakeCatalog({"acme": tenant_catalog("acme")}, fingerprints={"acme": "fp-1"})

    def refuse(_config: HarborChatClientConfig) -> FakeTenantChatClient:
        raise RuntimeError("provider 'anthropic' is not allowed")

    clients = TenantChatClients(
        RuntimeSettings(model_config_path=SHARED_MODEL_CONFIG),
        TenantModelSources(catalog=catalog, secrets=FakeSecrets()),
        builder=refuse,  # type: ignore[arg-type]
    )

    with caplog.at_level(logging.ERROR, logger="harborrag.runtime.chat.tenant"):
        resolution = await clients.resolve("acme")

    assert resolution.client is None, "a rejected client degrades to the shared one"
    assert resolution.catalog is None, "the shared catalog now bounds its model names"
    assert "acme" in caplog.text


@pytest.mark.asyncio
async def test_the_config_layer_itself_accepts_any_registered_provider() -> None:
    # Pinning where the boundary is: the projection validates shape, so a
    # provider the operator disallows still builds a config here and is
    # refused one step later, by the client constructor.
    catalog = tenant_catalog(
        "acme", deployment=replace(tenant_deployment("acme"), provider="anthropic")
    )

    config = await build_tenant_chat_config(
        catalog,
        secrets=FakeSecrets(),  # type: ignore[arg-type]
        shared_config_path=SHARED_MODEL_CONFIG,
    )

    assert config.default_model == "tenant-primary"


@pytest.mark.asyncio
async def test_a_routing_strategy_hint_is_dropped_rather_than_rejected() -> None:
    # The catalog surfaces a routing rule's strategy in ``extra``, but routing
    # strategy is one client-wide policy the operator owns, so it must be
    # ignored -- not turned into a broken tenant.
    catalog = FakeCatalog(
        {
            "acme": tenant_catalog(
                "acme",
                deployment=replace(
                    tenant_deployment("acme"),
                    extra={"routing_strategy": "weighted", "organization": "acme-org"},
                ),
            )
        },
        fingerprints={"acme": "fp-1"},
    )
    clients, built = _clients(catalog, FakeSecrets())

    assert (await clients.resolve("acme")).client is built[0]


@pytest.mark.asyncio
async def test_a_stored_environment_reference_is_refused() -> None:
    # ``from_dict`` expands ``${...}`` from the process environment, so a
    # stored one would read a variable the tenant was never meant to see.
    catalog = FakeCatalog(
        {
            "acme": tenant_catalog(
                "acme", deployment=replace(tenant_deployment("acme"), model="${HARBOR_CHAT_MODEL}")
            )
        },
        fingerprints={"acme": "fp-1"},
    )
    clients, built = _clients(catalog, FakeSecrets())

    assert (await clients.resolve("acme")).client is None
    assert built == []


@pytest.mark.asyncio
async def test_an_unresolvable_secret_ref_falls_back_with_an_error_log(
    caplog: pytest.LogCaptureFixture,
) -> None:
    catalog = FakeCatalog(
        {
            "acme": tenant_catalog(
                "acme", deployment=replace(tenant_deployment("acme"), secret_ref="ref-missing")
            )
        },
        fingerprints={"acme": "fp-1"},
    )
    clients, _built = _clients(catalog, FakeSecrets())

    with caplog.at_level(logging.ERROR, logger="harborrag.runtime.chat.tenant"):
        resolution = await clients.resolve("acme")

    assert resolution.client is None
    assert "acme" in caplog.text


@pytest.mark.asyncio
async def test_a_failing_catalog_port_never_fails_the_turn(
    caplog: pytest.LogCaptureFixture,
) -> None:
    catalog = FakeCatalog({"acme": tenant_catalog("acme")}, fingerprints={"acme": "fp-1"})
    catalog.fingerprint_failure = RuntimeError("control database is unreachable")
    clients, _built = _clients(catalog, FakeSecrets())

    with caplog.at_level(logging.ERROR, logger="harborrag.runtime.chat.tenant"):
        assert (await clients.resolve("acme")).client is None

    catalog.fingerprint_failure = None
    catalog.catalog_failure = RuntimeError("control database is unreachable")
    with caplog.at_level(logging.ERROR, logger="harborrag.runtime.chat.tenant"):
        assert (await clients.resolve("acme")).client is None


@pytest.mark.asyncio
async def test_aclose_disposes_every_cached_tenant_client() -> None:
    catalogs = {name: tenant_catalog(name) for name in ("one", "two")}
    catalog = FakeCatalog(catalogs, fingerprints=dict.fromkeys(catalogs, "fp-1"))
    clients, built = _clients(catalog, FakeSecrets())

    await clients.resolve("one")
    await clients.resolve("two")
    await clients.aclose()

    assert [client.closes for client in built] == [1, 1]


@pytest.mark.asyncio
async def test_a_blank_tenant_never_reaches_the_catalog_port() -> None:
    catalog = FakeCatalog()
    clients, _built = _clients(catalog, FakeSecrets())

    assert (await clients.resolve(None)).client is None
    assert (await clients.resolve("")).client is None
    assert catalog.fingerprint_calls == 0


_PROBE = "${HARBOR_TENANT_PROBE}"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "deployment",
    [
        replace(tenant_deployment("acme"), extra={"api_version": _PROBE}),
        replace(tenant_deployment("acme"), extra={"organization": _PROBE}),
        replace(tenant_deployment("acme"), extra={"metadata": {"tags": ["ok", _PROBE]}}),
        replace(tenant_deployment("acme"), capabilities={"streaming": _PROBE}),  # type: ignore[dict-item,unused-ignore]
        replace(tenant_deployment("acme"), api_base=f"https://{_PROBE}.example"),
        replace(tenant_deployment("acme"), name=_PROBE),
    ],
    ids=["deployment-field", "extension", "nested-extension", "capability", "api-base", "name"],
)
async def test_no_tenant_value_is_expanded_from_the_process_environment(
    deployment: TenantModelDeployment, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Set, so an expanding path would *succeed* with the operator's value --
    # the leak -- rather than fail for an unrelated reason.
    monkeypatch.setenv("HARBOR_TENANT_PROBE", "leaked")

    with pytest.raises(TenantChatConfigError) as caught:
        await build_tenant_chat_config(
            tenant_catalog("acme", deployment=deployment),
            secrets=FakeSecrets(),
            shared_config_path=SHARED_MODEL_CONFIG,
        )

    # Neither the variable's name nor its value may reach the message: the
    # refusal must look the same whether or not the variable exists.
    assert "HARBOR_TENANT_PROBE" not in str(caught.value)
    assert "leaked" not in str(caught.value)


@pytest.mark.asyncio
async def test_a_model_name_is_not_expanded_from_the_process_environment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # default_model is a value in the section, so it was expanded: with the
    # variable set to a real model name the tenant could confirm its value.
    monkeypatch.setenv("HARBOR_TENANT_PROBE", "tenant-primary")
    catalog = replace(tenant_catalog("acme"), default_model=_PROBE)

    with pytest.raises(TenantChatConfigError) as caught:
        await build_tenant_chat_config(
            catalog,
            secrets=FakeSecrets(),
            shared_config_path=SHARED_MODEL_CONFIG,
        )

    assert "HARBOR_TENANT_PROBE" not in str(caught.value)


@pytest.mark.asyncio
async def test_a_transient_catalog_failure_is_retried_on_the_next_request() -> None:
    catalog = FakeCatalog({"acme": tenant_catalog("acme")}, fingerprints={"acme": "fp-1"})
    catalog.catalog_failure = RuntimeError("control database is unreachable")
    clients, built = _clients(catalog, FakeSecrets())

    assert (await clients.resolve("acme")).client is None
    catalog.catalog_failure = None
    recovered = await clients.resolve("acme")

    # Same fingerprint, so a cached fallback would have pinned the tenant to the
    # shared client until its rows changed.
    assert recovered.client is built[0]  # type: ignore[comparison-overlap]


@pytest.mark.asyncio
async def test_a_transient_secret_failure_is_retried_on_the_next_request() -> None:
    # Non-empty on purpose: FakeSecrets treats an empty map as "use the default ref-1".
    secrets = FakeSecrets({"unrelated": "x"})
    catalog = FakeCatalog({"acme": tenant_catalog("acme")}, fingerprints={"acme": "fp-1"})
    clients, built = _clients(catalog, secrets)

    assert (await clients.resolve("acme")).client is None
    secrets.values["ref-1"] = "sk-tenant"

    assert (await clients.resolve("acme")).client is built[0]  # type: ignore[comparison-overlap]


@pytest.mark.asyncio
async def test_a_leased_client_survives_a_rebuild_until_released() -> None:
    catalog = FakeCatalog({"acme": tenant_catalog("acme")}, fingerprints={"acme": "fp-1"})
    clients, built = _clients(catalog, FakeSecrets())

    async with clients.lease("acme") as held:
        catalog.fingerprints["acme"] = "fp-2"
        catalog.catalogs["acme"] = tenant_catalog("acme", fingerprint="fp-2")
        replacement = await clients.resolve("acme")
        assert replacement.client is not held.client
        assert built[0].closes == 0, "a client in use must not be closed under its turn"

    assert built[0].closes == 1, "the last lease to go closes the retired client"
    assert built[1].closes == 0


@pytest.mark.asyncio
async def test_a_leased_client_survives_eviction_until_released() -> None:
    catalogs = {name: tenant_catalog(name) for name in ("one", "two")}
    catalog = FakeCatalog(catalogs, fingerprints=dict.fromkeys(catalogs, "fp-1"))
    clients, built = _clients(
        catalog, FakeSecrets(), settings=RuntimeSettings(chat_tenant_client_cache_size=1)
    )

    async with clients.lease("one"):
        await clients.resolve("two")
        assert built[0].closes == 0

    assert built[0].closes == 1


@pytest.mark.asyncio
async def test_aclose_defers_closing_a_leased_client_and_closes_it_once() -> None:
    catalog = FakeCatalog({"acme": tenant_catalog("acme")}, fingerprints={"acme": "fp-1"})
    clients, built = _clients(catalog, FakeSecrets())

    async with clients.lease("acme"):
        await clients.aclose()
        assert built[0].closes == 0

    assert built[0].closes == 1
    await clients.aclose()
    assert built[0].closes == 1


@pytest.mark.asyncio
async def test_a_retired_client_closes_only_when_the_last_of_several_leases_ends() -> None:
    catalog = FakeCatalog({"acme": tenant_catalog("acme")}, fingerprints={"acme": "fp-1"})
    clients, built = _clients(catalog, FakeSecrets())

    async with clients.lease("acme"):
        async with clients.lease("acme"):
            await clients.aclose()
        assert built[0].closes == 0, "one turn is still using the retired client"

    assert built[0].closes == 1


class _GatedClosingClient(FakeTenantChatClient):
    """A client whose ``aclose`` waits until the test opens the gate."""

    def __init__(self, label: str) -> None:
        super().__init__(label)
        self.closing = asyncio.Event()
        self.gate = asyncio.Event()

    async def aclose(self) -> None:
        self.closing.set()
        await self.gate.wait()
        await super().aclose()


@pytest.mark.asyncio
async def test_a_turn_cancelled_while_a_replaced_client_closes_releases_its_lease() -> None:
    """The turn that rebuilt the client disposes the old one before it is handed the new.

    Cancelling it there used to skip the release: the new client's lease stayed counted,
    so it was never closed once retired, and the old client's close was abandoned.
    """

    catalog = FakeCatalog({"acme": tenant_catalog("acme")}, fingerprints={"acme": "fp-1"})
    built: list[_GatedClosingClient] = []

    def build(config: HarborChatClientConfig) -> _GatedClosingClient:
        client = _GatedClosingClient(config.default_model)
        if built:
            client.gate.set()  # only the first client is slow to close
        built.append(client)
        return client

    clients = TenantChatClients(
        RuntimeSettings().model_copy(update={"model_config_path": SHARED_MODEL_CONFIG}),
        TenantModelSources(catalog=catalog, secrets=FakeSecrets()),
        builder=build,  # type: ignore[arg-type]
    )
    await clients.resolve("acme")
    catalog.fingerprints["acme"] = "fp-2"
    catalog.catalogs["acme"] = tenant_catalog("acme", fingerprint="fp-2")

    async def turn() -> None:
        async with clients.lease("acme"):
            pytest.fail("the turn was cancelled before it got its client")

    task = asyncio.create_task(turn())
    await built[0].closing.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    built[0].gate.set()
    catalog.fingerprints["acme"] = "fp-3"
    catalog.catalogs["acme"] = tenant_catalog("acme", fingerprint="fp-3")
    await clients.resolve("acme")
    await clients.aclose()

    assert built[1].closes == 1, "the cancelled turn's lease must not keep the client open"
    assert built[0].closes == 1, "the replaced client must still be closed"
