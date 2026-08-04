"""Request-scoped access to the application service (ML1/M1).

The lifespan composes one AppService at startup (api/app.py:_lifespan) and
hangs it off ``app.state``; routes should depend on it through here rather
than reaching into ``request.app.state.app_service`` directly, so tests can
swap the implementation via ``app.dependency_overrides`` instead of poking
state after the TestClient has already started.
"""

from __future__ import annotations

from typing import cast

from fastapi import Request

from harborrag_app.workflow_control import BaseAppService


def get_app_service(request: Request) -> BaseAppService:
    """The AppService the lifespan composed once at startup."""
    return cast(BaseAppService, request.app.state.app_service)
