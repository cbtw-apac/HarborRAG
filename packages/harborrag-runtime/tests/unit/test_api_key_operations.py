"""Issue, verify, list and revoke a reader key end to end on a migrated control DB."""

from __future__ import annotations

from datetime import timedelta
from pathlib import Path

import pytest
from alembic import command
from sqlalchemy.exc import IntegrityError

from harborrag_adapters.repositories.database.control_plane.migrations import (
    _build_config,
    head_revision,
    run_migrations,
)
from harborrag_runtime.composition.mcp_auth import build_api_key_verification
from harborrag_runtime.config.settings import RuntimeSettings
from harborrag_runtime.security import api_key_operations


@pytest.fixture
def settings(tmp_path: Path) -> RuntimeSettings:
    dsn = f"sqlite+aiosqlite:///{tmp_path}/control.db"
    run_migrations(dsn)
    return RuntimeSettings(control_db_url=dsn)


@pytest.mark.asyncio
async def test_key_lifecycle(settings: RuntimeSettings) -> None:
    created = await api_key_operations.create_key(
        settings,
        operator="op@host",
        tenant_id="engineering",
        owner="user-huy",
        name="laptop",
        lifetime=timedelta(days=1),
    )

    verifier = build_api_key_verification(settings)
    verified = await verifier.verify_key(created.raw_key)
    assert verified is not None and verified.tenant_id == "engineering"

    listed = await api_key_operations.list_keys(settings, "engineering")
    assert [(k["key_id"], k["state"], k["created_by"]) for k in listed] == [
        (created.key_id, "active", "op@host")
    ]
    assert all("secret" not in field for field in listed[0])

    assert (
        await api_key_operations.revoke_key(
            settings, operator="op@host", key_id=created.key_id, reason="lost"
        )
        == "revoked"
    )
    assert await verifier.verify_key(created.raw_key) is None  # next request is refused
    assert (await api_key_operations.list_keys(settings, "engineering"))[0]["state"] == "revoked"


@pytest.mark.asyncio
async def test_revoke_owner_covers_every_active_key(settings: RuntimeSettings) -> None:
    ids = []
    for name in ("laptop", "desktop"):
        created = await api_key_operations.create_key(
            settings,
            operator="op",
            tenant_id="engineering",
            owner="user-huy",
            name=name,
            lifetime=timedelta(hours=2),
        )
        ids.append(created.key_id)

    revoked = await api_key_operations.revoke_owner(
        settings, operator="op", tenant_id="engineering", owner="user-huy", reason="left"
    )

    assert sorted(revoked) == sorted(ids)
    assert not await api_key_operations.revoke_owner(
        settings, operator="op", tenant_id="engineering", owner="user-huy", reason="again"
    )


@pytest.mark.asyncio
async def test_ensure_key_schema_reports_or_applies_missing_migrations(tmp_path: Path) -> None:
    empty = RuntimeSettings(control_db_url=f"sqlite+aiosqlite:///{tmp_path}/fresh.db")

    with pytest.raises(api_key_operations.ApiKeySchemaMissing, match="--migrate"):
        await api_key_operations.ensure_key_schema(empty, migrate=False)
    with pytest.raises(api_key_operations.ApiKeySchemaMissing, match="--migrate"):
        await api_key_operations.ensure_key_schema(empty, migrate=False, require_head=False)

    assert await api_key_operations.ensure_key_schema(empty, migrate=True) is True
    assert await api_key_operations.ensure_key_schema(empty, migrate=False) is False
    created = await api_key_operations.create_key(
        empty, operator="op", tenant_id="t", owner="user-x", name="k", lifetime=timedelta(hours=1)
    )
    assert created.key_id


@pytest.mark.asyncio
async def test_migrate_refuses_a_key_table_without_a_migration_stamp(tmp_path: Path) -> None:
    # A key table created outside alembic: upgrading from "no revision" would try to
    # create it again and fail with a raw driver error the CLI can only name by type.
    import sqlite3

    path = tmp_path / "unstamped.db"
    with sqlite3.connect(path) as connection:
        connection.execute("CREATE TABLE mcp_api_keys (key_id TEXT PRIMARY KEY)")
    unstamped = RuntimeSettings(control_db_url=f"sqlite+aiosqlite:///{path}")

    with pytest.raises(api_key_operations.ApiKeySchemaUnstamped) as failure:
        await api_key_operations.ensure_key_schema(unstamped, migrate=True)

    message = str(failure.value)
    assert "mcp_api_keys" in message and "no migration stamp" in message
    assert "alembic" in message and "--migrate" in message


@pytest.fixture
def behind_head(tmp_path: Path) -> tuple[RuntimeSettings, str]:
    dsn = f"sqlite+aiosqlite:///{tmp_path}/behind.db"
    command.upgrade(_build_config(dsn), "0039")
    return RuntimeSettings(control_db_url=dsn), dsn


async def _version(dsn: str) -> str:
    from sqlalchemy import text

    from harborrag_adapters.repositories.database.control_plane.engine import (
        create_control_plane_engine,
    )

    engine = create_control_plane_engine(dsn)
    try:
        async with engine.connect() as connection:
            return str(
                (await connection.execute(text("SELECT version_num FROM alembic_version"))).scalar()
            )
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_migrate_upgrades_a_database_behind_head(
    behind_head: tuple[RuntimeSettings, str],
) -> None:
    behind, dsn = behind_head

    assert await api_key_operations.ensure_key_schema(behind, migrate=True) is True

    assert await _version(dsn) == head_revision(dsn)
    assert await api_key_operations.ensure_key_schema(behind, migrate=True) is False


@pytest.mark.asyncio
async def test_behind_head_without_migrate_is_reported(
    behind_head: tuple[RuntimeSettings, str],
) -> None:
    behind, dsn = behind_head

    with pytest.raises(api_key_operations.ApiKeySchemaBehind) as raised:
        await api_key_operations.ensure_key_schema(behind, migrate=False)

    message = str(raised.value)
    assert "0039" in message and head_revision(dsn) in message and "--migrate" in message
    assert await _version(dsn) == "0039"  # never migrated implicitly
    # The table exists, so operations that only need it may still proceed.
    assert (
        await api_key_operations.ensure_key_schema(behind, migrate=False, require_head=False)
        is False
    )


@pytest.mark.asyncio
async def test_a_constraint_rejection_becomes_a_fixed_message(
    settings: RuntimeSettings, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def reject(*args: object, **kwargs: object) -> None:
        raise IntegrityError("INSERT ... hunter2", {"secret_hash": "hunter2"}, Exception("hunter2"))

    monkeypatch.setattr(
        "harborrag_engine.security.ApiKeyManagementService.create_key", reject, raising=True
    )

    with pytest.raises(api_key_operations.ApiKeyRejected) as raised:
        await api_key_operations.create_key(
            settings, operator="op", tenant_id="t", owner="user-x", name="k", lifetime=timedelta(1)
        )

    assert "--migrate" in str(raised.value) and "hunter2" not in str(raised.value)


@pytest.mark.asyncio
async def test_revoke_key_distinguishes_unknown_from_already_revoked(
    settings: RuntimeSettings,
) -> None:
    created = await api_key_operations.create_key(
        settings, operator="op", tenant_id="t", owner="user-x", name="k", lifetime=timedelta(1)
    )

    async def revoke(key_id: str) -> str:
        return await api_key_operations.revoke_key(
            settings, operator="op", key_id=key_id, reason="r"
        )

    assert await revoke(created.key_id) == "revoked"
    assert await revoke(created.key_id) == "already_revoked"
    assert await revoke("f" * 24) == "not_found"
