"""The operations port run in this process, over the composed application service.

The API serves its routes through it; the CLI calls it directly in local mode.

Start from: LocalOperations.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from .admin import AdminOps
from .ingestions import IngestionOps

if TYPE_CHECKING:
    from harborrag_app.workflow_control.composition.service import AppService


class LocalOperations(AdminOps, IngestionOps):
    """The application operations, run in-process against the application service.

    Args:
        app_service: The composed application service the operations call.
    """

    def __init__(self, app_service: AppService) -> None:
        self._app_service = app_service
