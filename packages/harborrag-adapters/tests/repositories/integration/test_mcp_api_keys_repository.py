"""mcp_api_keys repositories against a migrated control-plane database."""

from __future__ import annotations

from collections.abc import AsyncIterator
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
import pytest_asyncio
import sqlalchemy as sa
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from harborrag_adapters.repositories.database.control_plane.engine import (
    create_control_plane_engine,
    create_session_factory,
)
from harborrag_adapters.repositories.database.control_plane.mcp_api_keys import (
    SqlApiKeyManager,
    SqlApiKeyReader,
)
from harborrag_adapters.repositories.database.control_plane.migrations import run_migrations
from harborrag_adapters.repositories.database.control_plane.schemas import (
    ActivityRow,
    McpApiKeyRow,
)
from harborrag_core.ports.api_keys import ApiKeyRecord, AuthStoreUnavailable

SessionFactory = async_sessionmaker[AsyncSession]
NOW = datetime(2026, 9, 29, 12, 0, tzinfo=UTC)


@pytest_asyncio.fixture
async def sessions(tmp_path: Path) -> AsyncIterator[SessionFactory]:
    dsn = f"sqlite+aiosqlite:///{tmp_path}/control.db"
    run_migrations(dsn)
    engine = create_control_plane_engine(dsn)
    yield create_session_factory(engine)
    await engine.dispose()


def _record(key_id: str, **overrides: object) -> ApiKeyRecord:
    record = ApiKeyRecord(
        key_id=key_id,
        tenant_id="engineering",
        owner="user-huy",
        name="laptop",
        secret_hash="a" * 64,
        environment="dev",
        created_at=NOW,
        created_by="op@host",
        expires_at=NOW + timedelta(days=1),
    )
    return replace(record, **overrides)  # type: ignore[arg-type]


@pytest.mark.asyncio
async def test_create_then_read_back_and_audit_row(sessions: SessionFactory) -> None:
    manager = SqlApiKeyManager(sessions)
    reader = SqlApiKeyReader(sessions)
    record = _record("0123456789abcdef01234567")

    await manager.create(record)

    assert await reader.get_by_key_id(record.key_id) == record
    assert await reader.get_by_key_id("f" * 24) is None
    async with sessions() as session:
        activity = (await session.scalars(sa.select(ActivityRow))).all()
    assert [(a.verb, a.entity_type, a.entity_id, a.tenant_id) for a in activity] == [
        ("mcp_key.created", "mcp_api_key", record.key_id, "engineering")
    ]
    assert "a" * 64 not in activity[0].summary


@pytest.mark.asyncio
async def test_revoke_is_recorded_once_and_keeps_the_first_reason(sessions: SessionFactory) -> None:
    manager = SqlApiKeyManager(sessions)
    reader = SqlApiKeyReader(sessions)
    record = _record("0123456789abcdef01234567")
    await manager.create(record)

    assert await manager.revoke(record.key_id, at=NOW, by="op", reason="laptop lost")
    assert not await manager.revoke(record.key_id, at=NOW, by="op", reason="again")
    assert not await manager.revoke("f" * 24, at=NOW, by="op", reason="unknown")

    stored = await reader.get_by_key_id(record.key_id)
    assert stored is not None
    assert (stored.revoked_at, stored.revoked_by, stored.revocation_reason) == (
        NOW,
        "op",
        "laptop lost",
    )
    async with sessions() as session:
        verbs = (await session.scalars(sa.select(ActivityRow.verb))).all()
    assert sorted(verbs) == ["mcp_key.created", "mcp_key.revoked"]


@pytest.mark.asyncio
async def test_revoke_by_owner_returns_only_the_keys_it_changed(sessions: SessionFactory) -> None:
    manager = SqlApiKeyManager(sessions)
    await manager.create(_record("a" * 24, created_at=NOW - timedelta(minutes=2)))
    await manager.create(_record("b" * 24, created_at=NOW - timedelta(minutes=1)))
    await manager.create(_record("c" * 24, revoked_at=NOW - timedelta(days=1)))
    await manager.create(_record("d" * 24, owner="user-other"))

    revoked = await manager.revoke_by_owner(
        "engineering", "user-huy", at=NOW, by="op", reason="left team"
    )

    assert revoked == ["a" * 24, "b" * 24]
    listed = await manager.list_for_tenant("engineering")
    # Newest first; c and d share created_at, so key_id breaks the tie.
    assert [r.key_id for r in listed] == ["c" * 24, "d" * 24, "b" * 24, "a" * 24]
    assert {r.key_id for r in listed if r.revoked_at is None} == {"d" * 24}


@pytest.mark.asyncio
async def test_constraints_reject_bad_rows(sessions: SessionFactory) -> None:
    async def insert(**columns: object) -> None:
        base: dict[str, object] = {
            "key_id": "0123456789abcdef01234567",
            "tenant_id": "engineering",
            "owner": "user-huy",
            "name": "k",
            "secret_hash": "a" * 64,
            "environment": "dev",
            "created_at": NOW,
            "created_by": "op",
            "expires_at": NOW + timedelta(hours=1),
        }
        async with sessions.begin() as session:
            session.add(McpApiKeyRow(**{**base, **columns}))

    with pytest.raises(IntegrityError):
        await insert(secret_hash="not-a-hash")
    with pytest.raises(IntegrityError):
        await insert(expires_at=NOW - timedelta(days=1))
    with pytest.raises(IntegrityError):
        await insert(environment="staging")
    with pytest.raises(IntegrityError):
        await insert(key_id="short")


@pytest.mark.asyncio
async def test_reader_reports_an_unreachable_store_as_unavailable(tmp_path: Path) -> None:
    # A database that was never migrated has no key table: deny, distinguishably.
    engine = create_control_plane_engine(f"sqlite+aiosqlite:///{tmp_path}/empty.db")
    reader = SqlApiKeyReader(create_session_factory(engine))
    try:
        with pytest.raises(AuthStoreUnavailable):
            await reader.get_by_key_id("0123456789abcdef01234567")
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_a_refused_connection_is_reported_as_unavailable() -> None:
    # Port 1 on loopback refuses immediately: an OSError from asyncpg, not a
    # SQLAlchemyError, and it must still become AuthStoreUnavailable.
    engine = create_control_plane_engine("postgresql+asyncpg://u:p@127.0.0.1:1/db")
    reader = SqlApiKeyReader(create_session_factory(engine))
    try:
        with pytest.raises(AuthStoreUnavailable):
            await reader.get_by_key_id("0123456789abcdef01234567")
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_a_wildcard_tenant_row_is_rejected_by_the_database(sessions: SessionFactory) -> None:
    async with sessions.begin() as session:
        session.add(
            McpApiKeyRow(
                key_id="0123456789abcdef01234567",
                tenant_id="*",
                owner="user-huy",
                name="k",
                secret_hash="a" * 64,
                environment="dev",
                created_at=NOW,
                created_by="op",
                expires_at=NOW + timedelta(hours=1),
            )
        )
        with pytest.raises(IntegrityError):
            await session.flush()
