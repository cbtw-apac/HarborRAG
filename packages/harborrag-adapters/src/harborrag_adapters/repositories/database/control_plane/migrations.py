"""Programmatic Alembic runner (ST5).

Migrations run at composition time — no CLI, no cwd dependence, so the
alembic script path resolves the same from any working directory. env.py drives the async engine via
asyncio.run, so when run_migrations is itself called from inside a running
event loop it hops to a worker thread first.
"""

from __future__ import annotations

import asyncio
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import TYPE_CHECKING

from harborrag_adapters.repositories.errors import (
    MissingOptionalDependencyError,
    SchemaAheadOfBuildError,
)

if TYPE_CHECKING:
    from sqlalchemy.engine import Connection

try:
    from alembic import command
    from alembic.config import Config
    from alembic.runtime.migration import MigrationContext
    from alembic.script import ScriptDirectory
    from alembic.script.revision import ResolutionError
    from alembic.util import CommandError
except ImportError as exc:  # pragma: no cover - exercised by the bare install check
    raise MissingOptionalDependencyError("alembic") from exc

_SCRIPT_LOCATION = Path(__file__).parent / "alembic"


def _build_config(dsn: str) -> Config:
    """Alembic config pointing at the packaged migration scripts."""
    config = Config()
    config.set_main_option("script_location", str(_SCRIPT_LOCATION))
    # Alembic stores main options in a ConfigParser. Percent-encoded
    # credentials must therefore escape percent signs at the configuration
    # boundary; reading the option in env.py restores the original DSN.
    config.set_main_option("sqlalchemy.url", dsn.replace("%", "%%"))
    return config


def head_revision(dsn: str) -> str:
    """The newest packaged revision; read from the scripts, no database needed."""
    return _packaged_head(_build_config(dsn))


def _packaged_head(config: Config) -> str:
    head = ScriptDirectory.from_config(config).get_current_head()
    if head is None:  # pragma: no cover - the package always ships migrations
        raise MissingOptionalDependencyError("alembic migrations")
    return head


def _upgrade_to_head(config: Config) -> None:
    """Upgrade to head, naming a database stamped by a build newer than this one.

    Upgrading to ``head`` resolves only the packaged head and the database's stamped
    revision, so a revision Alembic cannot resolve is the database's own stamp: one
    this build does not ship.
    """
    try:
        command.upgrade(config, "head")
    except CommandError as exc:
        cause = exc.__cause__
        if not isinstance(cause, ResolutionError):
            raise
        raise SchemaAheadOfBuildError(
            database_revision=cause.argument, build_head=_packaged_head(config)
        ) from exc


def current_revision(sync_connection: Connection) -> str | None:
    """The revision the connected database is stamped at; None when never migrated."""
    return MigrationContext.configure(sync_connection).get_current_revision()


def run_migrations(dsn: str) -> None:
    """Upgrade the control-plane schema to head; idempotent.

    Safe to call from sync code and from within a running event loop
    (lifespan startup): the loop case runs Alembic on a worker thread so
    env.py's asyncio.run gets a clean thread.

    Raises SchemaAheadOfBuildError when a newer build already migrated the
    database past every revision this build ships.
    """
    config = _build_config(dsn)
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        _upgrade_to_head(config)
        return
    with ThreadPoolExecutor(max_workers=1) as pool:
        pool.submit(_upgrade_to_head, config).result()
