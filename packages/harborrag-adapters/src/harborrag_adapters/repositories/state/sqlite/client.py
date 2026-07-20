from __future__ import annotations

from harborrag_adapters.repositories.shared.sqlalchemy import SQLAlchemyDBClient
from harborrag_adapters.repositories.shared.sqlite import sqlite_url


class SQLiteStateDBClient(SQLAlchemyDBClient):
    """Owns an embedded SQLite engine dedicated to operational workflow state."""

    def __init__(self, *, database: str) -> None:
        super().__init__(
            backend="sqlite",
            url=sqlite_url(database),
            pool_size=None,
            max_overflow=None,
            pool_recycle_seconds=1800,
            echo=False,
        )
