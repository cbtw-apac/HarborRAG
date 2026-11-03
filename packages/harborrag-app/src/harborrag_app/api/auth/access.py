"""Route guards that run credential, tenant and role checks in that order.

Handles:
- ``require_role``: the one dependency every ``/v1`` route declares for its lowest role
- ``minimum_role``: reading that role back from a route, for OpenAPI and the contract test

Does NOT verify credentials or pick tenants itself; it composes ``get_principal`` and the
tenancy dependencies, so a failed check never reaches the handler.
"""

from collections.abc import Callable, Iterator, Sequence
from functools import cache
from typing import Annotated, Any

from fastapi import Depends, Request, routing
from fastapi.dependencies.models import Dependant
from fastapi.routing import APIRoute
from starlette.routing import BaseRoute

from harborrag_core.domain.member import Role

from ..rate_limit import RateBucket, charge
from .dependencies import authorize_role, get_principal
from .principal import ROLE_ORDER, Principal
from .tenancy import resolve_tenant, tenant_header

_MINIMUM_ROLE = "minimum_role"


@cache
def require_role(
    minimum: Role, *, tenant: bool = True, bucket: RateBucket = "default"
) -> Callable[..., Any]:
    """Build the guard for a route whose lowest role is ``minimum``.

    The guard verifies the credential, then the tenant, then the role, and only then
    charges the call to the principal's rate bucket. With ``tenant=False`` an
    ``X-Tenant-Id`` the credential does not hold is still refused, but a credential with
    several tenants may omit it, for routes that take their tenant from the request or
    need none.

    Args:
        minimum: The lowest role that may call the route.
        tenant: Whether the route acts in one tenant picked from the credential.
        bucket: The rate bucket the call is charged to.

    Returns:
        A dependency that returns the caller's ``Principal``.
    """
    tenant_check = resolve_tenant if tenant else tenant_header

    async def guard(
        request: Request,
        principal: Annotated[Principal, Depends(get_principal)],
        _tenant: Annotated[str | None, Depends(tenant_check)],
    ) -> Principal:
        """Pass the principal through when its role clears the minimum and its bucket has room."""
        authorize_role(principal, minimum)
        await charge(request, principal, bucket)
        return principal

    setattr(guard, _MINIMUM_ROLE, minimum)
    return guard


def _roles(dependant: Dependant) -> list[Role]:
    """Collect every guard's role in a dependency tree."""
    found: list[Role] = []
    role = getattr(dependant.call, _MINIMUM_ROLE, None)
    if role is not None:
        found.append(role)
    for child in dependant.dependencies:
        found.extend(_roles(child))
    return found


def api_routes(routes: Sequence[BaseRoute]) -> Iterator[Any]:
    """Yield every API route with its full path and its routers' dependencies.

    Args:
        routes: An application's ``routes``.

    Returns:
        Route-like objects with ``path``, ``methods`` and ``dependant``.
    """
    iterate = getattr(routing, "iter_route_contexts", None)
    if iterate is None:
        yield from (route for route in routes if isinstance(route, APIRoute))
        return
    for context in iterate(routes):
        if isinstance(context.original_route, APIRoute):
            yield context


def minimum_role(route: Any) -> Role | None:
    """Return the lowest role that may call ``route``, or ``None`` when it has no guard."""
    roles = _roles(route.dependant)
    return sorted(roles, key=ROLE_ORDER.__getitem__)[-1] if roles else None


__all__ = ["api_routes", "minimum_role", "require_role"]
