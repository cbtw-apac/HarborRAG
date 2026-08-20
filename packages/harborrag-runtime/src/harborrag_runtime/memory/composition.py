"""Resource assembly for database-backed conversation memory."""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from harborrag_runtime.config.settings import RuntimeSettings

    from .service import DatabaseConversationMemory


def build_database_conversation_memory(
    settings: RuntimeSettings | None = None,
) -> DatabaseConversationMemory:
    """Migrate and open the configured control database for a standalone process."""

    from harborrag_adapters.repositories.database.control_plane.conversation import (
        SqlConversationMemoryRepository,
    )
    from harborrag_adapters.repositories.database.control_plane.engine import (
        create_control_plane_engine,
        create_session_factory,
    )
    from harborrag_adapters.repositories.database.control_plane.migrations import run_migrations
    from harborrag_core.contracts.errors import HarborConfigurationError
    from harborrag_runtime.config.settings import DEFAULT_CONTROL_DB_URL, RuntimeSettings

    from .service import DatabaseConversationMemory

    selected = settings or RuntimeSettings()
    dsn = selected.control_db_url.get_secret_value()
    if selected.env == "prod" and dsn == DEFAULT_CONTROL_DB_URL:
        raise HarborConfigurationError(
            "conversation memory requires HARBORRAG_CONTROL_DB_URL in production"
        )
    run_migrations(dsn)
    engine = create_control_plane_engine(dsn)
    repository = SqlConversationMemoryRepository(create_session_factory(engine))
    return DatabaseConversationMemory(repository=repository, engine=engine)


__all__ = ["build_database_conversation_memory"]
