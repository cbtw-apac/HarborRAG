"""Issue, verify, list and revoke a reader key end to end on a migrated control DB."""

from __future__ import annotations

from datetime import timedelta
from pathlib import Path

import pytest

from harborrag_adapters.repositories.database.control_plane.migrations import run_migrations
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

    assert await api_key_operations.revoke_key(
        settings, operator="op@host", key_id=created.key_id, reason="lost"
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

    assert await api_key_operations.ensure_key_schema(empty, migrate=True) is True
    assert await api_key_operations.ensure_key_schema(empty, migrate=False) is False
    created = await api_key_operations.create_key(
        empty, operator="op", tenant_id="t", owner="user-x", name="k", lifetime=timedelta(hours=1)
    )
    assert created.key_id
