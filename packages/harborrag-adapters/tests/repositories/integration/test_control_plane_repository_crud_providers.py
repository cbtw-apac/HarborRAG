"""SqlProviderRepository / SqlRoutingRuleRepository round-trips on SQLite.

Split out of test_control_plane_repository_crud.py to keep that file under
the repo's file-length gate.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path

import pytest
import pytest_asyncio

from harborrag_adapters.repositories.database.control_plane.engine import (
    create_control_plane_engine,
    create_session_factory,
)
from harborrag_adapters.repositories.database.control_plane.migrations import run_migrations
from harborrag_adapters.repositories.database.control_plane.session import SessionFactory
from harborrag_adapters.repositories.database.control_plane.workspace import (
    SqlProviderRepository,
    SqlRoutingRuleRepository,
)
from harborrag_core.contracts.errors import HarborConflictError
from harborrag_core.domain.provider import Provider
from harborrag_core.domain.routing_rule import RoutingRule

pytestmark = pytest.mark.integration


@pytest_asyncio.fixture
async def sessions(tmp_path: Path) -> AsyncIterator[SessionFactory]:
    """Migrated SQLite-file DB and a session factory, torn down per test."""

    dsn = f"sqlite+aiosqlite:///{tmp_path}/control.db"
    run_migrations(dsn)
    engine = create_control_plane_engine(dsn)
    yield create_session_factory(engine)
    await engine.dispose()


@pytest.mark.asyncio
@pytest.mark.whitebox
async def test_provider_repository_roundtrip_and_tenant_scoping(sessions: SessionFactory) -> None:
    """Provider create(save)/get/list/update(save)/delete against real SQL."""

    repo = SqlProviderRepository(sessions)
    provider = Provider(
        id="prov_1",
        tenant_id="tenant-a",
        name="OpenAI",
        family="chat",
        config={"model": "openai/gpt-4o-mini"},
        secret_ref="secret://db/1",
    )
    await repo.save(provider)
    await repo.save(Provider(id="prov_2", tenant_id="tenant-b", name="Local", family="embedding"))

    assert await repo.get("prov_1", tenant_ids=None) == provider
    assert await repo.get("prov_1", tenant_ids=frozenset({"tenant-b"})) is None
    assert [p.id for p in await repo.list(tenant_ids=frozenset({"tenant-a"}))] == ["prov_1"]
    assert len(await repo.list(tenant_ids=None)) == 2

    provider.name = "OpenAI Production"
    provider.secret_ref = "secret://db/2"
    await repo.save(provider)
    updated = await repo.get("prov_1", tenant_ids=None)
    assert updated is not None
    assert updated.name == "OpenAI Production"
    assert updated.secret_ref == "secret://db/2"

    provider.tenant_id = "tenant-b"
    with pytest.raises(HarborConflictError, match="tenant identity is immutable"):
        await repo.save(provider)

    await repo.delete("prov_2", tenant_ids=None)
    assert await repo.get("prov_2", tenant_ids=None) is None


@pytest.mark.asyncio
@pytest.mark.whitebox
async def test_routing_rule_repository_replace_is_atomic_and_not_tenant_scoped(
    sessions: SessionFactory,
) -> None:
    """replace swaps the whole table in one transaction; list has no tenant filter."""

    providers = SqlProviderRepository(sessions)
    await providers.save(Provider(id="prov_1", tenant_id="tenant-a", name="A", family="chat"))
    await providers.save(Provider(id="prov_2", tenant_id="tenant-b", name="B", family="chat"))

    repo = SqlRoutingRuleRepository(sessions)
    await repo.replace([RoutingRule(id="rule_1", family="chat", provider_id="prov_1", priority=0)])
    assert [r.id for r in await repo.list()] == ["rule_1"]

    replaced = await repo.replace(
        [
            RoutingRule(id="rule_2", family="chat", provider_id="prov_2", priority=1),
            RoutingRule(id="rule_3", family="chat", provider_id="prov_1", priority=0),
        ]
    )
    assert {r.id for r in replaced} == {"rule_2", "rule_3"}
    stored = await repo.list()
    assert [r.id for r in stored] == ["rule_2", "rule_3"]  # ordered by (family, id)
    assert stored[0].provider_id == "prov_2"
    assert stored[1].priority == 0
