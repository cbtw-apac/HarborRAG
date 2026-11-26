"""The API's control-plane engine gets pre-ping, recycle and the configured pool sizes."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from sqlalchemy.ext.asyncio import AsyncEngine

from harborrag_adapters.repositories.database.control_plane import engine as engine_module
from harborrag_runtime.composition import CompositionRoot
from harborrag_runtime.config.settings import RuntimeSettings

pytestmark = pytest.mark.whitebox


@pytest.mark.asyncio
async def test_production_control_plane_engine_gets_the_configured_pool_options(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[dict[str, Any]] = []
    real = engine_module.create_control_plane_engine

    def spy(dsn: str, **pool_options: Any) -> AsyncEngine:
        calls.append(pool_options)
        return real(dsn, **pool_options)

    monkeypatch.setattr(engine_module, "create_control_plane_engine", spy)
    settings = RuntimeSettings(
        control_db_url=f"sqlite+aiosqlite:///{tmp_path}/control.db",
        control_db_pool_size=7,
        control_db_max_overflow=3,
    )

    composition = CompositionRoot.production(settings)
    await composition.aclose()

    assert calls == [
        {"pool_pre_ping": True, "pool_recycle": 1800, "pool_size": 7, "max_overflow": 3}
    ]
