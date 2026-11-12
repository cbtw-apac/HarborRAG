"""Read and change runtime settings of one tenant.

Handles the use cases behind ``/v1/admin/settings``: reading the overrides or the
effective values at a level, planning and writing a PATCH under an optimistic
revision check, and publishing the registry's JSON schema. The level text, the role a
level needs and the refusals of a malformed PATCH are decided here, so every transport
applies them the same way.

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
from harborrag_core.contracts.errors import (
    HarborPreconditionFailedError,
    HarborUnavailableError,
    HarborValidationError,
)
from harborrag_core.domain.member import Role, ensure_role
from harborrag_core.ports.config_store import SettingsStore
from harborrag_engine.control_plane.connection_views import ANY_VERSION
from harborrag_runtime.composition import ControlPlaneRepositories

from ..errors import SettingsLevelNotFoundError


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


@dataclass(frozen=True, slots=True)
class SettingsPatch:
    """One PATCH as the caller sent it.

    Args:
        level: ``tenant`` (``None``), ``project:<id>`` or ``connection:<project_id>/<id>``.
        body: Key to new value; ``None`` removes the override.
        expected_revision: Revision the caller read, ``ANY_VERSION`` for any, ``None`` when
            the caller sent none.
        dry_run: Plan only.
        reindex: Whether the caller asked the change to start a reindex.
    """

    level: str | None
    body: Mapping[str, object]
    expected_revision: int | None
    dry_run: bool
    reindex: bool


class SettingsService:
    """Runtime settings use cases of one tenant at a time.

    Args:
        control_plane: Returns the control-plane repositories.
        registry: Registry of the settings keys and their validators.
    """

    def __init__(
        self, control_plane: Callable[[], ControlPlaneRepositories], registry: SettingsRegistry
    ) -> None:
        self._control_plane = control_plane
        self._registry = registry

    async def read(
        self, *, tenant_id: str, level: str | None, effective: bool, role: Role
    ) -> SettingsRead:
        """Read the overrides or the effective values at ``level`` from the store.

        A connection's level needs ``editor``; every other level needs ``admin``.

        Args:
            tenant_id: Tenant to read.
            level: ``tenant`` (``None``), ``project:<id>`` or ``connection:<project_id>/<id>``.
            effective: Resolve every key through the level chain instead of listing overrides.
            role: The caller's role.

        Returns:
            The values and the revision they were read at.

        Raises:
            HarborValidationError: ``level`` is malformed.
            HarborAuthError: ``role`` is below the role ``level`` needs.
            SettingsLevelNotFoundError: The project or connection of ``level`` does not exist.
            HarborUnavailableError: The settings store is not configured.
        """
        parsed = SettingsLevel.parse(level)
        if parsed.kind != "connection":
            ensure_role(role, "admin")
        store = self._store()
        await require_settings_level(self._control_plane(), tenant_id, parsed)
        snapshot = await store.load(tenant_id)
        if effective:
            resolved = resolve_effective(self._registry, snapshot, parsed)
            values: dict[str, Any] = {
                key: {"value": item.value, "origin": item.origin}
                for key, item in resolved.values.items()
            }
        else:
            values = dict(snapshot.at(parsed))
        return SettingsRead(str(parsed), snapshot.revision, values)

    async def patch(self, *, tenant_id: str, request: SettingsPatch, actor: str) -> SettingsPatched:
        """Plan the PATCH's body at its level and save it as the next revision unless it is a dry run.

        Args:
            tenant_id: Tenant to change.
            request: The level, body and preconditions the caller sent.
            actor: Identity recorded in the revision log.

        Returns:
            The revision after the call and the changes.

        Raises:
            HarborValidationError: The level is malformed, ``reindex`` is set, no revision
                was sent, or a key or value of the body is invalid.
            SettingsLevelNotFoundError: The project or connection of the level does not exist.
            HarborPreconditionFailedError: The expected revision is not the current revision.
            SettingsReindexRequiredError: A key of the body needs a reindex.
            HarborUnavailableError: The settings store is not configured.
        """
        level = SettingsLevel.parse(request.level)
        if request.reindex:
            raise HarborValidationError(
                "reindex cannot be started from a settings change", details={"field": "reindex"}
            )
        expected_revision = request.expected_revision
        if expected_revision is None:
            raise HarborValidationError("If-Match is required", details={"field": "If-Match"})
        store = self._store()
        await require_settings_level(self._control_plane(), tenant_id, level)
        snapshot = await store.load(tenant_id)
        if expected_revision not in {ANY_VERSION, snapshot.revision}:
            raise HarborPreconditionFailedError("settings changed since they were read")
        changed = plan_changes(self._registry, snapshot, level, request.body)
        if request.dry_run or not changed:
            return SettingsPatched(snapshot.revision, request.dry_run, changed)
        revision = await self._write(store, tenant_id, changed, snapshot, actor)
        return SettingsPatched(revision, request.dry_run, changed)

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
