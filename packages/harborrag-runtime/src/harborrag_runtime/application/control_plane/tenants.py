"""Which tenants hold data in the control plane.

``tenants_with_data`` reads across every tenant, so only trusted system callers such as
``harborrag doctor`` use it. ``tenant_has_data`` looks into one tenant and checks no role.
"""

from __future__ import annotations

from harborrag_core.contracts.errors import HarborUnavailableError
from harborrag_runtime.composition import ControlPlaneRepositories


async def tenants_with_data(control_plane: ControlPlaneRepositories) -> list[str]:
    """List the tenants that own a project or a connection, across the whole install.

    Args:
        control_plane: Repositories to read without a tenant scope.

    Returns:
        Each tenant ID once, in code point order.
    """
    projects = await control_plane.projects.list(tenant_ids=None)
    connections, _ = await control_plane.connections.list(tenant_ids=None)
    owners = {project.tenant_id for project in projects}
    owners.update(connection.tenant_id for connection in connections)
    return sorted(owners)


async def tenant_has_data(control_plane: ControlPlaneRepositories, tenant_id: str) -> bool:
    """Whether the tenant holds a project, a connection or a saved settings revision.

    The project repository lists a tenant's projects without a page limit, so that read
    still loads each project row; the connection and settings reads are single-row.

    Args:
        control_plane: Repositories to read.
        tenant_id: Tenant to look into.

    Returns:
        ``False`` only when the tenant has no project, no connection and settings revision 0.

    Raises:
        HarborUnavailableError: No settings store is configured and the tenant has no project
            or connection.
    """
    scope = frozenset({tenant_id})
    if await control_plane.projects.list(tenant_ids=scope):
        return True
    connections, _ = await control_plane.connections.list(tenant_ids=scope, limit=1)
    if connections:
        return True
    store = control_plane.config_store
    if store is None:
        raise HarborUnavailableError("settings store is not configured")
    return await store.latest_revision(tenant_id) > 0
