"""The operations port run in this process, over the composed application service.

The API serves its routes through it; the CLI calls it directly in local mode.

Start from: LocalOperations.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from harborrag_app.operations.port import Operations
from harborrag_core.config_store import SettingsLevel

from .admin import AdminOps
from .chat import ChatOps
from .ingestions import IngestionOps
from .settings import SettingsOps

if TYPE_CHECKING:
    from harborrag_app.workflow_control.composition.service import AppService

_TENANT_LEVEL = SettingsLevel("tenant")


class LocalOperations(AdminOps, IngestionOps, SettingsOps, ChatOps):
    """The application operations, run in-process against the application service.

    Args:
        app_service: The composed application service the operations call.
    """

    def __init__(self, app_service: AppService) -> None:
        self._app_service = app_service

    @property
    def app_service(self) -> AppService:
        """The application service the operations run over.

        Not part of the port: the CLI's in-process commands, such as ``ingestions start
        --inline``, use it directly.
        """
        return self._app_service

    async def tenant_has_data(self, tenant_id: str) -> bool:
        """Whether the tenant holds a project, a connection or a saved settings revision.

        Not an operation of the port: the CLI asks it before the first write into a tenant,
        on the client only, and it checks no role.

        Args:
            tenant_id: Tenant to look into.

        Returns:
            ``False`` only when the tenant has no project, no connection and settings
            revision 0.
        """
        services = self._app_service
        projects, _ = await services.project_service.list_page(tenant_id=tenant_id, limit=1)
        if projects:
            return True
        connections, _ = await services.connection_service.list_page(tenant_id=tenant_id, limit=1)
        if connections:
            return True
        settings = await services.settings_service.read(
            tenant_id=tenant_id, level=_TENANT_LEVEL, effective=False
        )
        return settings.revision > 0


_conforms: type[Operations] = LocalOperations
