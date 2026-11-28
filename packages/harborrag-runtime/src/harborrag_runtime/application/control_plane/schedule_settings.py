"""The tenant's ``schedules.*`` settings, read once for the schedule and connection use cases."""

from __future__ import annotations

from harborrag_core.config_store.levels import SettingsLevel
from harborrag_core.config_store.registry import SchedulesGroup
from harborrag_core.ports.config_store import ConfigProvider


async def read_schedule_settings(config: ConfigProvider | None, tenant_id: str) -> SchedulesGroup:
    """Read the tenant's ``schedules.*`` settings, or their defaults without a store.

    Args:
        config: The settings provider; ``None`` when the deployment has no settings store.
        tenant_id: Tenant whose effective settings to read.

    Returns:
        ``schedules.enabled``, ``schedules.min_interval`` and ``schedules.max_per_connection``.
    """
    if config is None:
        return SchedulesGroup.model_validate({})
    effective = await config.effective(tenant_id, SettingsLevel("tenant"))
    return SchedulesGroup.model_validate(
        {name: effective.value(f"schedules.{name}") for name in SchedulesGroup.model_fields}
    )
