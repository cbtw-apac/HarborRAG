"""A reader's connection chips list only connections it may search (SEC-6)."""

from __future__ import annotations

from dataclasses import replace

import pytest

from harborrag_core.contracts.errors import HarborConflictError
from harborrag_core.domain.project import Project
from harborrag_runtime.application.control_plane.connection_models import ConnectionKey
from harborrag_runtime.config.settings import RuntimeSettings
from harborrag_runtime.testing.connection_fakes import (
    JIRA_WRITE,
    ConnectionWorld,
    connection_world,
)

pytestmark = [pytest.mark.unit, pytest.mark.asyncio]

_READER = "svc-demo"
_PROJECTS = [Project(id="demo", name="DEMO", collection="demo", tenant_id="acme")]


async def _three_connections(world: ConnectionWorld) -> None:
    """``demo-tickets`` is readable, ``demo-wiki`` is not, ``demo-files`` was never ingested."""
    for connection_id in ("demo-tickets", "demo-wiki", "demo-files"):
        await world.service.put(
            ConnectionKey("acme", "demo", connection_id),
            replace(JIRA_WRITE, name=f"DEMO {connection_id}"),
            actor="u_alex",
        )
    world.runs.scopes = {"demo/demo-tickets": ["scope-tickets"], "demo/demo-wiki": ["scope-wiki"]}
    world.grants.readable[_READER] = ("scope-tickets",)


async def test_searchable_under_source_acl_lists_only_readable_ingested_connections() -> None:
    world = connection_world(_PROJECTS)
    await _three_connections(world)

    chips = await world.service.searchable(
        tenant_id="acme", project_id="demo", principal_id=_READER
    )

    assert [chip["connection_ref"] for chip in chips] == ["demo/demo-tickets"]
    assert [(access.principal_id, access.corpus_mode) for access in world.grants.requests] == [
        (_READER, "source_acl")
    ]


async def test_searchable_under_source_acl_lists_nothing_for_a_reader_without_grants() -> None:
    world = connection_world(_PROJECTS)
    await _three_connections(world)

    chips = await world.service.searchable(
        tenant_id="acme", project_id="demo", principal_id="someone-else"
    )

    assert chips == []


async def test_searchable_under_tenant_shared_lists_every_connection_without_asking() -> None:
    shared = RuntimeSettings(corpus_access_mode="tenant_shared", corpus_shared_tenant_id="acme")
    world = connection_world(_PROJECTS, settings=shared)
    await _three_connections(world)

    chips = await world.service.searchable(
        tenant_id="acme", project_id="demo", principal_id=_READER
    )

    assert sorted(chip["connection_ref"] for chip in chips) == [
        "demo/demo-files",
        "demo/demo-tickets",
        "demo/demo-wiki",
    ]
    assert world.grants.requests == []


async def test_searchable_past_the_enumeration_budget_fails_closed() -> None:
    world = connection_world(_PROJECTS)
    await _three_connections(world)
    world.grants.over_budget = True

    with pytest.raises(HarborConflictError, match="enumeration exceeds"):
        await world.service.searchable(tenant_id="acme", project_id="demo", principal_id=_READER)


async def test_closing_the_service_closes_its_authorizer() -> None:
    world = connection_world(_PROJECTS)

    await world.service.aclose()

    assert world.grants.closed is True
