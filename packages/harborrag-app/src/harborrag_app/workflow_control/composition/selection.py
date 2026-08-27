"""Select the configured application service without API-only dependencies."""

from __future__ import annotations

from ..ports import BaseAppService


def validate_serving_model_config() -> None:
    """Fail the process boot when the serving model catalogue cannot resolve.

    Delegates to the runtime composition layer, which owns the adapter
    configuration types; this wrapper only supplies process settings.
    """

    from harborrag_runtime.chat.composition import validate_serving_model_config as validate
    from harborrag_runtime.config.settings import RuntimeSettings

    validate(RuntimeSettings())


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
