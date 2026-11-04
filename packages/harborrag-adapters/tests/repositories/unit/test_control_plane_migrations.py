"""The control-plane migration runner names a database migrated by a newer build."""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from harborrag_adapters.repositories.database.control_plane.migrations import (
    head_revision,
    run_migrations,
)
from harborrag_adapters.repositories.errors import SchemaAheadOfBuildError

# A revision this build will never ship; a real case is a feature branch's 0043
# against a build whose newest script is 0040.
_NEWER_REVISION = "9999"


def _migrated_by_a_newer_build(tmp_path: Path) -> str:
    """A control DB at this build's head, then stamped at a revision it does not ship."""
    path = tmp_path / "control.db"
    dsn = f"sqlite+aiosqlite:///{path}"
    run_migrations(dsn)
    connection = sqlite3.connect(path)
    connection.execute("UPDATE alembic_version SET version_num = ?", (_NEWER_REVISION,))
    connection.commit()
    connection.close()
    return dsn


def test_database_ahead_of_this_build_names_both_revisions(tmp_path: Path) -> None:
    dsn = _migrated_by_a_newer_build(tmp_path)

    with pytest.raises(SchemaAheadOfBuildError) as caught:
        run_migrations(dsn)

    assert caught.value.database_revision == _NEWER_REVISION
    assert caught.value.build_head == head_revision(dsn)


@pytest.mark.asyncio
async def test_database_ahead_of_this_build_is_named_from_a_running_loop(tmp_path: Path) -> None:
    """Lifespan startup runs Alembic on a worker thread; the error must survive the hop."""
    dsn = _migrated_by_a_newer_build(tmp_path)

    with pytest.raises(SchemaAheadOfBuildError) as caught:
        run_migrations(dsn)

    assert caught.value.database_revision == _NEWER_REVISION
    assert caught.value.build_head == head_revision(dsn)
