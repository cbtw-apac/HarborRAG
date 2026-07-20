"""App-service selection shared by the CLI and the API lifespan (ST8).

Selection rule (plan ST8): HARBORRAG_ENV=dev with no HARBORRAG_CONTROL_DB_URL
-> mock composition; anything else -> CompositionRoot.production(). This
module must stay import-light and fastapi-free so the bare CLI keeps working
without the [api] extra.
"""

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

    from harborrag_runtime.composition import CompositionRoot

    from harborrag_app.services.app_service import AppService

    return AppService(CompositionRoot.production()), "production"


def get_app_service() -> BaseAppService:
    """The composed app service (CLI entrypoint; API reads app.state)."""
    service, _ = select_app_service()
    return service
