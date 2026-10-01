"""One dialect-aware upsert helper shared by every SQLAlchemy repository.

Six call sites across ``ingestion_control`` used to pick the ``INSERT``
constructor by hand, each repeating ``sqlite_insert if <dialect> == "sqlite"
else pg_insert`` (or, in one case, ``postgresql_insert if backend ==
"postgresql" else sqlite_insert``). Any dialect other than the one spelled out
silently took the wrong branch instead of failing loudly.

``insert_for_dialect`` centralizes that choice. Every site has a session in
scope (``task_results.py`` included -- it reads ``self._client.backend`` only
because nothing forced it to use the session that is already open), so one
parameter shape covers all six: a session/bind that resolves to a dialect
name. It accepts either:

- an ``AsyncSession``/``Session`` (resolved through its ``get_bind()``); or
- a bind/engine/connection that already exposes ``dialect.name`` directly
  (what ``get_bind()`` itself returns, and handy for tests).

A configured provider name (``SQLAlchemyDBClient.backend``) is deliberately
not accepted: it is a label the caller chose, not the dialect SQLAlchemy is
actually bound to, so trusting it instead of the live bind would reintroduce
the same kind of silent mismatch this helper exists to close.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from sqlalchemy.dialects.postgresql import Insert as PostgreSQLInsert
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.dialects.sqlite import Insert as SQLiteInsert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert

from harborrag_core.contracts import HarborNotSupportedError

_INSERT_FACTORIES: dict[str, Callable[..., PostgreSQLInsert | SQLiteInsert]] = {
    "sqlite": sqlite_insert,
    "postgresql": pg_insert,
}


def insert_for_dialect(
    session_or_bind: Any,
) -> Callable[..., PostgreSQLInsert | SQLiteInsert]:
    """Return the ``INSERT`` constructor matching ``session_or_bind``'s dialect.

    ``session_or_bind`` may be a session (anything with ``get_bind()``) or a
    bind/engine/connection (anything with ``dialect.name``).

    Raises:
        HarborNotSupportedError: the resolved dialect is neither "sqlite"
            nor "postgresql".
    """
    bind = (
        session_or_bind.get_bind() if hasattr(session_or_bind, "get_bind") else session_or_bind
    )
    dialect_name = bind.dialect.name
    try:
        return _INSERT_FACTORIES[dialect_name]
    except KeyError:
        raise HarborNotSupportedError(
            f"no dialect-aware insert constructor for SQL dialect {dialect_name!r}"
        ) from None
