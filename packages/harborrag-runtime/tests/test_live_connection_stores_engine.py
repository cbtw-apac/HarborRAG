from __future__ import annotations

from typing import Any

import pytest
from pydantic import SecretStr
from sqlalchemy.ext.asyncio import AsyncEngine

from harborrag_adapters.repositories.database.control_plane import engine as engine_module
from harborrag_runtime.composition.connection_stores import build_connection_stores
from harborrag_runtime.config.settings import RuntimeSettings

pytestmark = pytest.mark.whitebox


@pytest.mark.asyncio
async def test_worker_connection_store_engine_gets_pre_ping_and_the_configured_pool(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[dict[str, Any]] = []
    real = engine_module.create_control_plane_engine

    def spy(dsn: str, **pool_options: Any) -> AsyncEngine:
        calls.append(pool_options)
        return real(dsn, **pool_options)

    monkeypatch.setattr(engine_module, "create_control_plane_engine", spy)
    settings = RuntimeSettings(
        control_db_url=SecretStr("postgresql+asyncpg://demo:demo@127.0.0.1:5432/demo"),
        secrets_encryption_key=SecretStr("11" * 32),
        control_db_pool_size=7,
        control_db_max_overflow=3,
    )

    stores = build_connection_stores(settings)
    await stores.aclose()

    assert calls == [
        {"pool_pre_ping": True, "pool_recycle": 1800, "pool_size": 7, "max_overflow": 3}
    ]
    assert stores.engine.sync_engine.pool.size() == 7
