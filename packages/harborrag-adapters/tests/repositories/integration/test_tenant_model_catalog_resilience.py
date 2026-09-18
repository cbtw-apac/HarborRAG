"""One bad `providers` row must not take a tenant's chat offline.

Plus the fingerprint contract the caller's cross-replica cache rests on: the
same stored configuration must hash to the same stamp on every read, and any
insert, in-place edit, or delete of the tenant's provider or routing rows
must move it.
"""

from __future__ import annotations

import logging

import pytest
from tenant_catalog_support import (
    TENANT,
    ProviderSpec,
    chat_config,
    delete_provider,
    insert_providers,
    insert_rule,
    touch_provider,
)

from harborrag_adapters.repositories.database.control_plane.model_catalog import (
    SqlTenantModelCatalog,
)
from harborrag_adapters.repositories.database.control_plane.session import SessionFactory

pytestmark = pytest.mark.integration

_GOOD = ProviderSpec(id="p-good", name="good", config=chat_config("fast"))


@pytest.mark.asyncio
@pytest.mark.whitebox
@pytest.mark.parametrize(
    ("row_id", "config"),
    [
        ("p-unknown-key", chat_config("bad", region="eu-west-1")),
        ("p-no-model", {"provider": "openai", "logical_model": "bad"}),
        ("p-blank-logical", chat_config("   ")),
        ("p-bad-weight", chat_config("bad", weight=0)),
        ("p-bad-capabilities", chat_config("bad", capabilities={"streaming": "yes"})),
        ("p-bad-default", chat_config("bad", default="yes")),
        ("p-not-an-object", {}),
        ("p-unknown-provider", chat_config("bad", provider="skynet")),
    ],
)
async def test_one_unusable_row_is_skipped_and_the_tenant_survives(
    sessions: SessionFactory,
    caplog: pytest.LogCaptureFixture,
    row_id: str,
    config: dict[str, object],
) -> None:
    """Every rejection path must cost exactly one row, logged at ERROR by id."""
    await insert_providers(sessions, _GOOD, ProviderSpec(id=row_id, name="bad", config=config))

    with caplog.at_level(logging.ERROR):
        catalog = await SqlTenantModelCatalog(sessions).chat_catalog(TENANT)

    assert [m.logical_model for m in catalog.models] == ["fast"]
    assert catalog.is_empty() is False
    assert catalog.allows("bad") is False
    assert row_id in caplog.text


@pytest.mark.asyncio
@pytest.mark.whitebox
async def test_duplicate_deployment_names_drop_only_that_logical_model(
    sessions: SessionFactory, caplog: pytest.LogCaptureFixture
) -> None:
    """Two rows claiming one deployment name are ambiguous: drop the model, keep the rest."""
    await insert_providers(
        sessions,
        _GOOD,
        ProviderSpec(id="p-dup-a", name="clash", config=chat_config("dup", deployment="same")),
        ProviderSpec(id="p-dup-b", name="clash", config=chat_config("dup", deployment="same")),
    )

    with caplog.at_level(logging.ERROR):
        catalog = await SqlTenantModelCatalog(sessions).chat_catalog(TENANT)

    assert [m.logical_model for m in catalog.models] == ["fast"]
    assert "p-dup-a" in caplog.text and "p-dup-b" in caplog.text


@pytest.mark.asyncio
@pytest.mark.whitebox
async def test_fingerprint_is_stable_and_matches_the_catalog_it_labels(
    sessions: SessionFactory,
) -> None:
    """Repeated reads of unchanged rows must agree, so a cached catalog stays valid."""
    catalog_repo = SqlTenantModelCatalog(sessions)
    await insert_providers(sessions, _GOOD)

    first = await catalog_repo.fingerprint(TENANT)
    assert first == await catalog_repo.fingerprint(TENANT)
    assert (await catalog_repo.chat_catalog(TENANT)).fingerprint == first

    # A different tenant's stamp must not collide with this one.
    assert await catalog_repo.fingerprint("tenant-z") != first


@pytest.mark.asyncio
@pytest.mark.whitebox
async def test_fingerprint_moves_on_insert_update_and_delete(sessions: SessionFactory) -> None:
    """Every mutation of the tenant's provider rows must invalidate a cached catalog.

    Each step is compared against the state immediately before it, not against
    the whole history: an insert that a delete undoes *should* restore the
    earlier stamp, because it restores the earlier configuration.
    """
    catalog_repo = SqlTenantModelCatalog(sessions)
    await insert_providers(sessions, _GOOD, ProviderSpec(id="p-two", config=chat_config("deep")))
    start = await catalog_repo.fingerprint(TENANT)

    await insert_providers(sessions, ProviderSpec(id="p-three", config=chat_config("wide")))
    after_insert = await catalog_repo.fingerprint(TENANT)
    assert after_insert != start

    await touch_provider(sessions, "p-two", config=chat_config("deep", model="gpt-4o"))
    after_update = await catalog_repo.fingerprint(TENANT)
    assert after_update != after_insert

    await delete_provider(sessions, "p-good")
    after_delete = await catalog_repo.fingerprint(TENANT)
    assert after_delete != after_update
    assert after_delete not in {start, after_insert}


@pytest.mark.asyncio
@pytest.mark.whitebox
async def test_fingerprint_moves_when_only_a_routing_rule_changes(
    sessions: SessionFactory,
) -> None:
    """Routing rows carry the weights, so they must be inside the stamp too."""
    catalog_repo = SqlTenantModelCatalog(sessions)
    await insert_providers(sessions, _GOOD)
    before = await catalog_repo.fingerprint(TENANT)

    await insert_rule(sessions, "r1", "p-good", {"weight": 5})

    assert await catalog_repo.fingerprint(TENANT) != before
