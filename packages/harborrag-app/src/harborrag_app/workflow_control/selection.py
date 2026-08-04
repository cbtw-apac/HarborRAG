"""Select application services without importing API-only dependencies."""

from __future__ import annotations

import os

from .ports import BaseAppService


def select_app_service() -> tuple[BaseAppService, str]:
    env = os.getenv("HARBORRAG_ENV", "dev")
    control_db_url = os.getenv("HARBORRAG_CONTROL_DB_URL", "")
    if env == "dev" and not control_db_url:
        return development_app_service(), "development"
    return runtime_app_service(), "production"


def development_app_service() -> BaseAppService:
    """Build a real AppService without provisioning a control-plane database.

    Control-plane reads (projects/sources/activity/settings/metrics) are
    backed by in-memory fakes rather than left unconfigured, so the read
    routes return real (if empty) data in dev mode instead of a 503.
    """

    from harborrag_app.workflow_control.mock import mock_app_service

    return mock_app_service()


def runtime_app_service() -> BaseAppService:
    from harborrag_app.workflow_control.client import AppService
    from harborrag_runtime.composition import CompositionRoot
    from harborrag_runtime.config.settings import RuntimeSettings

    settings = RuntimeSettings()
    composition = CompositionRoot.production(settings)
    return AppService(composition, settings)


def get_app_service() -> BaseAppService:
    service, _ = select_app_service()
    return service
