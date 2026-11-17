"""Read and change runtime settings of one tenant.

Handles the use cases behind ``/v1/admin/settings``: reading the overrides or the
effective values at a level, planning and writing a PATCH under an optimistic
revision check, and publishing the registry's JSON schema.

Does NOT cache or notify other processes; the store and the config provider do.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any

from harborrag_core.config_store import (
    SettingChange,
    SettingsLevel,
    SettingsRegistry,
    SettingsSnapshot,
    plan_changes,
    resolve_effective,
)
from harborrag_core.contracts.errors import HarborPreconditionFailedError, HarborUnavailableError
from harborrag_core.ports.config_store import SettingsStore
from harborrag_runtime.composition import ControlPlaneRepositories

from ..errors import SettingsLevelNotFoundError
from .connection_views import ANY_VERSION


@dataclass(frozen=True, slots=True)
class SettingsRead:
    """Settings at one level.

    Args:
        level: Level label.
        revision: Tenant revision the values were read at.
        values: Overrides by key, or ``{value, origin}`` by key when effective.
    """

    level: str
    revision: int
    values: dict[str, Any]


@dataclass(frozen=True, slots=True)
class SettingsPatched:
    """Outcome of a PATCH.

    Args:
        revision: New revision, or the current one for a dry run or an empty change.
        dry_run: Nothing was saved because the caller asked for a preview.
        changed: Planned or applied changes.
    """

    revision: int
    dry_run: bool
    changed: tuple[SettingChange, ...]


class SettingsService:
    """Runtime settings use cases of one tenant at a time."""

    def __init__(
        self, control_plane: Callable[[], ControlPlaneRepositories], registry: SettingsRegistry
    ) -> None:
        self._control_plane = control_plane
        self._registry = registry

    async def read(self, *, tenant_id: str, level: SettingsLevel, effective: bool) -> SettingsRead:
        """Read the overrides or the effective values at ``level`` from the store.

        Args:
            tenant_id: Tenant to read.
            level: Level to read.
            effective: Resolve every key through the level chain instead of listing overrides.

        Returns:
            The values and the revision they were read at.

        Raises:
            SettingsLevelNotFoundError: The project or connection of ``level`` does not exist.
            HarborUnavailableError: The settings store is not configured.
        """
        store = self._store()
        await require_settings_level(self._control_plane(), tenant_id, level)
        snapshot = await store.load(tenant_id)
        if effective:
            resolved = resolve_effective(self._registry, snapshot, level)
            values: dict[str, Any] = {
                key: {"value": item.value, "origin": item.origin}
                for key, item in resolved.values.items()
            }
        else:
            values = dict(snapshot.at(level))
        return SettingsRead(str(level), snapshot.revision, values)

    async def patch(  # noqa: PLR0913 - one tenant, level, body, precondition and actor
        self,
        *,
        tenant_id: str,
        level: SettingsLevel,
        body: Mapping[str, object],
        expected_revision: int,
        dry_run: bool,
        actor: str,
    ) -> SettingsPatched:
        """Plan ``body`` at ``level`` and save it as the next revision unless ``dry_run``.

        Args:
            tenant_id: Tenant to change.
            level: Level the body applies to.
            body: Key to new value; ``None`` removes the override.
            expected_revision: Revision the caller read, or ``ANY_VERSION`` for the current one.
            dry_run: Plan only.
            actor: Identity recorded in the revision log.

        Returns:
            The revision after the call and the changes.

        Raises:
            SettingsLevelNotFoundError: The project or connection of ``level`` does not exist.
            HarborPreconditionFailedError: ``expected_revision`` is not the current revision.
            HarborValidationError: A key or value of ``body`` is invalid.
            SettingsReindexRequiredError: A key of ``body`` needs a reindex.
            HarborUnavailableError: The settings store is not configured.
        """
        store = self._store()
        await require_settings_level(self._control_plane(), tenant_id, level)
        snapshot = await store.load(tenant_id)
        if expected_revision not in {ANY_VERSION, snapshot.revision}:
            raise HarborPreconditionFailedError("settings changed since they were read")
        changed = plan_changes(self._registry, snapshot, level, body)
        if dry_run or not changed:
            return SettingsPatched(snapshot.revision, dry_run, changed)
        revision = await self._write(store, tenant_id, changed, snapshot, actor)
        return SettingsPatched(revision, dry_run, changed)

    def schema(self) -> dict[str, Any]:
        """Return the JSON schema of every registry key."""
        return self._registry.json_schema()

    async def _write(
        self,
        store: SettingsStore,
        tenant_id: str,
        changed: tuple[SettingChange, ...],
        snapshot: SettingsSnapshot,
        actor: str,
    ) -> int:
        try:
            return await store.write(
                tenant_id,
                changed,
                expected_revision=snapshot.revision,
                source="api",
                created_by=actor,
            )
        finally:
            config = self._control_plane().config
            if config is not None:
                config.invalidate(tenant_id)

    def _store(self) -> SettingsStore:
        store = self._control_plane().config_store
        if store is None:
            raise HarborUnavailableError("settings store is not configured")
        return store


async def require_settings_level(
    control_plane: ControlPlaneRepositories, tenant_id: str, level: SettingsLevel
) -> None:
    """Raise unless the project or connection of ``level`` exists in the tenant.

    Raises:
        SettingsLevelNotFoundError: The project or connection does not exist.
    """
    if level.kind == "tenant":
        return
    project_id = level.project_id or ""
    if level.kind == "project":
        exists = (
            await control_plane.projects.get(project_id, tenant_ids=frozenset({tenant_id}))
            is not None
        )
    else:
        exists = (
            await control_plane.connections.get(
                tenant_id=tenant_id,
                project_id=project_id,
                connection_id=level.connection_id or "",
            )
            is not None
        )
    if not exists:
        raise SettingsLevelNotFoundError(f"{level} does not exist", details={"field": "level"})
