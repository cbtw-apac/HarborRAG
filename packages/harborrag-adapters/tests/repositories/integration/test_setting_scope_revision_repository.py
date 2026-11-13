from __future__ import annotations

import pytest

from harborrag_adapters.repositories.database.control_plane.session import SessionFactory
from harborrag_adapters.repositories.database.control_plane.setting_scope_revisions import (
    SqlSettingScopeRevisionRepository,
)
from harborrag_core.contracts.errors import HarborPreconditionFailedError

pytestmark = pytest.mark.asyncio


async def test_get_on_an_unwritten_scope_is_zero(sessions: SessionFactory) -> None:
    repo = SqlSettingScopeRevisionRepository(sessions)
    assert await repo.get("acme", "tenant", "acme") == 0


async def test_bump_if_match_creates_the_row_at_revision_one(sessions: SessionFactory) -> None:
    repo = SqlSettingScopeRevisionRepository(sessions)
    revision = await repo.bump_if_match("acme", "tenant", "acme", expected_revision=0, source="api")
    assert revision == 1
    assert await repo.get("acme", "tenant", "acme") == 1


async def test_bump_if_match_advances_monotonically(sessions: SessionFactory) -> None:
    repo = SqlSettingScopeRevisionRepository(sessions)
    await repo.bump_if_match("acme", "tenant", "acme", expected_revision=0, source="api")
    second = await repo.bump_if_match("acme", "tenant", "acme", expected_revision=1, source="api")
    assert second == 2
    assert await repo.get("acme", "tenant", "acme") == 2


async def test_bump_if_match_rejects_a_stale_expectation(sessions: SessionFactory) -> None:
    repo = SqlSettingScopeRevisionRepository(sessions)
    await repo.bump_if_match("acme", "tenant", "acme", expected_revision=0, source="api")
    with pytest.raises(HarborPreconditionFailedError):
        await repo.bump_if_match("acme", "tenant", "acme", expected_revision=0, source="api")
    assert await repo.get("acme", "tenant", "acme") == 1


async def test_get_record_on_an_unwritten_scope_is_none(sessions: SessionFactory) -> None:
    repo = SqlSettingScopeRevisionRepository(sessions)
    assert await repo.get_record("acme", "tenant", "acme") is None


async def test_get_record_reports_the_last_bump_source_and_actor(
    sessions: SessionFactory,
) -> None:
    repo = SqlSettingScopeRevisionRepository(sessions)
    await repo.bump_if_match(
        "acme", "tenant", "acme", expected_revision=0, source="api", actor="u_huy"
    )
    assert await repo.get_record("acme", "tenant", "acme") == {
        "revision": 1,
        "source": "api",
        "actor": "u_huy",
    }


async def test_scopes_are_independent(sessions: SessionFactory) -> None:
    repo = SqlSettingScopeRevisionRepository(sessions)
    await repo.bump_if_match("acme", "tenant", "acme", expected_revision=0, source="api")
    await repo.bump_if_match("acme", "project", "cpm", expected_revision=0, source="api")
    assert await repo.get("acme", "tenant", "acme") == 1
    assert await repo.get("acme", "project", "cpm") == 1
    assert await repo.get("other", "tenant", "other") == 0
