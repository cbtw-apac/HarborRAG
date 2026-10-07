"""SqlMcpApiKeyRepository round-trips and revocation semantics on SQLite."""

from __future__ import annotations

import hashlib
import secrets
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
import sqlalchemy as sa
from sqlalchemy.exc import IntegrityError

from harborrag_adapters.repositories.backends.sqlalchemy import UTCDateTime
from harborrag_adapters.repositories.database.control_plane import mcp_api_keys
from harborrag_adapters.repositories.database.control_plane.engine import (
    create_control_plane_engine,
    create_session_factory,
)
from harborrag_adapters.repositories.database.control_plane.mcp_api_keys import (
    PostgresApiKeyManager,
    PostgresApiKeyReader,
    SqlMcpApiKeyRepository,
)
from harborrag_adapters.repositories.database.control_plane.schemas import ActivityRow
from harborrag_adapters.repositories.database.control_plane.schemas_mcp_auth import McpApiKeyRow
from harborrag_adapters.repositories.database.control_plane.session import SessionFactory
from harborrag_core.contracts.errors import AuthStoreUnavailable
from harborrag_core.ports.api_keys import ApiKeyRecord, AuditEvent, Revocation

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]


def mcp_key_row(key_id: str | None = None, *, tenant_id: str = "tenant-a") -> McpApiKeyRow:
    """A valid, unrevoked key row that satisfies every ck_mcp_key_* constraint."""
    now = datetime.now(UTC)
    return McpApiKeyRow(
        key_id=key_id or secrets.token_hex(12),  # 24 hex chars, matches ck_mcp_key_id
        tenant_id=tenant_id,
        owner="owner-1",
        name="ci-key",
        secret_hash=hashlib.sha256(b"not-a-real-secret").hexdigest(),
        environment="dev",
        created_at=now,  # explicit, so ck_mcp_key_expiry never compares mixed formats
        created_by="admin",
        expires_at=now + timedelta(days=30),
    )


async def test_insert_then_get_round_trips(sessions: SessionFactory) -> None:
    repo = SqlMcpApiKeyRepository(sessions)
    key_id = secrets.token_hex(12)
    await repo.insert(mcp_key_row(key_id))

    stored = await repo.get(key_id)

    assert stored is not None
    assert stored.tenant_id == "tenant-a"
    assert stored.revoked_at is None


async def test_get_unknown_key_returns_none(sessions: SessionFactory) -> None:
    assert await SqlMcpApiKeyRepository(sessions).get(secrets.token_hex(12)) is None


async def test_get_treats_sql_fragments_as_data(sessions: SessionFactory) -> None:
    repo = SqlMcpApiKeyRepository(sessions)
    await repo.insert(mcp_key_row())

    # Bound parameter: compared literally, never parsed as SQL.
    assert await repo.get("x' OR '1'='1") is None


async def test_revoke_is_idempotent_and_keeps_first_audit_fields(
    sessions: SessionFactory,
) -> None:
    repo = SqlMcpApiKeyRepository(sessions)
    key_id = secrets.token_hex(12)
    await repo.insert(mcp_key_row(key_id))

    assert await repo.revoke(key_id, tenant_id="tenant-a", revoked_by="alice", reason="leaked")
    first = await repo.get(key_id)

    assert not await repo.revoke(key_id, tenant_id="tenant-a", revoked_by="bob", reason="again")
    second = await repo.get(key_id)

    assert first is not None and second is not None
    assert second.revoked_at == first.revoked_at
    assert second.revoked_by == "alice"
    assert second.revocation_reason == "leaked"


async def test_revoke_with_other_tenant_changes_nothing(sessions: SessionFactory) -> None:
    repo = SqlMcpApiKeyRepository(sessions)
    key_id = secrets.token_hex(12)
    await repo.insert(mcp_key_row(key_id, tenant_id="tenant-a"))

    assert not await repo.revoke(key_id, tenant_id="tenant-b", revoked_by="amy")

    stored = await repo.get(key_id)
    assert stored is not None and stored.revoked_at is None


async def test_revoke_unknown_key_returns_false(sessions: SessionFactory) -> None:
    repo = SqlMcpApiKeyRepository(sessions)
    assert not await repo.revoke(secrets.token_hex(12), tenant_id="tenant-a", revoked_by="alice")


async def test_commit_failure_is_wrapped(sessions: SessionFactory) -> None:
    repo = SqlMcpApiKeyRepository(sessions)
    key_id = secrets.token_hex(12)
    await repo.insert(mcp_key_row(key_id))

    # Duplicate PK fails at commit, i.e. inside begin().__aexit__.
    with pytest.raises(AuthStoreUnavailable):
        await repo.insert(mcp_key_row(key_id))


