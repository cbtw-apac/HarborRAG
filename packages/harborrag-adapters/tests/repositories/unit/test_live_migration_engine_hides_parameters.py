from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
import sqlalchemy.ext.asyncio as sqlalchemy_asyncio
from sqlalchemy.ext.asyncio import AsyncEngine

from harborrag_adapters.repositories.database.control_plane.migrations import run_migrations

pytestmark = pytest.mark.whitebox


def test_migration_engine_hides_sql_parameters(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[dict[str, Any]] = []
    real = sqlalchemy_asyncio.create_async_engine

    def spy(url: str, **options: Any) -> AsyncEngine:
        calls.append(options)
        return real(url, **options)

    monkeypatch.setattr(sqlalchemy_asyncio, "create_async_engine", spy)

    run_migrations(f"sqlite+aiosqlite:///{tmp_path}/control.db")

    assert calls
    assert all(options.get("hide_parameters") is True for options in calls)
