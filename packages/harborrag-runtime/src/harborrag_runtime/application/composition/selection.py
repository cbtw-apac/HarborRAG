"""Selection of the configured application service, free of API-only dependencies.

Runtime imports stay inside the functions so importing this module stays cheap.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from ..ports import BaseAppService

if TYPE_CHECKING:
    from harborrag_runtime.config.settings import RuntimeSettings

logger = logging.getLogger("harborrag.app.workflow_control.composition.selection")


def select_app_service() -> tuple[BaseAppService, str]:
    """Return the configured application service and the name of its composition mode."""
    return runtime_app_service(), "production"


def runtime_app_service() -> BaseAppService:
    """Build the production application service from the process settings."""
    from harborrag_runtime.composition import CompositionRoot
    from harborrag_runtime.config.settings import RuntimeSettings

    return _app_service_from(RuntimeSettings())


def get_app_service() -> BaseAppService:
    """Return the configured application service."""
    service, _ = select_app_service()
    return service


def _app_service_from(settings: RuntimeSettings) -> BaseAppService:
    """Build the production composition and the app service on top of it."""
    from harborrag_runtime.composition import CompositionRoot

    from .service import AppService

    composition = CompositionRoot.production(settings)
    return AppService(composition, settings)
