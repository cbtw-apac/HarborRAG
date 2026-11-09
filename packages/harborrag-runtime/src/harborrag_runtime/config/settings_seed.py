"""Seed `setting_overrides` once from a mounted config.yaml; detect drift after.

Mirrors `config.connectors.importing.import_connectors_once`: a marker recorded
in the single-document workspace settings table gates a one-time import, so a
file that changes after day one is never silently re-applied.
"""

from __future__ import annotations

import hashlib
import logging
from dataclasses import replace
from pathlib import Path
from typing import Any

import yaml

from harborrag_core.domain.setting_override import SYSTEM_ACTOR, SettingOverride
from harborrag_core.ports.control_plane import (
    SettingOverrideRepositoryPort,
    SettingScopeRevisionRepositoryPort,
    SettingsRepositoryPort,
)

logger = logging.getLogger("harborrag.runtime.config.settings_seed")

SEED_MARKER_KEY = "settings_yaml_seed"
DRIFT_EVENT = "config_yaml_drift"


def _file_hash(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _read_settings_map(path: Path) -> dict[str, Any]:
    """The raw `settings:` mapping in `path`, or `{}` if it cannot be parsed."""
    try:
        raw: Any = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except yaml.YAMLError:
        return {}
    if not isinstance(raw, dict):
        return {}
    settings = raw.get("settings", {})
    return settings if isinstance(settings, dict) else {}


def _drifted_keys(path: Path, seeded: dict[str, Any]) -> list[str]:
    """Keys in `path` whose value no longer matches what was originally seeded."""
    current = _read_settings_map(path)
    keys = sorted(set(seeded) | set(current))
    return [key for key in keys if seeded.get(key) != current.get(key)]


def _parse_overrides(path: Path, default_tenant_id: str) -> list[SettingOverride]:
    """Build and validate every tenant-level override declared in `path`.

    Raises:
        ValueError: The YAML is malformed, or `settings` is not a mapping.
        KeyError: A key is not in `SETTING_REGISTRY`.
    """
    raw: Any = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    if not isinstance(raw, dict):
        raise ValueError("config.yaml must be a mapping")
    tenant = raw.get("tenant")
    tenant_id = tenant if isinstance(tenant, str) and tenant else default_tenant_id
    settings = raw.get("settings", {})
    if not isinstance(settings, dict):
        raise ValueError("config.yaml 'settings' must be a mapping of key to value")
    return [
        SettingOverride(
            tenant_id=tenant_id,
            scope_level="tenant",
            scope_id=tenant_id,
            key=key,
            value=value,
            created_by=SYSTEM_ACTOR,
            updated_by=SYSTEM_ACTOR,
        )
        for key, value in settings.items()
    ]


async def seed_setting_overrides_once(
    path: Path,
    *,
    overrides: SettingOverrideRepositoryPort,
    workspace: SettingsRepositoryPort,
    revisions: SettingScopeRevisionRepositoryPort,
    default_tenant_id: str = "DEFAULT",
) -> list[SettingOverride] | None:
    """Seed tenant-level overrides from `path` the first time an install boots with none.

    Runs only when: `path` exists, no earlier seed was recorded, and the
    `setting_overrides` table is completely empty (an operator may have written
    rows by hand before any marker existed; this never overwrites those). A
    seed that fails validation is logged and left for the operator to fix and
    retry on the next boot -- it never writes a partial seed or a marker.

    Once a marker is recorded, later boots never re-read the file. If its
    content changes after that, this logs `config_yaml_drift` instead of
    re-applying it: settings changes after day one go through the settings
    API/CLI, not an edited mounted file.

    Returns:
        The overrides written, or None if seeding did not run.
    """
    if not path.is_file():
        return None
    doc = await workspace.get()
    marker = doc.data.get(SEED_MARKER_KEY)
    current_hash = _file_hash(path)
    if marker is not None:
        if marker.get("hash") != current_hash:
            drifted = _drifted_keys(path, marker.get("settings", {}))
            logger.warning(
                "%s path=%s keys=%s seeded_hash=%s current_hash=%s",
                DRIFT_EVENT,
                path,
                ",".join(drifted) if drifted else "<unparseable>",
                marker.get("hash"),
                current_hash,
            )
        return None
    if await overrides.count_all() > 0:
        return None
    try:
        planned = _parse_overrides(path, default_tenant_id)
    except (ValueError, KeyError) as exc:
        logger.error("Settings seed from %s failed validation: %s", path, exc)
        return None
    for override in planned:
        await overrides.put(override, expected_version=None)
    tenant_id = planned[0].tenant_id if planned else default_tenant_id
    if planned:
        await revisions.bump_if_match(
            tenant_id, "tenant", tenant_id, expected_revision=0, source="seed", actor=None
        )
    await workspace.put(
        replace(
            doc,
            data={
                **doc.data,
                SEED_MARKER_KEY: {
                    "path": str(path),
                    "hash": current_hash,
                    "tenant_id": tenant_id,
                    "settings": {o.key: o.value for o in planned},
                },
            },
        )
    )
    return planned
