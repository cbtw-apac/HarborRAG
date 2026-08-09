from __future__ import annotations

from collections.abc import Mapping

from harborrag_adapters.repositories.vector.qdrant.client import QdrantDBClient
from harborrag_adapters.repositories.vector.qdrant.config import QdrantVectorConfig
from harborrag_core.storage import (
    HealthStatus,
    RepositoryHealth,
    StorageFamily,
)


async def qdrant_health(
    database: QdrantDBClient,
    config: QdrantVectorConfig,
) -> RepositoryHealth:
    """Probe Qdrant while returning only bounded, secret-safe diagnostics."""

    details = {
        "deployment": database.deployment,
        "storage": database.storage,
    }
    if not database.is_connected:
        return _result(config, HealthStatus.UNKNOWN, details)
    try:
        await database.ping()
        return _result(config, HealthStatus.HEALTHY, details)
    except Exception as error:  # pragma: no cover - integration behavior
        return _result(
            config,
            HealthStatus.UNHEALTHY,
            {**details, "error_type": type(error).__name__},
        )


def _result(
    config: QdrantVectorConfig,
    status: HealthStatus,
    details: Mapping[str, object],
) -> RepositoryHealth:
    return RepositoryHealth(
        family=StorageFamily.VECTOR,
        backend="qdrant",
        instance_name=config.instance_name,
        status=status,
        details=dict(details),
    )
