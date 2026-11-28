"""Bearer-token owner authentication for the local MCP HTTP boundary.

Administering the server is a different permission from reading every tenant's
data. Only the local owner token carries ``ADMIN_SCOPE``, and reader keys never do.
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable, Mapping
from typing import TYPE_CHECKING

from harborrag_mcp_server.server.http_responses import error_response

if TYPE_CHECKING:
    from fastmcp.server.auth import AccessToken, TokenVerifier
    from starlette.requests import Request
    from starlette.responses import Response

logger = logging.getLogger("harborrag.mcp.server.http")

ADMIN_SCOPE = "mcp:admin"

type OwnerHandler = Callable[[Request, str], Awaitable[Response]]


class Unauthorized(Exception):
    """A request lacks the credential or the grant an MCP route requires.

    Args:
        message: What to tell the caller.
        status_code: The HTTP status to answer with, 401 or 403.
    """

    def __init__(self, message: str, *, status_code: int) -> None:
        super().__init__(message)
        self.message = message
        self.status_code = status_code


OWNER_ROLES: frozenset[str] = frozenset({"owner"})
READER_ROLES: frozenset[str] = frozenset({"reader", "owner"})


async def authenticated_principal(
    request: Request, token_verifier: TokenVerifier, *, roles: frozenset[str]
) -> str:
    """Authenticate the bearer and require one of ``roles``; record its grants on the request.

    A reader key (``role: reader``, one tenant grant, no admin scope) may list and
    run tools for its own tenant. Everything that changes the server stays owner-only.

    A verifier that raises is treated as a rejected token, and only the exception
    type is logged. A verifier is free to put the presented token into its message
    or traceback, so logging either would leak the credential.

    Args:
        request: The request to authenticate. Its state receives the grants.
        token_verifier: Checks the presented bearer token.
        roles: The roles allowed on this route.

    Returns:
        The authenticated principal's ID.

    Raises:
        Unauthorized: The token is missing, rejected or lacks an allowed role.
    """
    authorization = request.headers.get("authorization", "")
    scheme, separator, token = authorization.partition(" ")
    if scheme.lower() != "bearer" or not separator or not token.strip():
        raise Unauthorized("missing bearer token", status_code=401)
    try:
        access = await token_verifier.verify_token(token.strip())
    except Exception as exc:  # noqa: BLE001 - a verifier's message or traceback may hold the token
        logger.warning("Bearer token verification raised %s", type(exc).__name__)
        access = None
    if access is None:
        raise Unauthorized("invalid bearer token", status_code=401)
    claims = access.claims or {}
    role = claims.get("role")
    if role not in roles:
        if role == "reader":
            raise Unauthorized(
                "owner role required: a reader key can list and run tools, not change "
                "this server's configuration",
                status_code=403,
            )
        raise Unauthorized("owner role required", status_code=403)
    request.state.token_role = role
    request.state.allowed_tenants = allowed_tenants(claims)
    request.state.token_scopes = frozenset(access.scopes or ())
    request.state.credential_id = credential_id(access)
    subject = claims.get("sub")
    principal_id = subject if isinstance(subject, str) and subject.strip() else access.client_id
    return principal_id or "authenticated-owner"


def credential_id(access: AccessToken) -> str:
    """Return the ID of the credential a token came from, which keys its request budget.

    The client ID names one key, while several keys can share an owner's subject.
    """
    if access.client_id:
        return access.client_id
    subject = (access.claims or {}).get("sub")
    return subject if isinstance(subject, str) and subject.strip() else "authenticated-unknown"


def allowed_tenants(claims: Mapping[str, object]) -> frozenset[str]:
    """Return canonical tenant grants from an authenticated token."""
    raw_tenants = claims.get("tenants")
    if not isinstance(raw_tenants, (list, tuple, set, frozenset)):
        return frozenset()
    return frozenset(
        tenant.strip() for tenant in raw_tenants if isinstance(tenant, str) and tenant.strip()
    )


def authorize_claimed_tenant(claims: Mapping[str, object], tenant_id: str) -> None:
    """Require a concrete tenant grant or an explicit global wildcard."""
    tenant = tenant_id.strip()
    grants = allowed_tenants(claims)
    if "*" not in grants and tenant not in grants:
        raise PermissionError("token is not authorized for the requested tenant")


def _grants(request: Request) -> frozenset[str]:
    """Return the tenant grants ``authenticated_principal`` recorded on the request."""
    return getattr(request.state, "allowed_tenants", frozenset())


def grants_any_tenant(request: Request) -> bool:
    """Whether the token may name any tenant, not only the ones it lists."""
    return "*" in _grants(request)


def granted_tenants(request: Request) -> list[str]:
    """List the tenants the token names, sorted, without the wildcard."""
    return sorted(_grants(request) - {"*"})


def request_tenant_default(request: Request) -> str | None:
    """Return the single tenant a token is bound to, or ``None`` when unbound.

    A token that grants exactly one tenant makes that tenant the implied scope of
    a request naming none, which is how the MCP transport already binds a call.
    """
    grants = _grants(request)
    if "*" in grants or len(grants) != 1:
        return None
    return next(iter(grants))


def authorize_administration(request: Request) -> None:
    """Require administration rights rather than a grant over every tenant.

    ``HARBORRAG_MCP_READER_TENANT_ID`` binds the local owner token to one tenant so
    its *reads* stay inside that corpus. Asking for a wildcard tenant grant here
    read that data scope as an admin demotion and locked the owner out of their own
    configuration with a valid bearer token. Reader keys carry ``mcp:read`` alone
    and are already refused by ``owner_only``, so global state stays owner-only.
    """
    scopes: frozenset[str] = getattr(request.state, "token_scopes", frozenset())
    if ADMIN_SCOPE in scopes or grants_any_tenant(request):
        return
    raise Unauthorized(
        "token is not authorized to administer this server",
        status_code=403,
    )


def authorize_request_tenant(request: Request, tenant_id: str) -> None:
    """Authorize a custom HTTP request against grants set by ``owner_only``."""
    if not grants_any_tenant(request) and tenant_id.strip() not in _grants(request):
        raise Unauthorized(
            "token is not authorized for the requested tenant",
            status_code=403,
        )


def owner_only(
    token_verifier: TokenVerifier,
) -> Callable[[OwnerHandler], Callable[[Request], Awaitable[Response]]]:
    """Authenticate the owner once and translate rejections into JSON responses."""
    return authenticated(token_verifier, roles=OWNER_ROLES)


def reader_or_owner(
    token_verifier: TokenVerifier,
) -> Callable[[OwnerHandler], Callable[[Request], Awaitable[Response]]]:
    """Guard a playground route for reader keys and the owner token alike."""
    return authenticated(token_verifier, roles=READER_ROLES)


def authenticated(
    token_verifier: TokenVerifier, *, roles: frozenset[str]
) -> Callable[[OwnerHandler], Callable[[Request], Awaitable[Response]]]:
    """Build a decorator that authenticates a route and answers rejections as JSON."""

    def decorate(handler: OwnerHandler) -> Callable[[Request], Awaitable[Response]]:
        async def guarded(request: Request) -> Response:
            try:
                principal_id = await authenticated_principal(request, token_verifier, roles=roles)
            except Unauthorized as exc:
                return error_response(
                    exc.message,
                    status_code=exc.status_code,
                    headers=({"WWW-Authenticate": "Bearer"} if exc.status_code == 401 else None),
                )
            try:
                return await handler(request, principal_id)
            except Unauthorized as exc:
                return error_response(exc.message, status_code=exc.status_code)

        return guarded

    return decorate
