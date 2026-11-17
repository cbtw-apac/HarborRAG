"""Runtime setting overrides: read raw/effective values at a scope, patch them with an ETag.

Handles:
- Parsing the `level` query param into `(scope_level, scope_id)`
- `resolve_effective`: default -> tenant -> project -> connection, with origin
- `patch`: validate every key against `SETTING_REGISTRY`, bump the scope revision,
  write or delete each override

Does NOT speak HTTP; routes translate these results and errors.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, Literal, TypedDict

from harborrag_app.api.errors import ReferenceNotFoundError, ReindexRequiredError
from harborrag_core.contracts.errors import HarborValidationError
from harborrag_core.domain.setting_override import SettingOverride
from harborrag_core.domain.setting_registry import (
    SETTING_REGISTRY,
    ApplyMode,
    ScopeLevel,
    get_setting_spec,
)
from harborrag_core.ports.control_plane import (
    SettingOverrideRepositoryPort,
    SettingScopeRevisionRepositoryPort,
    SettingsPatchRepositoryPort,
)
from harborrag_runtime.composition import ControlPlaneRepositories
from harborrag_runtime.ingestion.durations import require_activity_timeout_seconds

_PROJECT_ID_PATTERN = re.compile(r"^[a-z0-9][a-z0-9_-]{0,62}$")
_CONNECTION_ID_PATTERN = re.compile(r"^[a-z0-9][a-z0-9_-]{0,62}$")


def _check_activity_timeout(value: Any) -> None:
    """``ingestion.activity_timeout`` is a `string` in the registry, but only a
    duration inside Temporal's activity-timeout bounds is actually usable.

    Rejecting it here, instead of only when `_resolved_workflow_options`
    rebuilds Temporal options for a run, keeps a bad override from being
    saved and then failing every later submission on that scope.
    """
    try:
        require_activity_timeout_seconds(str(value))
    except ValueError as exc:
        raise HarborValidationError(
            str(exc), details={"field": "ingestion.activity_timeout"}
        ) from exc


def _no_extra_check(value: Any) -> None:
    del value


# Keyed validators beyond what SettingSpec.validate_value's generic type check
# covers, for keys whose on-disk shape (e.g. `string`) is looser than the
# value's real-world constraints.
_EXTRA_VALUE_CHECKS: dict[str, Callable[[Any], None]] = {
    "ingestion.activity_timeout": _check_activity_timeout,
}


class EffectiveValue(TypedDict):
    """One key's resolved value plus where it came from."""

    value: Any
    origin: Literal["default", "tenant", "project", "connection"]


@dataclass(slots=True)
class SettingsReadResult:
    """`GET /v1/admin/settings` response body, before the ETag header."""

    level: str
    revision: int
    values: dict[str, Any] | dict[str, EffectiveValue]


@dataclass(slots=True)
class SettingsChange:
    """One key's before/after in a `PATCH /v1/admin/settings` response."""

    key: str
    before: Any
    after: Any
    applies: ApplyMode


@dataclass(slots=True)
class SettingsChangeResult:
    """`PATCH /v1/admin/settings` response body."""

    revision: int
    dry_run: bool
    changed: list[SettingsChange]


def parse_level(level: str) -> tuple[ScopeLevel, str]:
    """Parse `tenant`, `project:{id}` or `connection:{project_id}/{connection_id}`.

    The `tenant` level has no id of its own in the query string; its `scope_id`
    here is a placeholder that every call site replaces with the real
    `tenant_id` via `_scope_id_for_tenant`.

    Raises:
        HarborValidationError: `level` does not match one of the three shapes.
    """
    if level == "tenant":
        return "tenant", "tenant"
    if level.startswith("project:"):
        project_id = level.removeprefix("project:")
        if not _PROJECT_ID_PATTERN.fullmatch(project_id):
            raise HarborValidationError(
                f"invalid project id in level: {project_id!r}", details={"field": "level"}
            )
        return "project", project_id
    if level.startswith("connection:"):
        rest = level.removeprefix("connection:")
        project_id, _, connection_id = rest.partition("/")
        if not (
            _PROJECT_ID_PATTERN.fullmatch(project_id)
            and _CONNECTION_ID_PATTERN.fullmatch(connection_id)
        ):
            raise HarborValidationError(
                f"invalid connection id in level: {rest!r}", details={"field": "level"}
            )
        return "connection", f"{project_id}/{connection_id}"
    raise HarborValidationError(
        "level must be 'tenant', 'project:{id}' or 'connection:{project_id}/{connection_id}'",
        details={"field": "level"},
    )


