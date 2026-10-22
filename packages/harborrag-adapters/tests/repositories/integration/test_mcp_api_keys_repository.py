"""mcp_api_keys repositories against a migrated control-plane database."""

from __future__ import annotations

import os
from collections.abc import AsyncGenerator, AsyncIterator
from contextlib import AsyncExitStack, asynccontextmanager
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import uuid4

import pytest
import pytest_asyncio
import sqlalchemy as sa
from sqlalchemy.engine import make_url
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from harborrag_adapters.repositories.database.control_plane import mcp_api_keys
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
from harborrag_adapters.repositories.database.control_plane.tenants import SqlTenantDirectory
from harborrag_adapters.repositories.database.ingestion_control.schema import INGESTION_TASKS
from harborrag_core.contracts.errors import HarborError
from harborrag_core.ports.api_keys import ApiKeyRecord, AuthStoreUnavailable
from harborrag_core.schemas.storage import StorageOperationContext
from harborrag_core.security.context import AccessContext

SessionFactory = async_sessionmaker[AsyncSession]
NOW = datetime(2026, 9, 29, 12, 0, tzinfo=UTC)
PLATFORM = StorageOperationContext.platform("op")
ENGINEERING = StorageOperationContext.for_access(
    AccessContext(principal_id="op", tenant_id="engineering")  # type: ignore[arg-type]
)
OTHER_TENANT = StorageOperationContext.for_access(
    AccessContext(principal_id="op", tenant_id="other")  # type: ignore[arg-type]
)
# Server URL (any database the user may CREATE DATABASE from) for the PostgreSQL
# runs; unset, those runs skip and only SQLite is exercised.
POSTGRES_URL_ENV = "HARBORRAG_TEST_POSTGRES_URL"


