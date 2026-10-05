"""SqlMcpApiKeyRepository round-trips and revocation semantics on SQLite."""

from __future__ import annotations

import hashlib
import secrets
from datetime import UTC, datetime, timedelta

import pytest
import sqlalchemy as sa
from sqlalchemy.exc import IntegrityError

from harborrag_adapters.repositories.backends.sqlalchemy import UTCDateTime
from harborrag_adapters.repositories.database.control_plane.engine import (
    create_control_plane_engine,
    create_session_factory,
)
from harborrag_adapters.repositories.database.control_plane.mcp_api_keys import (
    SqlMcpApiKeyRepository,
)
from harborrag_adapters.repositories.database.control_plane.schemas_mcp_auth import McpApiKeyRow
from harborrag_adapters.repositories.database.control_plane.session import SessionFactory
from harborrag_core.contracts.errors import AuthStoreUnavailable

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
