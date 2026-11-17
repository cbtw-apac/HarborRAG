"""Runtime settings: registry, levels, effective values and change planning.

Pure domain layer; storage and caching are ports in ``harborrag_core.ports.config_store``.

Start from: SettingsRegistry, SettingsLevel, resolve_effective, plan_changes.
"""

from harborrag_core.config_store.levels import LevelKind, SettingsLevel
from harborrag_core.config_store.registry import (
    BOOT_ONLY_PREFIXES,
    SETTINGS_GROUPS,
    Applies,
    SettingDefinition,
    SettingsRegistry,
    is_boot_only,
)
from harborrag_core.config_store.resolution import (
    EffectiveSettings,
    EffectiveValue,
    Origin,
    RevisionSource,
    SettingChange,
    SettingsReindexRequiredError,
    SettingsRevision,
    SettingsSnapshot,
    plan_changes,
    resolve_effective,
    revision_label,
)

__all__ = [
    "BOOT_ONLY_PREFIXES",
    "SETTINGS_GROUPS",
    "Applies",
    "EffectiveSettings",
    "EffectiveValue",
    "LevelKind",
    "Origin",
    "RevisionSource",
    "SettingChange",
    "SettingDefinition",
    "SettingsLevel",
    "SettingsReindexRequiredError",
    "SettingsRegistry",
    "SettingsRevision",
    "SettingsSnapshot",
    "is_boot_only",
    "plan_changes",
    "resolve_effective",
    "revision_label",
]
