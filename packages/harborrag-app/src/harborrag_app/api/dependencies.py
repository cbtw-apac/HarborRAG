"""Request-scoped access to the application service (ML1/M1).

The lifespan composes one AppService at startup (api/app.py:_lifespan) and
hangs it off ``app.state``; routes should depend on it through here rather
than reaching into ``request.app.state.app_service`` directly, so tests can
swap the implementation via ``app.dependency_overrides`` instead of poking
state after the TestClient has already started.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Annotated, cast

from fastapi import Depends, Request, Response

from harborrag_app.api.settings import ApiSettings
from harborrag_app.workflow_control import BaseAppService


@dataclass(frozen=True, slots=True)
class ResponseContext:
    settings: ApiSettings
    response: Response


def response_context(request: Request, response: Response) -> ResponseContext:
    """Server settings and response headers for one HTTP operation."""
    return ResponseContext(request.app.state.settings, response)


ResponseContextDependency = Annotated[ResponseContext, Depends(response_context)]


def get_app_service(request: Request) -> BaseAppService:
    """The AppService the lifespan composed once at startup."""
    return cast(BaseAppService, request.app.state.app_service)
