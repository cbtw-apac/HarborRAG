from __future__ import annotations

from harborrag_adapters.repositories.backends.sqlalchemy import SQLAlchemyDBClient
from harborrag_adapters.repositories.backends.sqlite import sqlite_url


class SQLiteDBClient(SQLAlchemyDBClient):
    """Owns an embedded SQLite engine for canonical database persistence."""

    def __init__(
        self,
        *,
        database: str,
        echo: bool = False,
        backend_name: str = "sqlite",
    ) -> None:
        super().__init__(
            backend=backend_name,
            url=sqlite_url(database),
            pool_size=None,
            max_overflow=None,
            pool_recycle_seconds=1800,
            echo=echo,
        )
