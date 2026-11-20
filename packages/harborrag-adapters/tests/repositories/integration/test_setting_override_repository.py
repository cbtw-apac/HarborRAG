from __future__ import annotations

import pytest

from harborrag_adapters.repositories.database.control_plane.session import SessionFactory
from harborrag_adapters.repositories.database.control_plane.setting_overrides import (
    SqlSettingOverrideRepository,
)
from harborrag_core.contracts.errors import HarborPreconditionFailedError
from harborrag_core.domain.setting_override import SettingOverride

pytestmark = pytest.mark.asyncio


def _override(**kwargs: object) -> SettingOverride:
    defaults: dict[str, object] = {
        "tenant_id": "acme",
        "scope_level": "tenant",
        "scope_id": "acme",
        "key": "chunking.max_tokens",
        "value": 512,
        "created_by": "tester",
        "updated_by": "tester",
    }
    defaults.update(kwargs)
    return SettingOverride(**defaults)  # type: ignore[arg-type]


async def test_put_creates_then_get_at_returns_it(sessions: SessionFactory) -> None:
    repo = SqlSettingOverrideRepository(sessions)
    created = await repo.put(_override(), expected_version=None)
    assert created.version == 1

    values = await repo.get_at("acme", "tenant", "acme")
    assert values == {"chunking.max_tokens": 512}


async def test_put_update_requires_matching_version(sessions: SessionFactory) -> None:
    repo = SqlSettingOverrideRepository(sessions)
    await repo.put(_override(), expected_version=None)

    with pytest.raises(HarborPreconditionFailedError):
        await repo.put(_override(value=256), expected_version=99)

    updated = await repo.put(_override(value=256), expected_version=1)
    assert updated.version == 2
    assert (await repo.get_at("acme", "tenant", "acme"))["chunking.max_tokens"] == 256


async def test_put_with_expected_version_on_missing_row_fails(sessions: SessionFactory) -> None:
    repo = SqlSettingOverrideRepository(sessions)
    with pytest.raises(HarborPreconditionFailedError):
        await repo.put(_override(), expected_version=1)


async def test_delete_is_idempotent(sessions: SessionFactory) -> None:
    repo = SqlSettingOverrideRepository(sessions)
    await repo.put(_override(), expected_version=None)
    await repo.delete("acme", "tenant", "acme", "chunking.max_tokens")
    await repo.delete("acme", "tenant", "acme", "chunking.max_tokens")  # no error
    assert await repo.get_at("acme", "tenant", "acme") == {}


async def test_list_for_tenant_spans_every_scope(sessions: SessionFactory) -> None:
    repo = SqlSettingOverrideRepository(sessions)
    await repo.put(_override(), expected_version=None)
    await repo.put(
        _override(scope_level="project", scope_id="cpm", key="chunking.max_tokens", value=384),
        expected_version=None,
    )
    await repo.put(
        _override(
            scope_level="connection",
            scope_id="cpm/cpm-tickets",
            key="chunking.max_tokens",
            value=256,
        ),
        expected_version=None,
    )

    overrides = await repo.list_for_tenant("acme")
    assert {o.scope_level for o in overrides} == {"tenant", "project", "connection"}


async def test_count_all_reflects_every_tenant(sessions: SessionFactory) -> None:
    repo = SqlSettingOverrideRepository(sessions)
    assert await repo.count_all() == 0
    await repo.put(_override(), expected_version=None)
    await repo.put(_override(tenant_id="other", scope_id="other"), expected_version=None)
    assert await repo.count_all() == 2
