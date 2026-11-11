"""The tenants that hold data in the control plane.

Reads across every tenant, so only trusted system callers such as ``harborrag doctor`` use it.
"""

from __future__ import annotations

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
