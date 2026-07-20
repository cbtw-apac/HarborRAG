from typing import Literal

from pydantic import Field

from harborrag_adapters.repositories.plugin import RepositoryConfig


class SQLiteDatabaseConfig(RepositoryConfig):
    """Configures embedded SQLite for canonical HarborRAG domain data."""

    backend: Literal["sqlite"] = "sqlite"
    database: str = Field(default="./.local/harborrag.db", min_length=1)
    echo: bool = False
    create_schema: bool = False
