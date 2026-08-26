from typing import Literal

from pydantic import Field

from harborrag_adapters.repositories.plugin import RepositoryConfig


class SQLiteStateConfig(RepositoryConfig):
    """Configures embedded SQLite for checkpoints, workflow state, and leases."""

    backend: Literal["sqlite"] = "sqlite"
    database: str = Field(default=":memory:", min_length=1)
    create_schema: bool = False
