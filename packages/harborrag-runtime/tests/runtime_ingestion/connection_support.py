"""Build connections and their resolved snapshots for runtime tests."""

from __future__ import annotations

from typing import Any

from harborrag_core.domain.connection import Connection, ConnectionBuilder
from harborrag_core.domain.connection_snapshot import ConnectionSnapshot
from harborrag_runtime.config.connectors.models import source_types
from harborrag_runtime.ingestion.connections import ResolvedConnection


def resolved_connection(  # noqa: PLR0913 - one keyword per connection part
    source_type: str,
    *,
    tenant_id: str = "tenant-1",
    source_key: str = "docs",
    credentials: dict[str, Any] | None = None,
    config: dict[str, Any] | None = None,
    tuning: dict[str, Any] | None = None,
) -> ResolvedConnection:
    """Validate the parts against the source type and return the run's view of them."""
    spec = source_types.get(source_type)
    validated = spec.validate(credentials or {}, config or {}, tuning)
    snapshot = ConnectionSnapshot(
        tenant_id=tenant_id,
        source_key=source_key,
        source_type=spec.source_type,
        credentials=validated.credentials,
        config=validated.config,
        tuning=validated.tuning,
    )
    return ResolvedConnection(snapshot, spec)


def connection_row(  # noqa: PLR0913 - one keyword per connection part
    source_type: str,
    *,
    tenant_id: str = "tenant-1",
    project_id: str = "default",
    connection_id: str = "docs",
    source_key: str | None = None,
    credentials: dict[str, Any] | None = None,
    config: dict[str, Any] | None = None,
    tuning: dict[str, Any] | None = None,
) -> Connection:
    """Return a stored connection row as the API or the YAML import writes it."""
    return (
        ConnectionBuilder.for_new(
            tenant_id=tenant_id, project_id=project_id, connection_id=connection_id
        )
        .source_type(source_types.get(source_type).source_type)
        .source_key(source_key or connection_id)
        .credentials(credentials or {})
        .config(config or {})
        .tuning(tuning or {})
        .build()
    )
