from __future__ import annotations

from harborrag_adapters.repositories.backends.sqlalchemy import SQLAlchemyDBClient
from harborrag_adapters.repositories.database import IngestionControlPlaneDatabase

from .config.settings import RuntimeSettings


def build_ingestion_control(
    settings: RuntimeSettings,
) -> IngestionControlPlaneDatabase:
    """Create the Postgres authority without importing projection providers."""

    url = settings.control_db_url.get_secret_value()
    backend = "postgresql" if url.startswith("postgresql+asyncpg://") else "sqlite"
    client = SQLAlchemyDBClient(
        backend=backend,
        url=url,
        pool_size=(settings.control_db_pool_size if backend == "postgresql" else None),
        max_overflow=(settings.control_db_max_overflow if backend == "postgresql" else None),
        pool_recycle_seconds=1800,
        echo=False,
    )
    return IngestionControlPlaneDatabase(client, create_schema=False)
