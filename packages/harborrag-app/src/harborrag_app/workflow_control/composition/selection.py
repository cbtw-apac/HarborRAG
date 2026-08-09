"""Select the configured application service without API-only dependencies."""

from __future__ import annotations

from ..ports import BaseAppService


def select_app_service() -> tuple[BaseAppService, str]:
    return runtime_app_service(), "production"


def runtime_app_service() -> BaseAppService:
    from harborrag_runtime.composition import CompositionRoot
    from harborrag_runtime.config.settings import RuntimeSettings

    from .service import AppService

    settings = RuntimeSettings()
    composition = CompositionRoot.production(settings)
    return AppService(composition, settings)


def get_app_service() -> BaseAppService:
    service, _ = select_app_service()
    return service
