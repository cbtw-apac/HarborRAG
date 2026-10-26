"""Explicit trusted-operator mode and resolved ACL administration; no model I/O."""

from harborrag_core.topology.config import TenantIndexingConfig
from harborrag_core.topology.permissions import ResolvedPermissionSnapshot
from harborrag_runtime.config.settings import RuntimeSettings

from .composition import connect_topology_authority


async def configure_indexing(
    settings: RuntimeSettings, config: TenantIndexingConfig
) -> dict[str, object]:
    async with connect_topology_authority(settings) as control:
        state = await control.topology.configure_indexing(config)
        count = await control.topology.reconcile(config.tenant_id)
    return {"state": state.model_dump(mode="json"), "enqueued": count}


async def indexing_status(settings: RuntimeSettings, tenant_id: str) -> dict[str, object]:
    async with connect_topology_authority(settings) as control:
        state = await control.topology.get_indexing(tenant_id)
    return state.model_dump(mode="json")


async def permission_coverage(settings: RuntimeSettings, tenant_id: str) -> dict[str, object]:
    """Return aggregate active-corpus ACL coverage without source or principal identifiers."""

    async with connect_topology_authority(settings) as control:
        report = await control.topology.permission_coverage(tenant_id)
    return report.model_dump(mode="json")


async def import_permissions(
    settings: RuntimeSettings, snapshot: ResolvedPermissionSnapshot
) -> dict[str, object]:
    async with connect_topology_authority(settings) as control:
        await control.topology.set_permissions(snapshot)
        count = await control.topology.reconcile(snapshot.tenant_id)
    return {
        "resource_kind": snapshot.resource_kind,
        "resource_id": snapshot.resource_id,
        "revision": snapshot.revision,
        "enqueued": count,
    }
