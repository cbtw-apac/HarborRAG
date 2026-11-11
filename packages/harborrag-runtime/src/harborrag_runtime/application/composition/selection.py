"""Select the configured application service without API-only dependencies."""

from __future__ import annotations

from ..ports import BaseAppService


def select_app_service() -> tuple[BaseAppService, str]:
    """Return the configured application service and the name of its composition mode."""

    return runtime_app_service(), "production"


def runtime_app_service() -> BaseAppService:
    """Build the production application service from the process settings."""

    from harborrag_runtime.composition import CompositionRoot
    from harborrag_runtime.config.settings import RuntimeSettings

    from .service import AppService

    settings = RuntimeSettings()
    composition = CompositionRoot.production(settings)
    return AppService(composition, settings)


def get_app_service() -> BaseAppService:
    """Return the configured application service."""

    service, _ = select_app_service()
    return service
