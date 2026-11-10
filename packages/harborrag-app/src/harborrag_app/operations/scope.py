"""Who is acting and which connection they name, independent of the transport.

Handles:
- ``Scope``: the principal and tenant an operation runs for, with the role and tenant check
- ``ConnectionRef``: the ``project_id/connection_id`` text a CLI user types

Does NOT verify credentials; the transport builds the ``Principal`` before an operation runs.
"""

from __future__ import annotations

from dataclasses import dataclass

from harborrag_app.api.auth.principal import Principal, authorize_role
from harborrag_app.operations.errors import TenantForbiddenError
from harborrag_app.workflow_control.control_plane.connections import ConnectionKey
from harborrag_core.domain.member import Role


@dataclass(frozen=True, slots=True)
class Scope:
    """The principal and tenant one operation acts for.

    Args:
        principal: Authenticated caller.
        tenant_id: Tenant the operation reads and writes.
    """

    principal: Principal
    tenant_id: str

    def require(self, minimum: Role) -> None:
        """Refuse a caller below ``minimum`` or one that does not hold the tenant.

        Raises:
            HarborAuthError: The principal's role is below ``minimum``.
            TenantForbiddenError: The principal does not hold ``tenant_id``.
        """
        authorize_role(self.principal, minimum)
        if not self.principal.can_access_tenant(self.tenant_id):
            raise TenantForbiddenError()

    @property
    def actor(self) -> str:
        """Subject recorded as the author of a change."""
        return self.principal.subject

    @property
    def actor_type(self) -> str:
        """``api_key`` or ``user``."""
        return self.principal.kind


@dataclass(frozen=True, slots=True)
class ConnectionRef:
    """A connection named as ``project_id/connection_id``.

    Args:
        project_id: Project that owns the connection.
        connection_id: Connection within the project.
    """

    project_id: str
    connection_id: str

    @classmethod
    def parse(cls, value: str) -> ConnectionRef:
        """Split ``project_id/connection_id``.

        Raises:
            ValueError: ``value`` does not hold exactly one ``/`` between two non-blank parts.
        """
        project_id, separator, connection_id = value.partition("/")
        if not separator or "/" in connection_id:
            raise ValueError("a connection is named project_id/connection_id")
        project_id, connection_id = project_id.strip(), connection_id.strip()
        if not project_id or not connection_id:
            raise ValueError("a connection is named project_id/connection_id")
        return cls(project_id, connection_id)

    def key(self, tenant_id: str) -> ConnectionKey:
        """The connection's identity inside ``tenant_id``."""
        return ConnectionKey(tenant_id, self.project_id, self.connection_id)

    def __str__(self) -> str:
        return f"{self.project_id}/{self.connection_id}"
