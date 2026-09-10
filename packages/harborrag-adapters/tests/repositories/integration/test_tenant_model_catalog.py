"""SqlTenantModelCatalog reads a tenant's own chat models out of `providers`.

The point of the adapter is that a tenant can bring its own deployment and
key without a redeploy, *and* that a tenant which configured nothing keeps
working off the process-wide YAML catalog. Both halves are asserted here,
along with the skip-one-bad-row behaviour that keeps a typo in a single row
from taking a tenant's chat offline.
"""

from __future__ import annotations

import logging

import pytest
from tenant_catalog_support import (
    OTHER_TENANT,
    TENANT,
    ProviderSpec,
    chat_config,
    insert_providers,
    insert_rule,
)

from harborrag_adapters.repositories.database.control_plane.model_catalog import (
    SqlTenantModelCatalog,
)
from harborrag_adapters.repositories.database.control_plane.session import SessionFactory
from harborrag_core.ports.model_catalog import TenantModelCatalogPort

pytestmark = pytest.mark.integration


@pytest.mark.asyncio
@pytest.mark.whitebox
async def test_tenant_without_provider_rows_yields_an_empty_catalog(
    sessions: SessionFactory,
) -> None:
    """No rows must mean an empty catalog, never an error: the caller falls back to YAML.

    The annotation is the load-bearing part: it makes ``uv run mypy packages``
    prove the adapter still satisfies the port the model layer codes against.
    """
    port: TenantModelCatalogPort = SqlTenantModelCatalog(sessions)
    catalog = await port.chat_catalog(TENANT)
    assert catalog.fingerprint == await port.fingerprint(TENANT)

    assert catalog.is_empty() is True
    assert catalog.models == ()
    assert catalog.default_model is None
    assert catalog.tenant_id == TENANT
    assert catalog.fingerprint  # an empty tenant still gets a stable stamp
    assert catalog.allows("anything") is False


@pytest.mark.asyncio
@pytest.mark.whitebox
async def test_populated_tenant_groups_rows_into_logical_models(
    sessions: SessionFactory,
) -> None:
    """Two rows sharing a logical_model become two deployments behind one name."""
    await insert_providers(
        sessions,
        ProviderSpec(
            id="p1",
            name="fast-us",
            secret_ref="secret://db/aaa",
            config=chat_config(
                "fast",
                capabilities={"streaming": True, "tools": False},
                weight=3,
                default=True,
                api_base="https://us.example.com/v1",
                extra={"organization": "acme"},
            ),
        ),
        ProviderSpec(
            id="p2",
            name="fast-eu",
            config=chat_config("fast", provider="anthropic", model="claude-3-5-haiku"),
        ),
        ProviderSpec(id="p3", name="deep", config=chat_config("deep", model="gpt-4o")),
    )

    catalog = await SqlTenantModelCatalog(sessions).chat_catalog(TENANT)

    assert catalog.is_empty() is False
    assert [m.logical_model for m in catalog.models] == ["fast", "deep"]
    assert catalog.default_model == "fast"
    assert catalog.allows("fast") and catalog.allows("deep")
    assert catalog.allows("missing") is False

    fast, deep = catalog.models
    assert [d.name for d in fast.deployments] == ["fast-us", "fast-eu"]
    primary, secondary = fast.deployments
    assert (primary.provider, primary.model, primary.weight) == ("openai", "gpt-4o-mini", 3)
    assert primary.secret_ref == "secret://db/aaa"
    assert primary.api_base == "https://us.example.com/v1"
    assert dict(primary.capabilities) == {"streaming": True, "tools": False}
    assert dict(primary.extra) == {"organization": "acme"}
    assert (secondary.provider, secondary.weight, secondary.secret_ref) == ("anthropic", 1, None)
    assert [d.model for d in deep.deployments] == ["gpt-4o"]


@pytest.mark.asyncio
@pytest.mark.whitebox
async def test_routing_rule_overrides_weight_and_surfaces_strategy(
    sessions: SessionFactory,
) -> None:
    """rule_json is the routing knob: it wins over the row's configured weight."""
    await insert_providers(
        sessions, ProviderSpec(id="p1", name="fast-us", config=chat_config("fast", weight=2))
    )
    await insert_rule(sessions, "r1", "p1", {"weight": 7, "strategy": "weighted"})

    catalog = await SqlTenantModelCatalog(sessions).chat_catalog(TENANT)

    (deployment,) = catalog.models[0].deployments
    assert deployment.weight == 7
    assert dict(deployment.extra) == {"routing_strategy": "weighted"}


@pytest.mark.asyncio
@pytest.mark.whitebox
async def test_malformed_rule_is_dropped_without_dropping_the_provider_row(
    sessions: SessionFactory, caplog: pytest.LogCaptureFixture
) -> None:
    """A bad routing tweak must not cost the tenant the deployment it tweaks."""
    await insert_providers(
        sessions, ProviderSpec(id="p1", name="fast-us", config=chat_config("fast", weight=2))
    )
    await insert_rule(sessions, "r1", "p1", {"weight": -4})

    with caplog.at_level(logging.ERROR):
        catalog = await SqlTenantModelCatalog(sessions).chat_catalog(TENANT)

    (deployment,) = catalog.models[0].deployments
    assert deployment.weight == 2  # the row's own weight, not the rejected -4
    assert dict(deployment.extra) == {}
    assert "r1" in caplog.text and "p1" in caplog.text


@pytest.mark.asyncio
@pytest.mark.whitebox
async def test_other_families_and_other_tenants_are_invisible(sessions: SessionFactory) -> None:
    """Only this tenant's `chat`-family rows may reach a chat catalog."""
    await insert_providers(
        sessions,
        ProviderSpec(id="p1", name="embed", family="embedding", config=chat_config("embed")),
        ProviderSpec(id="p2", name="theirs", tenant_id=OTHER_TENANT, config=chat_config("theirs")),
        ProviderSpec(id="p3", name="mine", config=chat_config("mine")),
    )

    catalog = await SqlTenantModelCatalog(sessions).chat_catalog(TENANT)

    assert [m.logical_model for m in catalog.models] == ["mine"]
