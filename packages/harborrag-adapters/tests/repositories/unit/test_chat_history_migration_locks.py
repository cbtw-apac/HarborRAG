"""Migration 0047 bounds its lock wait on Postgres in both directions."""

from __future__ import annotations

import importlib.util
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from types import ModuleType, SimpleNamespace

import pytest

import harborrag_adapters.repositories.database.control_plane as control_plane

pytestmark = pytest.mark.unit

_MIGRATION = (
    Path(control_plane.__file__).parent / "alembic" / "versions" / "0047_chat_history_contract.py"
)


class _RecordingOp:
    """The slice of ``alembic.op`` the migration calls, recorded in order."""

    def __init__(self, dialect: str) -> None:
        self.dialect = dialect
        self.calls: list[tuple[str, ...]] = []

    def get_bind(self) -> SimpleNamespace:
        return SimpleNamespace(dialect=SimpleNamespace(name=self.dialect))

    def execute(self, statement: str) -> None:
        self.calls.append(("execute", statement))

    def add_column(self, table: str, column: object) -> None:
        self.calls.append(("add_column", table))

    @contextmanager
    def batch_alter_table(self, table: str) -> Iterator[SimpleNamespace]:
        yield SimpleNamespace(drop_column=lambda name: self.calls.append(("drop", table, name)))


def _migration(op: _RecordingOp) -> ModuleType:
    spec = importlib.util.spec_from_file_location("migration_0047", _MIGRATION)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    module.op = op  # type: ignore[attr-defined]
    return module


@pytest.mark.parametrize("direction", ["upgrade", "downgrade"])
def test_each_direction_sets_the_lock_timeout_first_on_postgres(direction: str) -> None:
    op = _RecordingOp("postgresql")

    getattr(_migration(op), direction)()

    assert op.calls[0] == ("execute", "SET LOCAL lock_timeout = '5s'")


def test_sqlite_sets_no_lock_timeout_on_downgrade() -> None:
    op = _RecordingOp("sqlite")

    _migration(op).downgrade()

    assert all(call[0] != "execute" for call in op.calls)
