"""FastAPI-free application service selection shared with the CLI."""

from harborrag_app.services.selection import get_app_service, select_app_service

from __future__ import annotations

import os

from harborrag_app.services.base import BaseAppService
from harborrag_app.services.mock import MockAppService


def select_app_service() -> tuple[BaseAppService, str]:
    """Return (service, composition_mode) per the env selection rule.

    Production composition runs migrations and a DB probe — callers inside an
    event loop should run this via asyncio.to_thread.
    """
    env = os.getenv("HARBORRAG_ENV", "dev")
    control_db_url = os.getenv("HARBORRAG_CONTROL_DB_URL", "")
    if env == "dev" and not control_db_url:
        return MockAppService(), "mock"

    from harborrag_app.services.app_service import AppService
    from harborrag_runtime.composition import CompositionRoot

    return AppService(CompositionRoot.production()), "production"


def get_app_service() -> BaseAppService:
    """The composed app service (CLI entrypoint; API reads app.state)."""
    service, _ = select_app_service()
    return service
