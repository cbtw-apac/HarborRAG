"""SqlMcpApiKeyRepository maps driver failures to AuthStoreUnavailable."""

from __future__ import annotations

import logging
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from typing import cast

import pytest
from sqlalchemy.exc import OperationalError

from harborrag_adapters.repositories.database.control_plane.mcp_api_keys import (
    PostgresApiKeyManager,
    PostgresApiKeyReader,
    SqlMcpApiKeyRepository,
)
from harborrag_adapters.repositories.database.control_plane.session import SessionFactory
from harborrag_core.contracts.errors import AuthStoreUnavailable
from harborrag_core.ports.api_keys import Revocation

_SECRET_HASH = "ab" * 32


class _BrokenSessions:
    """Stands in for async_sessionmaker; every session fails like a dead database."""

    def __call__(self):
        return self._fail()

    def begin(self):
        return self._fail()

    @asynccontextmanager
    async def _fail(self) -> AsyncGenerator[None, None]:
        raise OperationalError(
            "SELECT ...", {"secret_hash": _SECRET_HASH}, ConnectionRefusedError("db down")
        )
        yield  # prama: no cover - makes this a generator


@pytest.fixture
def repo() -> SqlMcpApiKeyRepository:
    return SqlMcpApiKeyRepository(cast(SessionFactory, _BrokenSessions()))


@pytest.mark.asyncio
async def test_get_wraps_driver_error(repo: SqlMcpApiKeyRepository) -> None:
    with pytest.raises(AuthStoreUnavailable, match="during get") as info:
        await repo.get("a" * 24)

    # `from None`: the SQLAlchemy error (and its parameters) is not chained.
    assert info.value.__cause__ is None
    assert info.value.__suppress_context__ is True


@pytest.mark.asyncio
async def test_revoke_wraps_driver_error(repo: SqlMcpApiKeyRepository) -> None:
    with pytest.raises(AuthStoreUnavailable, match="during revoke"):
        await repo.revoke("a" * 24, tenant_id="tenant-a", revoked_by="alice")


@pytest.mark.asyncio
async def test_failure_log_names_error_type_without_parameters(
    repo: SqlMcpApiKeyRepository, caplog: pytest.LogCaptureFixture
) -> None:
    with caplog.at_level(logging.ERROR), pytest.raises(AuthStoreUnavailable):
        await repo.get("a" * 24)

    assert "OperationalError" in caplog.text
    assert _SECRET_HASH not in caplog.text


def test_repository_exposes_no_delete() -> None:
    # Rows are never deleted; revocation is the only lifecycle exit.
    assert not any("delete" in name for name in dir(SqlMcpApiKeyRepository))


@pytest.mark.asyncio
async def test_reader_wraps_driver_error() -> None:
    reader = PostgresApiKeyReader(cast(SessionFactory, _BrokenSessions()))

    with pytest.raises(AuthStoreUnavailable, match="during get") as info:
        await reader.get_by_key_id("a" * 24)

    assert info.value.__cause__ is None


@pytest.mark.asyncio
async def test_manager_wraps_driver_errors() -> None:
    manager = PostgresApiKeyManager(cast(SessionFactory, _BrokenSessions()))
    revocation = Revocation(at=datetime.now(UTC), by="alice")

    with pytest.raises(AuthStoreUnavailable, match="during revoke_by_owner"):
        await manager.revoke_by_owner("tenant-a", "owner-1", revocation=revocation)
    with pytest.raises(AuthStoreUnavailable, match="during list_for_tenant"):
        await manager.list_for_tenant("tenant-a")


def test_reader_exposes_no_write_methods() -> None:
    # The MCP server only ever gets a reader; it must have nothing to mutate with.
    public = {name for name in dir(PostgresApiKeyReader) if not name.startswith("_")}
    assert public == {"get_by_key_id", "sessions"}
