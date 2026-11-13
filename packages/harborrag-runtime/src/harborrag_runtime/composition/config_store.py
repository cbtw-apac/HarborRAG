"""Composition of the settings store and its config provider.

Both ``build_settings_store`` and ``build_config_provider`` are backed by
``setting_overrides``/``setting_scope_revisions`` (the same tables
``/v1/admin/settings`` reads and writes) through
``SettingOverridesConfigStore``, so chat, the MCP server, and ingestion's
``RunSettingsResolver`` see exactly the overrides an admin set through the
HTTP API, at the same per-scope revision -- one settings store, not two.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncEngine

    from harborrag_adapters.repositories.database.control_plane.config_store_adapter import (
        SettingOverridesConfigStore,
    )
    from harborrag_core.ports.config_store import ConfigProvider, SettingsStore
    from harborrag_runtime.config.settings import RuntimeSettings


def build_settings_store(engine: AsyncEngine) -> SettingsStore:
    """Return the settings store over ``engine``."""
    return _build(engine)


def _build(engine: AsyncEngine) -> SettingOverridesConfigStore:
    from harborrag_adapters.repositories.database.control_plane.config_store_adapter import (
        SettingOverridesConfigStore,
    )
    from harborrag_adapters.repositories.database.control_plane.engine import (
        create_session_factory,
    )
    from harborrag_adapters.repositories.database.control_plane.setting_overrides import (
        SqlSettingOverrideRepository,
    )
    from harborrag_adapters.repositories.database.control_plane.setting_scope_revisions import (
        SqlSettingScopeRevisionRepository,
    )

    sessions = create_session_factory(engine)
    return SettingOverridesConfigStore(
        overrides=SqlSettingOverrideRepository(sessions),
        revisions=SqlSettingScopeRevisionRepository(sessions),
    )


def build_config_provider(
    settings: RuntimeSettings, *, engine: AsyncEngine, store: SettingsStore | None = None
) -> ConfigProvider:
    """Build the provider.

    Args:
        settings: Unused; kept so existing callers do not need updating.
        engine: Control-plane engine the store reads from when ``store`` is not given.
        store: Settings store to read; defaults to one over ``engine``.

    Returns:
        A ``ConfigProvider`` ready to use immediately (nothing to start: every
        read goes straight to Postgres, there is no cache to warm).
    """
    del settings
    return store if store is not None else _build(engine)  # type: ignore[return-value]
