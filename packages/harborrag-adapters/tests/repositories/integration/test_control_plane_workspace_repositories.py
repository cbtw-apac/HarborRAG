"""Workspace control-plane repositories round-trip against migrated SQLite."""

from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path

import pytest
import pytest_asyncio

from harborrag_adapters.repositories.database.control_plane.engine import (
    create_control_plane_engine,
    create_session_factory,
)
from harborrag_adapters.repositories.database.control_plane.jobs import (
    SqlActivityRepository,
)
from harborrag_adapters.repositories.database.control_plane.migrations import run_migrations
from harborrag_adapters.repositories.database.control_plane.session import SessionFactory
from harborrag_adapters.repositories.database.control_plane.workspace import (
    SqlMemberRepository,
    SqlProviderRepository,
    SqlSettingsRepository,
)
from harborrag_core.domain.activity import ActivityEntry
from harborrag_core.domain.member import Member
from harborrag_core.domain.provider import Provider
from harborrag_core.domain.settings import WorkspaceSettings

pytestmark = pytest.mark.integration


@pytest_asyncio.fixture
async def sessions(tmp_path: Path) -> AsyncIterator[SessionFactory]:
    dsn = f"sqlite+aiosqlite:///{tmp_path}/control.db"
    run_migrations(dsn)
    engine = create_control_plane_engine(dsn)
    yield create_session_factory(engine)
    await engine.dispose()


@pytest.mark.asyncio
@pytest.mark.whitebox
async def test_activity_settings_provider_member_roundtrips(
    sessions: SessionFactory,
) -> None:
    """Audit, settings, provider, and member repositories preserve their contracts."""

    activity = SqlActivityRepository(sessions)
    await activity.append(
        ActivityEntry(
            id="a1",
            tenant_id="tenant-a",
            actor="nguyen.vu@cbtw.tech",
            verb="created",
            entity_type="project",
            entity_id="p1",
            summary="Created project Docs",
        )
    )
    entries = await activity.list()
    assert [entry.id for entry in entries] == ["a1"]
    assert entries[0].tenant_id == "tenant-a"

    settings = SqlSettingsRepository(sessions)
    assert (await settings.get()).data == {}
    await settings.put(WorkspaceSettings(tenant_id="tenant-a", data={"theme": "dark"}))
    stored_settings = await settings.get()
    assert stored_settings.tenant_id == "tenant-a"
    assert stored_settings.data == {"theme": "dark"}
    await settings.put(WorkspaceSettings(tenant_id="tenant-a", data={"theme": "light"}))
    assert (await settings.get()).data == {"theme": "light"}

    providers = SqlProviderRepository(sessions)
    provider = Provider(
        id="pr1",
        tenant_id="tenant-a",
        name="OpenAI",
        family="chat",
        secret_ref="secret://key",
    )
    await providers.save(provider)
    assert await providers.get("pr1") == provider
    provider.name = "OpenAI EU"
    await providers.save(provider)
    listed = await providers.list()
    assert listed[0].name == "OpenAI EU"
    await providers.delete("pr1")
    assert await providers.get("pr1") is None

    members = SqlMemberRepository(sessions)
    member = Member(id="m1", tenant_id="tenant-a", subject="user@cbtw.tech", role="editor")
    await members.save(member)
    assert await members.get_by_subject("user@cbtw.tech") == member
    assert await members.get_by_subject("ghost@cbtw.tech") is None
    assert [stored.id for stored in await members.list()] == ["m1"]
    await members.delete("m1")
    assert await members.list() == []