async def _insert_without_created_at(sessions: SessionFactory, expires_at: datetime) -> str:
    """Insert leaving created_at to SQLite's CURRENT_TIMESTAMP (second precision).

    Plain SQL, because the column's Python default (utc_now) also fires for Core
    inserts and would fill created_at before the database default could.
    """
    key_id = secrets.token_hex(12)
    statement = sa.text(
        "INSERT INTO mcp_api_keys"
        " (key_id, tenant_id, owner, name, secret_hash, environment, created_by, expires_at)"
        " VALUES (:key_id, 'tenant-a', 'owner-1', 'ci-key', :secret_hash, 'dev', 'admin',"
        " :expires_at)"
    ).bindparams(sa.bindparam("expires_at", type_=UTCDateTime()))
    async with sessions.begin() as session:
        await session.execute(
            statement,
            {
                "key_id": key_id,
                "secret_hash": hashlib.sha256(b"not-a-real-secret").hexdigest(),
                "expires_at": expires_at,
            },
        )
    return key_id


async def test_expiry_check_rejects_expiry_not_after_defaulted_created_at(
    sessions: SessionFactory,
) -> None:
    # Floored to the second, expires_at is <= the defaulted created_at even if
    # the clock ticks before the insert. A raw text comparison let this pass.
    with pytest.raises(IntegrityError, match="ck_mcp_key_expiry"):
        await _insert_without_created_at(sessions, datetime.now(UTC).replace(microsecond=0))


async def test_expiry_check_accepts_future_expiry_with_defaulted_created_at(
    sessions: SessionFactory,
) -> None:
    key_id = await _insert_without_created_at(sessions, datetime.now(UTC) + timedelta(days=30))

    assert await SqlMcpApiKeyRepository(sessions).get(key_id) is not None


async def test_model_ddl_normalizes_expiry_check_on_sqlite() -> None:
    # The `sessions` fixture builds the schema from the migration; this builds it
    # from the ORM model, so both definitions of ck_mcp_key_expiry are covered.
    engine = create_control_plane_engine("sqlite+aiosqlite:///:memory:")
    try:
        async with engine.begin() as conn:
            await conn.run_sync(McpApiKeyRow.__table__.create)
        with pytest.raises(IntegrityError, match="ck_mcp_key_expiry"):
            await _insert_without_created_at(
                create_session_factory(engine), datetime.now(UTC).replace(microsecond=0)
            )
    finally:
        await engine.dispose()


def api_key_record(
    *, tenant_id: str = "tenant-a", owner: str = "owner-1", created_at: datetime | None = None
) -> ApiKeyRecord:
    now = created_at or datetime.now(UTC)
    return ApiKeyRecord(
        key_id=secrets.token_hex(12),
        tenant_id=tenant_id,
        owner=owner,
        name="ci-key",
        secret_hash=hashlib.sha256(b"not-a-real-secret").hexdigest(),
        environment="dev",
        created_at=now,
        created_by="admin",
        expires_at=now + timedelta(days=30),
        revoked_at=None,
        revoked_by=None,
        revocation_reason=None,
    )


def audit(record: ApiKeyRecord, action: str = "mcp_key.created") -> AuditEvent:
    return AuditEvent(
        actor="admin",
        tenant_id=record.tenant_id,
        action=action,
        entity_id=record.key_id,  # type: ignore[arg-type]
    )


async def activity_verbs(sessions: SessionFactory, key_id: str) -> list[str]:
    statement = sa.select(ActivityRow.verb).where(ActivityRow.entity_id == key_id)
    async with sessions() as session:
        return list((await session.scalars(statement)).all())


async def test_manager_create_writes_key_and_activity(sessions: SessionFactory) -> None:
    record = api_key_record()

    await PostgresApiKeyManager(sessions).create(record, audit(record))

    stored = await PostgresApiKeyReader(sessions).get_by_key_id(record.key_id)
    assert stored == record
    assert await activity_verbs(sessions, record.key_id) == ["mcp_key.created"]


async def test_reader_get_unknown_key_returns_none(sessions: SessionFactory) -> None:
    assert await PostgresApiKeyReader(sessions).get_by_key_id("f" * 24) is None


async def test_manager_create_duplicate_rolls_back_activity(sessions: SessionFactory) -> None:
    manager = PostgresApiKeyManager(sessions)
    record = api_key_record()
    await manager.create(record, audit(record))

    with pytest.raises(AuthStoreUnavailable):
        await manager.create(record, audit(record))

    assert await activity_verbs(sessions, record.key_id) == ["mcp_key.created"]


