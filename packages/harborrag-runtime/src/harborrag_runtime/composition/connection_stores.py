"""Control-plane stores an ingestion worker needs to build connectors.

Handles:
- connection rows (status updates and scheduled runs), connection revisions
  and stored secrets, over one control-plane engine
- the same encryption-key rule the API applies, checked at worker start

Does NOT run migrations; the API process owns the schema.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from harborrag_core.ports.control_plane import (
    ConnectionRepositoryPort,
    ConnectionRevisionRepositoryPort,
)
from harborrag_core.ports.secrets import SecretsPort

from .control_plane import secrets_encryption_key

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncEngine

    from harborrag_runtime.config.settings import RuntimeSettings


@dataclass(frozen=True, slots=True)
class ConnectionStores:
    """Connection rows, revisions and stored secrets sharing one engine."""

    connections: ConnectionRepositoryPort
    revisions: ConnectionRevisionRepositoryPort
    secrets: SecretsPort
    engine: AsyncEngine

    async def aclose(self) -> None:
        """Dispose the engine behind the stores."""
        await self.engine.dispose()


def build_connection_stores(settings: RuntimeSettings) -> ConnectionStores:
    """Open the control-plane stores a worker builds connectors from.

    Args:
        settings: Runtime settings with the control database URL and secrets key.

    Returns:
        Stores over a lazily connecting engine.

    Raises:
        HarborConfigurationError: The secrets key is blank for a non-SQLite database.
    """
    from harborrag_adapters.repositories.database.control_plane.connections import (
        SqlConnectionRepository,
        SqlConnectionRevisionRepository,
    )
    from harborrag_adapters.repositories.database.control_plane.engine import (
        create_control_plane_engine,
        create_session_factory,
    )
    from harborrag_adapters.repositories.database.control_plane.secrets import (
        SqlSecretsRepository,
    )

    key = secrets_encryption_key(settings)
    engine = create_control_plane_engine(settings.control_db_url.get_secret_value())
    sessions = create_session_factory(engine)
    return ConnectionStores(
        connections=SqlConnectionRepository(sessions),
        revisions=SqlConnectionRevisionRepository(sessions),
        secrets=SqlSecretsRepository(sessions, encryption_key=key),
        engine=engine,
    )
