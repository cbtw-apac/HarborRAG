"""Ports for runtime settings storage, change notification and cached reads.

``SettingsStore`` is the durable per-tenant revision log; ``SettingsChangeFeed``
tells a process which tenant changed; ``ConfigProvider`` is what runtime code
reads. Implementations live in adapters and runtime.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from typing import Protocol

from harborrag_core.config_store import (
    EffectiveSettings,
    RevisionSource,
    SettingChange,
    SettingsLevel,
    SettingsRevision,
    SettingsSnapshot,
)

SETTINGS_CHANGED_CHANNEL = "settings_changed"


class SettingsStore(Protocol):
    """Durable overrides and revision log of every tenant."""

    async def load(self, tenant_id: str) -> SettingsSnapshot:
        """Return the tenant's current overrides; an empty log is revision 0."""
        ...

    async def latest_revision(self, tenant_id: str) -> int:
        """Return the tenant's latest revision number; 0 for an empty log."""
        ...

    async def revision(self, tenant_id: str, revision: int) -> SettingsRevision | None:
        """Return one logged revision, or ``None`` when it does not exist."""
        ...

    async def write(
        self,
        tenant_id: str,
        changes: Sequence[SettingChange],
        *,
        expected_revision: int,
        source: RevisionSource,
        created_by: str,
    ) -> int:
        """Apply ``changes`` as the next revision and notify other processes.

        Args:
            tenant_id: Tenant to write.
            changes: Planned changes; must not be empty.
            expected_revision: Revision the caller planned against.
            source: Origin of the change.
            created_by: Actor recorded in the log.

        Returns:
            The new revision number.

        Raises:
            HarborPreconditionFailedError: ``expected_revision`` is stale or a concurrent
                write took the next revision.
        """
        ...

    async def ping(self) -> None:
        """Raise when the store is unreachable."""
        ...


class SettingsChangeFeed(Protocol):
    """Source of change notifications that outlives connection loss."""

    async def run(self, on_change: Callable[[str | None], None]) -> None:
        """Call ``on_change(tenant_id)`` per change; ``None`` means every tenant changed."""
        ...


class ConfigProvider(Protocol):
    """Cached read access to settings."""

    async def snapshot(self, tenant_id: str) -> SettingsSnapshot:
        """Return the tenant's overrides."""
        ...

    async def effective(self, tenant_id: str, level: SettingsLevel) -> EffectiveSettings:
        """Return every key resolved at ``level``."""
        ...

    def invalidate(self, tenant_id: str | None = None) -> None:
        """Drop the cached tenant, or every tenant when ``None``."""
        ...
