"""Migration 0050 and the MCP server's grant check agree on the tables the MCP login reads."""

from __future__ import annotations

import importlib.util
import re
from collections.abc import AsyncIterator, Iterator
from contextlib import asynccontextmanager
from pathlib import Path
from types import ModuleType, SimpleNamespace

import pytest
from sqlalchemy.sql.elements import TextClause

import harborrag_adapters.repositories.database.control_plane as control_plane
from harborrag_adapters.repositories.database.control_plane.reader_grants import (
    MCP_READER_LOGIN,
    MCP_READER_TABLES,
    tables_beyond_reader_grants,
)
from harborrag_adapters.repositories.database.control_plane.schemas import Base
from harborrag_adapters.repositories.database.ingestion_control.schema import (
    METADATA as INGESTION_METADATA,
)
from harborrag_core.contracts.errors import HarborUnavailableError

pytestmark = pytest.mark.unit

_MIGRATION = (
    Path(control_plane.__file__).parent / "alembic" / "versions" / "0051_mcp_reader_grants.py"
)


class _RecordingOp:
    """The slice of ``alembic.op`` the migration calls, recorded in order."""

    def __init__(self, dialect: str) -> None:
        self.dialect = dialect
        self.statements: list[str] = []

    def get_context(self) -> SimpleNamespace:
        return SimpleNamespace(dialect=SimpleNamespace(name=self.dialect))

    def execute(self, statement: str) -> None:
        self.statements.append(statement)


def _migration(op: _RecordingOp) -> ModuleType:
    spec = importlib.util.spec_from_file_location("migration_0050", _MIGRATION)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    module.op = op  # type: ignore[attr-defined]  # the recording stand-in for alembic.op
    return module


def _upgrade_sql() -> str:
    op = _RecordingOp("postgresql")
    _migration(op).upgrade()
    return "\n".join(op.statements)


def test_migration_grants_exactly_the_tables_the_server_checks() -> None:
    frozen = _migration(_RecordingOp("postgresql"))._MCP_READER_TABLES

    assert len(frozen) == len(set(frozen))
    assert set(frozen) == MCP_READER_TABLES
    for table in MCP_READER_TABLES:
        assert f"'{table}'" in _upgrade_sql()


def test_every_allow_listed_table_is_a_control_plane_table() -> None:
    tables = set(Base.metadata.tables) | set(INGESTION_METADATA.tables)

    assert MCP_READER_TABLES <= tables


def test_migration_creates_the_login_the_server_checks() -> None:
    assert f"CREATE ROLE {MCP_READER_LOGIN} NOLOGIN" in _upgrade_sql()


@pytest.mark.parametrize("direction", ["upgrade", "downgrade"])
def test_migration_does_nothing_on_sqlite(direction: str) -> None:
    op = _RecordingOp("sqlite")

    getattr(_migration(op), direction)()

    assert op.statements == []


@pytest.mark.parametrize("direction", ["upgrade", "downgrade"])
def test_migration_sets_the_lock_timeout_first_on_postgres(direction: str) -> None:
    op = _RecordingOp("postgresql")

    getattr(_migration(op), direction)()

    assert op.statements[0] == "SET LOCAL lock_timeout = '5s'"


def test_upgrade_grants_only_reads_and_sets_no_password() -> None:
    statements = _upgrade_sql().upper()

    granted = set(re.findall(r"\bGRANT (\w+) ON\b", statements))
    assert granted == {"CONNECT", "USAGE", "SELECT"}
    assert "GRANT HARBORRAG_MCP_READ TO HARBORRAG_MCP" in statements
    assert "REVOKE ALL ON ALL TABLES IN SCHEMA PUBLIC FROM HARBORRAG_MCP, HARBORRAG_MCP_READ" in (
        statements
    )
    for clause in ("PASSWORD", "LOGIN PASSWORD", "NOREPLICATION", "NOBYPASSRLS", "ON TABLES TO"):
        assert clause not in statements


def test_downgrade_restores_no_broader_grant() -> None:
    op = _RecordingOp("postgresql")

    _migration(op).downgrade()

    statements = "\n".join(op.statements).upper()
    assert "REVOKE ALL ON ALL TABLES IN SCHEMA PUBLIC FROM HARBORRAG_MCP_READ" in statements
    assert "GRANT" not in statements


class _Connection:
    """Answers the readable-tables query with fixed names and keeps the statement."""

    def __init__(self, names: tuple[str, ...]) -> None:
        self.names = names
        self.statement: TextClause | None = None

    async def scalars(self, statement: TextClause) -> Iterator[str]:
        self.statement = statement
        return iter(self.names)


class _Engine:
    def __init__(self, connection: _Connection) -> None:
        self.connection = connection

    @asynccontextmanager
    async def connect(self) -> AsyncIterator[_Connection]:
        yield self.connection


class _UnreachableEngine:
    def connect(self) -> None:
        raise OSError("connection refused")


@pytest.mark.asyncio
async def test_tables_beyond_reader_grants_returns_the_readable_tables_off_the_list() -> None:
    connection = _Connection(("documents", "ingestion_tasks", "mcp_api_keys", "task_events"))

    beyond = await tables_beyond_reader_grants(_Engine(connection))  # type: ignore[arg-type]  # a stand-in engine

    assert beyond == ("ingestion_tasks", "task_events")
    assert connection.statement is not None
    assert connection.statement.compile().params == {"login": MCP_READER_LOGIN}


@pytest.mark.asyncio
async def test_tables_beyond_reader_grants_is_empty_when_only_allowed_tables_are_readable() -> None:
    connection = _Connection(tuple(sorted(MCP_READER_TABLES)))

    assert await tables_beyond_reader_grants(_Engine(connection)) == ()  # type: ignore[arg-type]  # a stand-in engine


@pytest.mark.asyncio
async def test_tables_beyond_reader_grants_names_an_unreachable_database() -> None:
    with pytest.raises(HarborUnavailableError, match="grants cannot be read"):
        await tables_beyond_reader_grants(_UnreachableEngine())  # type: ignore[arg-type]  # a stand-in engine
