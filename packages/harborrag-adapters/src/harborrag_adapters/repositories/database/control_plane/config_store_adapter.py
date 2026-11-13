"""``config_store`` ports over ``setting_overrides``/``setting_scope_revisions``.

Chat, the MCP server, and ingestion's ``RunSettingsResolver`` all read settings
through ``harborrag_core.ports.config_store.ConfigProvider``. This adapter
backs that port (and ``SettingsStore``, kept only for the two callers that
reuse a pre-built store) with this branch's existing, per-scope-revisioned
``setting_overrides``/``setting_scope_revisions`` tables instead of a second,
tenant-wide-revision store — so ``/v1/admin/settings`` and every other reader
agree on one history.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any, Literal

from harborrag_core.config_store import (
    EffectiveSettings,
    EffectiveValue,
    RevisionSource,
    SettingChange,
    SettingsLevel,
    SettingsRevision,
    SettingsSnapshot,
)
from harborrag_core.domain.setting_registry import SETTING_REGISTRY, ScopeLevel
from harborrag_core.ports.control_plane import (
    SettingOverrideRepositoryPort,
    SettingScopeRevisionRepositoryPort,
)


def _scope_level_and_id(tenant_id: str, level: SettingsLevel) -> tuple[ScopeLevel, str]:
    """Map config_store's ``SettingsLevel`` to this branch's ``(scope_level, scope_id)``."""
    if level.kind == "tenant":
        return "tenant", tenant_id
    if level.kind == "project":
        assert level.project_id is not None
        return "project", level.project_id
    assert level.project_id is not None and level.connection_id is not None
    return "connection", f"{level.project_id}/{level.connection_id}"


def _row_scope_id(tenant_id: str, scope_level: ScopeLevel, scope_id: str) -> str:
    """The row-level ``scope_id`` stored for ``scope_level``; ``tenant`` rows key on ``tenant_id``."""
    return tenant_id if scope_level == "tenant" else scope_id


def _ancestor_chain(
    tenant_id: str, scope_level: ScopeLevel, scope_id: str
) -> list[tuple[ScopeLevel, str]]:
    """``[(level, scope_id), ...]`` from ``tenant`` down to ``scope_level``, inclusive."""
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
class SettingOverridesConfigStore:
    """``SettingsStore`` + ``ConfigProvider`` over ``setting_overrides``/``setting_scope_revisions``."""

    overrides: SettingOverrideRepositoryPort
    revisions: SettingScopeRevisionRepositoryPort

    # --- ConfigProvider ----------------------------------------------------

    async def effective(self, tenant_id: str, level: SettingsLevel) -> EffectiveSettings:
        """Resolve every registry key at ``level`` through tenant -> project -> connection."""
        scope_level, scope_id = _scope_level_and_id(tenant_id, level)
        row_scope_id = _row_scope_id(tenant_id, scope_level, scope_id)
        revision = await self.revisions.get(tenant_id, scope_level, row_scope_id)
        all_overrides = await self.overrides.list_for_tenant(tenant_id)
        index = {(o.scope_level, o.scope_id, o.key): o.value for o in all_overrides}
        chain = _ancestor_chain(tenant_id, scope_level, scope_id)
        values: dict[str, EffectiveValue] = {}
        for key, spec in SETTING_REGISTRY.items():
            value: Any = spec.default
            origin: Literal["default", "tenant", "project", "connection"] = "default"
            for level_name, sid in chain:
                row_sid = _row_scope_id(tenant_id, level_name, sid)
                found = index.get((level_name, row_sid, key))
                if found is not None:
                    value = found
                    origin = level_name
            values[key] = EffectiveValue(value=value, origin=origin)
        return EffectiveSettings(revision=revision, values=values)

    async def snapshot(self, tenant_id: str) -> SettingsSnapshot:
        """Every raw override of ``tenant_id``, grouped by level label."""
        overrides = await self.overrides.list_for_tenant(tenant_id)
        by_level: dict[str, dict[str, object]] = {}
        for override in overrides:
            label = (
                "tenant"
                if override.scope_level == "tenant"
                else f"{override.scope_level}:{override.scope_id}"
            )
            by_level.setdefault(label, {})[override.key] = override.value
        revision = await self.revisions.get(tenant_id, "tenant", tenant_id)
        return SettingsSnapshot(tenant_id=tenant_id, revision=revision, overrides=by_level)

    def invalidate(self, tenant_id: str | None = None) -> None:
        """No-op: every read goes straight to Postgres, nothing here is cached."""

    async def start(self) -> None:
        """No-op: there is no cache to warm and no change feed to subscribe to."""

    async def aclose(self) -> None:
        """No-op: this adapter holds no resources of its own beyond the shared engine."""

    # --- SettingsStore -------------------------------------------------
    # Kept only so `ControlPlaneRepositories.config_store`'s type still holds
    # and the two callers that pass it straight back into
    # `build_config_provider(store=...)` keep working. Nothing in the active
    # path calls these directly any more -- reads go through `effective()`
    # above, writes go through `SettingsService.patch()`.

    async def load(self, tenant_id: str) -> SettingsSnapshot:
        return await self.snapshot(tenant_id)

    async def latest_revision(self, tenant_id: str) -> int:
        return await self.revisions.get(tenant_id, "tenant", tenant_id)

    async def revision(self, tenant_id: str, revision: int) -> SettingsRevision | None:
        """This store keeps the current value per scope, not a replayable change log."""
        return None

    async def write(
        self,
        tenant_id: str,
        changes: Sequence[SettingChange],
        *,
        expected_revision: int,
        source: RevisionSource,
        created_by: str,
    ) -> int:
        raise NotImplementedError(
            "writes go through SettingsService.patch(), not the config_store SettingsStore port"
        )

    async def ping(self) -> None:
        """Raise when the store is unreachable."""
        await self.revisions.get("__ping__", "tenant", "__ping__")
