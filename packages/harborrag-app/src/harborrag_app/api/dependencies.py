"""Request-scoped access to the application service and the operations port (ML1/M1).

The lifespan composes one AppService and its ``LocalOperations`` at startup
(api/app.py:_lifespan) and hangs them off ``app.state``; routes should depend on
them through here rather than reaching into ``request.app.state`` directly, so tests
can swap the implementation via ``app.dependency_overrides`` instead of poking
state after the TestClient has already started.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Annotated, cast

from fastapi import Depends, Request, Response

from harborrag_app.api.auth.dependencies import get_principal
from harborrag_app.api.auth.tenancy import TenantDependency
from harborrag_app.api.settings import ApiSettings
from harborrag_app.operations.port import Operations
from harborrag_app.operations.principal import Principal
from harborrag_app.operations.scope import Scope
from harborrag_runtime.application import BaseAppService


@dataclass(frozen=True, slots=True)
class ResponseContext:
    """Settings and response a route needs to set headers.

    Args:
        settings: API settings.
        response: Response being built.
    """

    settings: ApiSettings
    response: Response


def response_context(request: Request, response: Response) -> ResponseContext:
    """Server settings and response headers for one HTTP operation."""
    return ResponseContext(request.app.state.settings, response)


ResponseContextDependency = Annotated[ResponseContext, Depends(response_context)]


def get_app_service(request: Request) -> BaseAppService:
    """The AppService the lifespan composed once at startup."""
    return cast(BaseAppService, request.app.state.app_service)


def get_operations(request: Request) -> Operations:
    """Return the operations the lifespan composed over the AppService."""
    operations: Operations = request.app.state.operations
    return operations


def operation_scope(
    principal: Annotated[Principal, Depends(get_principal)], tenant: TenantDependency
) -> Scope:
    """Return the verified caller and the tenant the request acts in."""
    return Scope(principal, tenant)


OperationsDependency = Annotated[Operations, Depends(get_operations)]
ScopeDependency = Annotated[Scope, Depends(operation_scope)]
