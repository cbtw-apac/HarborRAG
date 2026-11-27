"""The operations port run in this process, over the composed application service.

The API serves its routes through it; the CLI calls it directly in local mode.

Start from: LocalOperations.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from harborrag_app.operations.port import Operations

from .admin import AdminOps
from .chat import ChatOps
from .ingestions import IngestionOps
from .schedules import ScheduleOps
from .settings import SettingsOps

if TYPE_CHECKING:
    from harborrag_runtime.application.composition.service import AppService


class LocalOperations(AdminOps, IngestionOps, ScheduleOps, SettingsOps, ChatOps):
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
        return await self._app_service.tenant_has_data(tenant_id)


_conforms: type[Operations] = LocalOperations