def _scope_id_for_tenant(tenant_id: str, scope_level: ScopeLevel, scope_id: str) -> str:
    """The row-level `scope_id` stored for `scope_level`; `tenant` rows key on `tenant_id`."""
    return tenant_id if scope_level == "tenant" else scope_id


def _ancestor_chain(
    tenant_id: str, scope_level: ScopeLevel, scope_id: str
) -> list[tuple[ScopeLevel, str]]:
    """`[(level, scope_id), ...]` from `tenant` down to `scope_level`, inclusive."""
    chain: list[tuple[ScopeLevel, str]] = [("tenant", tenant_id)]
    if scope_level == "tenant":
        return chain
    if scope_level == "project":
        chain.append(("project", scope_id))
        return chain
    project_id, _, _connection_id = scope_id.partition("/")
    chain.append(("project", project_id))
    chain.append(("connection", scope_id))
    return chain


@dataclass(slots=True)
class SettingsService:
    """Use cases behind `/v1/admin/settings`."""

    control_plane: Callable[[], ControlPlaneRepositories]

    def _overrides(self) -> SettingOverrideRepositoryPort:
        port = self.control_plane().setting_overrides
        assert port is not None, "composition did not wire SettingOverrideRepositoryPort"
        return port

    def _revisions(self) -> SettingScopeRevisionRepositoryPort:
        port = self.control_plane().setting_scope_revisions
        assert port is not None, "composition did not wire SettingScopeRevisionRepositoryPort"
        return port

    def _patches(self) -> SettingsPatchRepositoryPort:
        port = self.control_plane().setting_patches
        assert port is not None, "composition did not wire SettingsPatchRepositoryPort"
        return port

    async def _ensure_scope_exists(
        self, tenant_id: str, scope_level: ScopeLevel, scope_id: str
    ) -> None:
        """Confirm the project or connection a `level` names is real and visible.

        Raises:
            ReferenceNotFoundError: `scope_level` is `project` or `connection` and
                no such row exists for `tenant_id`.
        """
        if scope_level == "tenant":
            return
        cp = self.control_plane()
        tenant_ids = frozenset({tenant_id})
        if scope_level == "project":
            if await cp.projects.get(scope_id, tenant_ids=tenant_ids) is None:
                raise ReferenceNotFoundError(f"no such project: {scope_id!r}")
            return
        project_id, _, connection_id = scope_id.partition("/")
        if (
            await cp.connections.get(
                tenant_id=tenant_id, project_id=project_id, connection_id=connection_id
            )
            is None
        ):
            raise ReferenceNotFoundError(f"no such connection: {scope_id!r}")

    async def read(
        self, tenant_id: str, scope_level: ScopeLevel, scope_id: str, *, effective: bool
    ) -> SettingsReadResult:
        """Raw overrides at exactly this scope, or every registry key's effective value.

        Raises:
            ReferenceNotFoundError: `scope_level` names a project or connection
                that does not exist for `tenant_id`.
        """
        await self._ensure_scope_exists(tenant_id, scope_level, scope_id)
        overrides_repo = self._overrides()
        row_scope_id = _scope_id_for_tenant(tenant_id, scope_level, scope_id)
        revision = await self._revisions().get(tenant_id, scope_level, row_scope_id)
        level = "tenant" if scope_level == "tenant" else f"{scope_level}:{scope_id}"
        if not effective:
            raw = await overrides_repo.get_at(tenant_id, scope_level, row_scope_id)
            return SettingsReadResult(level=level, revision=revision, values=raw)

        overrides = await overrides_repo.list_for_tenant(tenant_id)
        index = {(o.scope_level, o.scope_id, o.key): o.value for o in overrides}
        chain = _ancestor_chain(tenant_id, scope_level, scope_id)
        resolved: dict[str, EffectiveValue] = {}
        for key, spec in SETTING_REGISTRY.items():
            value: Any = spec.default
            origin: Literal["default", "tenant", "project", "connection"] = "default"
            for level_name, sid in chain:
                row_sid = _scope_id_for_tenant(tenant_id, level_name, sid)
                found = index.get((level_name, row_sid, key))
                if found is not None:
                    value = found
                    origin = level_name
            resolved[key] = {"value": value, "origin": origin}
        return SettingsReadResult(level=level, revision=revision, values=resolved)

    async def patch(
        self,
        tenant_id: str,
        scope_level: ScopeLevel,
        scope_id: str,
        changes: dict[str, Any],
        *,
        expected_revision: int,
        actor: str,
        dry_run: bool,
    ) -> SettingsChangeResult:
        """Validate every key, then (unless `dry_run`) bump the scope revision and write.

        Raises:
            ReferenceNotFoundError: `scope_level` names a project or connection
                that does not exist for `tenant_id`, or a key is not in
                `SETTING_REGISTRY`.
            HarborValidationError: A key is not allowed at `scope_level`, or a value
                fails the key's type/range/enum.
            ReindexRequiredError: A key whose `applies` is `reindex` changed; this
                version does not accept `reindex=true` (lands in v2.1).
            HarborPreconditionFailedError: `expected_revision` does not match the
                scope's stored revision.
        """
        await self._ensure_scope_exists(tenant_id, scope_level, scope_id)
        overrides_repo = self._overrides()
        row_scope_id = _scope_id_for_tenant(tenant_id, scope_level, scope_id)
        current = await overrides_repo.get_at(tenant_id, scope_level, row_scope_id)
        # Ancestors of this scope, excluding it: what a deleted key here falls
        # back to is whatever the nearest ancestor override says, not the
        # registry default.
        ancestor_chain = _ancestor_chain(tenant_id, scope_level, scope_id)[:-1]
        ancestor_index: dict[tuple[ScopeLevel, str, str], Any] | None = None
        planned: list[SettingsChange] = []
        for key, new_value in changes.items():
            try:
                spec = get_setting_spec(key)
            except KeyError as exc:
                raise ReferenceNotFoundError(f"unknown setting key: {key!r}") from exc
            if scope_level not in spec.levels:
                raise HarborValidationError(f"{key} cannot be overridden at level {scope_level!r}")
            before = current.get(key, spec.default)
            if new_value is None:
                if ancestor_index is None:
                    overrides = await overrides_repo.list_for_tenant(tenant_id)
                    ancestor_index = {(o.scope_level, o.scope_id, o.key): o.value for o in overrides}
                after = spec.default
                for level_name, sid in ancestor_chain:
                    row_sid = _scope_id_for_tenant(tenant_id, level_name, sid)
                    found = ancestor_index.get((level_name, row_sid, key))
                    if found is not None:
                        after = found
                if spec.applies == "reindex" and after != before:
                    raise ReindexRequiredError(
                        f"{key} requires reindex=true, not accepted before v2.1"
                    )
                planned.append(SettingsChange(key=key, before=before, after=after, applies=spec.applies))
                continue
            try:
                spec.validate_value(new_value)
            except ValueError as exc:
                raise HarborValidationError(str(exc), details={"field": key}) from exc
            _EXTRA_VALUE_CHECKS.get(key, _no_extra_check)(new_value)
            if spec.applies == "reindex" and new_value != before:
                raise ReindexRequiredError(f"{key} requires reindex=true, not accepted before v2.1")
            planned.append(
                SettingsChange(key=key, before=before, after=new_value, applies=spec.applies)
            )

        if dry_run:
            revision = await self._revisions().get(tenant_id, scope_level, row_scope_id)
            return SettingsChangeResult(revision=revision, dry_run=True, changed=planned)

        deletes = [change.key for change in planned if changes[change.key] is None]
        puts = [
            SettingOverride(
                tenant_id=tenant_id,
                scope_level=scope_level,
                scope_id=row_scope_id,
                key=change.key,
                value=change.after,
                created_by=actor,
                updated_by=actor,
            )
            for change in planned
            if changes[change.key] is not None
        ]
        # One transaction for the revision bump and every override write, so a
        # failure midway rolls back the bump too, and the row lock on the
        # scope's revision covers the whole write -- a concurrent PATCH on the
        # same scope cannot land between the bump and these writes.
        new_revision = await self._patches().apply(
            tenant_id,
            scope_level,
            row_scope_id,
            expected_revision=expected_revision,
            source="api",
            actor=actor,
            puts=puts,
            deletes=deletes,
        )
        return SettingsChangeResult(revision=new_revision, dry_run=False, changed=planned)