async def test_manager_revoke_twice_keeps_first_revocation(sessions: SessionFactory) -> None:
    manager = PostgresApiKeyManager(sessions)
    record = api_key_record()
    await manager.create(record, audit(record))
    first = Revocation(at=datetime.now(UTC), by="alice", reason="leaked")
    second = Revocation(at=first.at + timedelta(minutes=5), by="bob", reason="again")
    revoked = audit(record, "mcp_key.revoked")

    assert await manager.revoke(
        record.key_id, tenant_id="tenant-a", revocation=first, audit=revoked
    )
    assert not await manager.revoke(
        record.key_id, tenant_id="tenant-a", revocation=second, audit=revoked
    )

    stored = await PostgresApiKeyReader(sessions).get_by_key_id(record.key_id)
    assert stored is not None
    assert stored.revoked_at == first.at
    assert stored.revoked_by == "alice"
    assert stored.revocation_reason == "leaked"
    assert sorted(await activity_verbs(sessions, record.key_id)) == [
        "mcp_key.created",
        "mcp_key.revoked",
    ]


async def test_manager_revoke_other_tenant_changes_nothing(sessions: SessionFactory) -> None:
    manager = PostgresApiKeyManager(sessions)
    record = api_key_record(tenant_id="tenant-a")
    await manager.create(record, audit(record))

    assert not await manager.revoke(
        record.key_id,
        tenant_id="tenant-b",
        revocation=Revocation(at=datetime.now(UTC), by="mallory"),
        audit=audit(record, "mcp_key.revoked"),
    )
    stored = await PostgresApiKeyReader(sessions).get_by_key_id(record.key_id)
    assert stored is not None and stored.revoked_at is None


async def test_manager_revoke_by_owner_returns_only_live_keys_in_scope(
    sessions: SessionFactory,
) -> None:
    manager = PostgresApiKeyManager(sessions)
    live_1, live_2 = api_key_record(), api_key_record()
    already_revoked = api_key_record()
    other_owner = api_key_record(owner="owner-2")
    other_tenant = api_key_record(tenant_id="tenant-b")
    for record in (live_1, live_2, already_revoked, other_owner, other_tenant):
        await manager.create(record, audit(record))
    earlier = Revocation(at=datetime.now(UTC) - timedelta(days=1), by="admin", reason="old")
    assert await manager.revoke(
        already_revoked.key_id,
        tenant_id="tenant-a",
        revocation=earlier,
        audit=audit(already_revoked, "mcp_key.revoked"),
    )
    revocation = Revocation(at=datetime.now(UTC), by="alice", reason="offboarded")

    revoked = await manager.revoke_by_owner("tenant-a", "owner-1", revocation=revocation)

    assert sorted(revoked) == sorted([live_1.key_id, live_2.key_id])
    assert await manager.revoke_by_owner("tenant-a", "owner-1", revocation=revocation) == []
    assert "mcp_key.revoked" in await activity_verbs(sessions, live_1.key_id)
    assert "mcp_key.revoked" not in await activity_verbs(sessions, other_owner.key_id)

    stored = await PostgresApiKeyReader(sessions).get_by_key_id(already_revoked.key_id)
    assert stored is not None and stored.revocation_reason == "old"


async def test_manager_list_for_tenant_is_newest_first_and_scoped(sessions: SessionFactory) -> None:
    manager = PostgresApiKeyManager(sessions)
    now = datetime.now(UTC)
    older = api_key_record(created_at=now - timedelta(hours=1))
    newer = api_key_record(created_at=now)
    foreign = api_key_record(tenant_id="tenant-b")
    for record in (older, newer, foreign):
        await manager.create(record, audit(record))

    listed = await manager.list_for_tenant("tenant-a")

    assert [r.key_id for r in listed] == [newer.key_id, older.key_id]


async def test_manager_create_rolls_back_key_when_audit_insert_fails(
    sessions: SessionFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    original = mcp_api_keys._activity_row

    def _activity_row_without_summary(event: AuditEvent, at: datetime) -> ActivityRow:
        row = original(event, at)
        row.summary = None  # type: ignore[assignment]  # NOT NULL -> fails at commit
        return row

    monkeypatch.setattr(mcp_api_keys, "_activity_row", _activity_row_without_summary)
    record = api_key_record()

    with pytest.raises(AuthStoreUnavailable):
        await PostgresApiKeyManager(sessions).create(record, audit(record))

    assert await PostgresApiKeyReader(sessions).get_by_key_id(record.key_id) is None


async def test_reader_raises_store_unavailable_when_database_is_unreachable(
    tmp_path: Path,
) -> None:
    engine = create_control_plane_engine(f"sqlite+aiosqlite:///{tmp_path}/missing/dir/control.db")
    try:
        reader = PostgresApiKeyReader(create_session_factory(engine))
        with pytest.raises(AuthStoreUnavailable):
            await reader.get_by_key_id("f" * 24)
    finally:
        await engine.dispose()
