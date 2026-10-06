"""Bearer-token owner authentication for the local MCP HTTP boundary."""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable, Mapping
from typing import TYPE_CHECKING

from harborrag_mcp_server.server.http_responses import error_response

if TYPE_CHECKING:
    from fastmcp.server.auth import TokenVerifier
    from starlette.requests import Request
    from starlette.responses import Response

logger = logging.getLogger("harborrag.mcp.server.http")

# Administering this server is a different permission from reading every tenant's
# data. Only the local owner token carries it; hashed reader keys never do.
ADMIN_SCOPE = "mcp:admin"

type OwnerHandler = Callable[[Request, str], Awaitable[Response]]


class Unauthorized(Exception):
    """Reject a request that is not an authenticated MCP owner."""

    def __init__(self, message: str, *, status_code: int) -> None:
        super().__init__(message)
        self.message = message
        self.status_code = status_code


OWNER_ROLES: frozenset[str] = frozenset({"owner"})
READER_ROLES: frozenset[str] = frozenset({"reader", "owner"})


async def authenticated_owner(request: Request, token_verifier: TokenVerifier) -> str:
    """Return the authenticated owner's principal id, or raise Unauthorized."""

    return await authenticated_principal(request, token_verifier, roles=OWNER_ROLES)


async def authenticated_principal(
    request: Request, token_verifier: TokenVerifier, *, roles: frozenset[str]
) -> str:
    """Authenticate the bearer and require one of ``roles``; record its grants on the request.

    A reader key (``role: reader``, one tenant grant, no admin scope) may list and
    run tools for its own tenant; everything that changes the server stays owner-only.
    """

    authorization = request.headers.get("authorization", "")
    scheme, separator, token = authorization.partition(" ")
    if scheme.lower() != "bearer" or not separator or not token.strip():
        raise Unauthorized("missing bearer token", status_code=401)
    try:
        access = await token_verifier.verify_token(token.strip())
    except Exception as exc:
        # A verifier that raises is indistinguishable from a bad token to the caller, so
        # record it here or a misconfigured verifier looks exactly like a wrong password.
        # Only the exception type is logged: a verifier is free to put the presented token
        # into its message or traceback, and this log line must never become a credential
        # sink. The type alone separates "misconfigured" from "rejected".
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
    subject = claims.get("sub")
    principal_id = subject if isinstance(subject, str) and subject.strip() else access.client_id
    return principal_id or "authenticated-owner"


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


def request_tenant_default(request: Request) -> str | None:
    """Return the single tenant a token is bound to, or ``None`` when unbound.

    A token that grants exactly one tenant makes that tenant the implied scope of
    a request naming none, which is how the MCP transport already binds a call.
    """

    grants: frozenset[str] = getattr(request.state, "allowed_tenants", frozenset())
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
    grants: frozenset[str] = getattr(request.state, "allowed_tenants", frozenset())
    if ADMIN_SCOPE in scopes or "*" in grants:
        return
    raise Unauthorized(
        "token is not authorized to administer this server",
        status_code=403,
    )


def authorize_request_tenant(request: Request, tenant_id: str) -> None:
    """Authorize a custom HTTP request against grants set by ``owner_only``."""

    grants: frozenset[str] = getattr(request.state, "allowed_tenants", frozenset())
    tenant = tenant_id.strip()
    if "*" not in grants and tenant not in grants:
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
    """The playground guard: reader keys and the owner token, each within its grants."""

    return authenticated(token_verifier, roles=READER_ROLES)


def authenticated(
    token_verifier: TokenVerifier, *, roles: frozenset[str]
) -> Callable[[OwnerHandler], Callable[[Request], Awaitable[Response]]]:
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


__all__ = [
    "ADMIN_SCOPE",
    "OWNER_ROLES",
    "READER_ROLES",
    "Unauthorized",
    "allowed_tenants",
    "authenticated",
    "authenticated_owner",
    "authenticated_principal",
    "authorize_administration",
    "authorize_claimed_tenant",
    "authorize_request_tenant",
    "owner_only",
    "reader_or_owner",
    "request_tenant_default",
]