@asynccontextmanager
async def _scratch_postgres_dsn() -> AsyncGenerator[str]:
    """A freshly created database on the server named by POSTGRES_URL_ENV, dropped afterwards."""

    server = os.environ.get(POSTGRES_URL_ENV, "").strip()
    if not server:
        pytest.skip(f"set {POSTGRES_URL_ENV} to run against PostgreSQL")
    name = f"harborrag_test_{uuid4().hex[:12]}"
    admin = create_async_engine(server, isolation_level="AUTOCOMMIT")
    try:
        async with admin.connect() as conn:
            await conn.execute(sa.text(f'CREATE DATABASE "{name}"'))
        try:
            yield make_url(server).set(database=name).render_as_string(hide_password=False)
        finally:
            async with admin.connect() as conn:
                await conn.execute(sa.text(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)'))
    finally:
        await admin.dispose()


@pytest_asyncio.fixture(params=["sqlite", "postgresql"])
async def sessions(request: pytest.FixtureRequest, tmp_path: Path) -> AsyncIterator[SessionFactory]:
    async with AsyncExitStack() as stack:
        if request.param == "postgresql":
            dsn = await stack.enter_async_context(_scratch_postgres_dsn())
        else:
            dsn = f"sqlite+aiosqlite:///{tmp_path}/control.db"
        run_migrations(dsn)
        engine = create_control_plane_engine(dsn)
        stack.push_async_callback(engine.dispose)
        yield create_session_factory(engine)


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

    await manager.create(record, context=ENGINEERING)

    assert await reader.get_by_key_id(record.key_id, context=PLATFORM) == record
    assert await reader.get_by_key_id("f" * 24, context=PLATFORM) is None
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
    await manager.create(record, context=ENGINEERING)

    assert await manager.revoke(
        record.key_id, at=NOW, by="op", reason="laptop lost", context=PLATFORM
    )
    assert not await manager.revoke(
        record.key_id, at=NOW, by="op", reason="again", context=PLATFORM
    )
    assert not await manager.revoke("f" * 24, at=NOW, by="op", reason="unknown", context=PLATFORM)

    stored = await reader.get_by_key_id(record.key_id, context=PLATFORM)
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
    await manager.create(
        _record("a" * 24, created_at=NOW - timedelta(minutes=2)), context=ENGINEERING
    )
    await manager.create(
        _record("b" * 24, created_at=NOW - timedelta(minutes=1)), context=ENGINEERING
    )
    await manager.create(_record("c" * 24, revoked_at=NOW - timedelta(days=1)), context=ENGINEERING)
    await manager.create(_record("d" * 24, owner="user-other"), context=ENGINEERING)

    revoked = await manager.revoke_by_owner(
        "engineering", "user-huy", at=NOW, by="op", reason="left team", context=ENGINEERING
    )

    assert revoked == ["a" * 24, "b" * 24]
    listed = await manager.list_for_tenant("engineering", context=ENGINEERING)
    # Newest first; c and d share created_at, so key_id breaks the tie.
    assert [r.key_id for r in listed] == ["c" * 24, "d" * 24, "b" * 24, "a" * 24]
    assert {r.key_id for r in listed if r.revoked_at is None} == {"d" * 24}


@pytest.mark.asyncio
async def test_a_failed_audit_insert_rolls_back_the_key(
    sessions: SessionFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    real_audit = mcp_api_keys._audit

    def broken_audit(**fields: str) -> ActivityRow:
        row = real_audit(**fields)
        row.summary = None  # type: ignore[assignment]  # activity.summary is NOT NULL
        return row

    monkeypatch.setattr(mcp_api_keys, "_audit", broken_audit)
    record = _record("0123456789abcdef01234567")

    with pytest.raises(IntegrityError):
        await SqlApiKeyManager(sessions).create(record, context=ENGINEERING)

    assert await SqlApiKeyReader(sessions).get_by_key_id(record.key_id, context=PLATFORM) is None
    async with sessions() as session:
        assert (await session.scalars(sa.select(ActivityRow))).all() == []


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
        await insert(environment="qa")
    with pytest.raises(IntegrityError):
        await insert(key_id="short")
    # Right length, wrong alphabet: only lowercase hex is a valid key id or hash.
    with pytest.raises(IntegrityError):
        await insert(key_id="z" * 24)
    with pytest.raises(IntegrityError):
        await insert(key_id="0123456789ABCDEF01234567")
    with pytest.raises(IntegrityError):
        await insert(secret_hash="z" * 64)

    await insert(environment="staging")  # accepted alongside dev and prod


@pytest.mark.asyncio
async def test_reader_reports_an_unreachable_store_as_unavailable(tmp_path: Path) -> None:
    # A database that was never migrated has no key table: deny, distinguishably.
    engine = create_control_plane_engine(f"sqlite+aiosqlite:///{tmp_path}/empty.db")
    reader = SqlApiKeyReader(create_session_factory(engine))
    try:
        with pytest.raises(AuthStoreUnavailable):
            await reader.get_by_key_id("0123456789abcdef01234567", context=PLATFORM)
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
            await reader.get_by_key_id("0123456789abcdef01234567", context=PLATFORM)
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_manager_reports_an_unreachable_store_as_unavailable(tmp_path: Path) -> None:
    # Unmigrated SQLite (no key table) and a refused PostgreSQL connection: every
    # manager operation must surface the outage as AuthStoreUnavailable.
    for dsn in (
        f"sqlite+aiosqlite:///{tmp_path}/empty.db",
        "postgresql+asyncpg://u:p@127.0.0.1:1/db",
    ):
        engine = create_control_plane_engine(dsn)
        manager = SqlApiKeyManager(create_session_factory(engine))
        try:
            with pytest.raises(AuthStoreUnavailable):
                await manager.create(_record("0123456789abcdef01234567"), context=ENGINEERING)
            with pytest.raises(AuthStoreUnavailable):
                await manager.revoke(
                    "0123456789abcdef01234567", at=NOW, by="op", reason="r", context=PLATFORM
                )
            with pytest.raises(AuthStoreUnavailable):
                await manager.revoke_by_owner(
                    "engineering", "user-huy", at=NOW, by="op", reason="r", context=ENGINEERING
                )
            with pytest.raises(AuthStoreUnavailable):
                await manager.list_for_tenant("engineering", context=ENGINEERING)
        finally:
            await engine.dispose()


@pytest.mark.asyncio
async def test_manager_lets_a_rejected_row_raise_integrity_error(sessions: SessionFactory) -> None:
    # A duplicate key id is a bad write, not an outage: it must not be reported
    # as AuthStoreUnavailable.
    manager = SqlApiKeyManager(sessions)
    await manager.create(_record("0123456789abcdef01234567"), context=ENGINEERING)

    with pytest.raises(IntegrityError):
        await manager.create(_record("0123456789abcdef01234567", name="other"), context=ENGINEERING)
    with pytest.raises(IntegrityError):
        await manager.create(_record("f" * 24, environment="qa"), context=ENGINEERING)


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


@pytest.mark.asyncio
async def test_a_tenant_is_known_once_an_ingestion_task_exists(sessions: SessionFactory) -> None:
    directory = SqlTenantDirectory(sessions)
    assert not await directory.exists("engineering", context=PLATFORM)

    # Any status counts, even a failed first ingestion: the tenant was named.
    async with sessions.begin() as session:
        await session.execute(
            INGESTION_TASKS.insert().values(
                task_id="task-1",
                tenant_id="engineering",
                source_scope_id="scope-1",
                status="FAILED",
                submitted_at=NOW,
                request={},
                summary={},
            )
        )

    assert await directory.exists("engineering", context=PLATFORM)
    assert not await directory.exists(
        "Engineering", context=PLATFORM
    )  # ids are exact, not case-folded
    assert not await directory.exists("other", context=PLATFORM)


@pytest.mark.asyncio
async def test_repositories_refuse_a_context_of_the_wrong_scope(sessions: SessionFactory) -> None:
    manager = SqlApiKeyManager(sessions)
    reader = SqlApiKeyReader(sessions)
    key_id = "0123456789abcdef01234567"
    await manager.create(_record(key_id), context=ENGINEERING)

    # Lookups and revocation by key id are global: a tenant's context is not enough.
    with pytest.raises(HarborError):
        await reader.get_by_key_id(key_id, context=ENGINEERING)
    with pytest.raises(HarborError):
        await manager.get_by_key_id(key_id, context=ENGINEERING)
    with pytest.raises(HarborError):
        await manager.revoke(key_id, at=NOW, by="op", reason="r", context=ENGINEERING)
    with pytest.raises(HarborError):
        await SqlTenantDirectory(sessions).exists("engineering", context=ENGINEERING)
    # Tenant-bound operations need exactly that tenant, not another and not platform.
    for wrong in (OTHER_TENANT, PLATFORM):
        with pytest.raises(HarborError):
            await manager.create(_record("f" * 24), context=wrong)
        with pytest.raises(HarborError):
            await manager.list_for_tenant("engineering", context=wrong)
        with pytest.raises(HarborError):
            await manager.revoke_by_owner(
                "engineering", "user-huy", at=NOW, by="op", reason="r", context=wrong
            )

    stored = await reader.get_by_key_id(key_id, context=PLATFORM)
    assert stored is not None and stored.revoked_at is None  # nothing slipped through
