from __future__ import annotations

import pytest

from harborrag_adapters.repositories.backends import sqlalchemy as sqlalchemy_module
from harborrag_adapters.repositories.backends.sqlalchemy import SQLAlchemyDBClient
from harborrag_adapters.repositories.backends.sqlite import sqlite_url


def make_client(
    *, pool_size: int | None = None, max_overflow: int | None = None
) -> SQLAlchemyDBClient:
    return SQLAlchemyDBClient(
        backend="sqlite",
        url=sqlite_url(":memory:"),
        pool_size=pool_size,
        max_overflow=max_overflow,
        pool_recycle_seconds=1800,
        echo=False,
    )


def test_require_url_prefix_accepts_matching_prefix() -> None:
    SQLAlchemyDBClient.require_url_prefix("sqlite+aiosqlite:///x", "sqlite+aiosqlite://", "sqlite")


def test_require_url_prefix_rejects_mismatched_prefix() -> None:
    with pytest.raises(ValueError, match="sqlite URL must use"):
        SQLAlchemyDBClient.require_url_prefix(
            "postgresql+asyncpg://x", "sqlite+aiosqlite://", "sqlite"
        )


def test_raw_raises_when_not_connected() -> None:
    client = make_client()
    with pytest.raises(RuntimeError, match="not connected"):
        _ = client.raw


def test_sessions_raises_when_not_connected() -> None:
    client = make_client()
    with pytest.raises(RuntimeError, match="not connected"):
        _ = client.sessions


@pytest.mark.asyncio
async def test_connect_is_idempotent_and_close_disposes_engine() -> None:
    client = make_client()
    await client.connect()
    engine_first = client.raw
    await client.connect()  # second call must be a no-op (early return branch)
    assert client.raw is engine_first
    await client.close()
    with pytest.raises(RuntimeError, match="not connected"):
        _ = client.raw


@pytest.mark.asyncio
async def test_close_without_prior_connect_is_a_safe_no_op() -> None:
    client = make_client()
    await client.close()  # engine is None; dispose branch must be skipped
    with pytest.raises(RuntimeError, match="not connected"):
        _ = client.raw


@pytest.mark.asyncio
async def test_connect_applies_pool_size_and_max_overflow_when_provided(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, object] = {}

    class FakeEngine:
        def __init__(self) -> None:
            self.disposed = False

        async def dispose(self, *, close: bool) -> None:
            assert close is True
            self.disposed = True

    engine = FakeEngine()

    def create_engine(url: str, **kwargs: object) -> FakeEngine:
        captured.update(url=url, **kwargs)
        return engine

    monkeypatch.setattr(sqlalchemy_module, "create_async_engine", create_engine)
    monkeypatch.setattr(
        sqlalchemy_module,
        "async_sessionmaker",
        lambda bound_engine, *, expire_on_commit: (bound_engine, expire_on_commit),
    )
    client = SQLAlchemyDBClient(
        backend="postgresql",
        url="postgresql+asyncpg://user:pass@localhost/db",
        pool_size=3,
        max_overflow=7,
        pool_recycle_seconds=1800,
        echo=False,
    )
    await client.connect()

    assert captured == {
        "url": "postgresql+asyncpg://user:pass@localhost/db",
        "echo": False,
        "pool_pre_ping": True,
        "pool_recycle": 1800,
        "pool_size": 3,
        "max_overflow": 7,
    }

    await client.close()
    assert engine.disposed is True


@pytest.mark.asyncio
async def test_ping_executes_select_one_against_a_live_connection() -> None:
    client = make_client()
    await client.connect()
    try:
        await client.ping()
    finally:
        await client.close()


def test_backend_property_reports_configured_backend_name() -> None:
    client = make_client()
    assert client.backend == "sqlite"


def test_sqlite_url_rejects_empty_database_location() -> None:
    with pytest.raises(ValueError, match="cannot be empty"):
        sqlite_url("   ")


def test_sqlite_url_builds_memory_url() -> None:
    assert sqlite_url(":memory:") == "sqlite+aiosqlite:///:memory:"


def test_sqlite_url_builds_absolute_path_url(tmp_path) -> None:  # type: ignore[no-untyped-def]
    target = tmp_path / "nested" / "harbor.db"
    url = sqlite_url(str(target))
    assert url == f"sqlite+aiosqlite:///{target}"
    assert target.parent.is_dir()
