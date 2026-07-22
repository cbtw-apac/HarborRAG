"""Select application services without importing API-only dependencies."""

from __future__ import annotations

import os

from harborrag_app.services.base import BaseAppService
from harborrag_app.services.mock import MockAppService


def select_app_service() -> tuple[BaseAppService, str]:
    env = os.getenv("HARBORRAG_ENV", "dev")
    control_db_url = os.getenv("HARBORRAG_CONTROL_DB_URL", "")
    if env == "dev" and not control_db_url:
        return MockAppService(), "mock"
    return runtime_app_service(), "production"


def runtime_app_service() -> BaseAppService:
    from harborrag_runtime.composition import CompositionRoot
    from harborrag_runtime.config.settings import RuntimeSettings

    from harborrag_app.services.app_service import AppService

    settings = RuntimeSettings()
    composition = CompositionRoot(
        control_db={
            "ping": "ok",
            "scheme": "temporal-client",
        }
    )
    return AppService(composition, settings)


def get_app_service() -> BaseAppService:
    service, _ = select_app_service()
    return service
