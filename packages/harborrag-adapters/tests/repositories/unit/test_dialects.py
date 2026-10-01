from __future__ import annotations

import pytest
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert

from harborrag_adapters.repositories.database.sqlalchemy.dialects import insert_for_dialect
from harborrag_core.contracts import HarborNotSupportedError


class _FakeDialect:
    def __init__(self, name: str) -> None:
        self.name = name


class _FakeBind:
    def __init__(self, name: str) -> None:
        self.dialect = _FakeDialect(name)


class _FakeSession:
    """Stands in for an AsyncSession: resolves to a bind via get_bind()."""

    def __init__(self, name: str) -> None:
        self._bind = _FakeBind(name)

    def get_bind(self) -> _FakeBind:
        return self._bind


def test_insert_for_dialect_returns_sqlite_insert_for_sqlite_bind() -> None:
    assert insert_for_dialect(_FakeBind("sqlite")) is sqlite_insert


def test_insert_for_dialect_returns_postgresql_insert_for_postgresql_bind() -> None:
    assert insert_for_dialect(_FakeBind("postgresql")) is pg_insert


def test_insert_for_dialect_raises_for_unsupported_dialect() -> None:
    with pytest.raises(HarborNotSupportedError, match="mysql"):
        insert_for_dialect(_FakeBind("mysql"))


def test_insert_for_dialect_accepts_a_session_via_get_bind() -> None:
    assert insert_for_dialect(_FakeSession("sqlite")) is sqlite_insert
    assert insert_for_dialect(_FakeSession("postgresql")) is pg_insert
