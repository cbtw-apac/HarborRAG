from __future__ import annotations

from harborrag_adapters.repositories.backends.sqlalchemy import SQLAlchemyDBClient
from harborrag_adapters.repositories.backends.sqlite import prepare_sqlite_database


class SQLiteStateDBClient(SQLAlchemyDBClient):
    """Owns an embedded SQLite engine dedicated to operational workflow state."""

    def __init__(self, *, database: str) -> None:
        super().__init__(
            backend="sqlite",
            url=prepare_sqlite_database(database),
            pool_size=None,
            max_overflow=None,
            pool_recycle_seconds=1800,
            echo=False,
        )
