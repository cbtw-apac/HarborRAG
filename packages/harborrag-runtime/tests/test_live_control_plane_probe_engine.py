from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
import sqlalchemy.ext.asyncio as sqlalchemy_asyncio
from sqlalchemy.ext.asyncio import AsyncEngine

from harborrag_runtime.composition.control_plane import _probe_control_db

pytestmark = pytest.mark.whitebox


def test_control_plane_probe_engine_hides_sql_parameters(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[dict[str, Any]] = []
    real = sqlalchemy_asyncio.create_async_engine

    def spy(url: str, **options: Any) -> AsyncEngine:
        calls.append(options)
        return real(url, **options)

    monkeypatch.setattr(sqlalchemy_asyncio, "create_async_engine", spy)

    _probe_control_db(f"sqlite+aiosqlite:///{tmp_path}/control.db")

    assert calls == [{"hide_parameters": True}]
