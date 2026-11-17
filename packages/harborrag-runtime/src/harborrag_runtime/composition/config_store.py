"""Composition of the settings store and its cached provider."""

from __future__ import annotations

from typing import TYPE_CHECKING

from harborrag_runtime.config_store import CachedConfigProvider
from harborrag_runtime.config_store.defaults import registry_for

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncEngine

    from harborrag_core.ports.config_store import SettingsStore
    from harborrag_runtime.config.settings import RuntimeSettings

_POSTGRESQL = "postgresql"


def build_settings_store(engine: AsyncEngine) -> SettingsStore:
    """Return the SQL settings store over ``engine``."""
    from harborrag_adapters.repositories.database.control_plane.config_store import (
        SqlSettingsStore,
    )
    from harborrag_adapters.repositories.database.control_plane.engine import (
        create_session_factory,
    )

    return SqlSettingsStore(create_session_factory(engine))


def build_config_provider(
    settings: RuntimeSettings, *, engine: AsyncEngine, store: SettingsStore | None = None
) -> CachedConfigProvider:
    """Build the provider; on Postgres it also listens for change notifications.

    Args:
        settings: Runtime settings with the control database URL and model configuration.
        engine: Control-plane engine; its dialect decides whether a feed is attached.
        store: Settings store to read; defaults to one over ``engine``.

    Returns:
        A provider that is not started.
    """
    feed = None
    if engine.dialect.name == _POSTGRESQL:
        from harborrag_adapters.repositories.database.control_plane.config_store_feed import (
            PostgresSettingsChangeFeed,
        )

        feed = PostgresSettingsChangeFeed(settings.control_db_url.get_secret_value())
    return CachedConfigProvider(
        store or build_settings_store(engine), registry_for(settings), feed=feed
    )
