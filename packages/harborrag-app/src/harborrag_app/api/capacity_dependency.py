"""FastAPI dependencies applying capacity leases and server-owned deadlines."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator
from typing import Annotated

from fastapi import Depends, Request

from harborrag_app.api.auth.dependencies import get_principal
from harborrag_app.api.auth.principal import Principal
from harborrag_core.contracts.errors import HarborConnectionError, HarborDeadlineExceeded

from .capacity import ApiCapacityLimiter
from .capacity_scope import SHARED_TENANT_KEY, CapacityScope
from .settings import ApiSettings

logger = logging.getLogger("harborrag.app.api.capacity")


def capacity_scope_for(principal: Principal) -> CapacityScope:
    """Charge a request to its user, its credential, and its tenant.

    The tenant key is the principal's single tenant id, or one shared pool
    for wildcard (dev) and multi-tenant credentials: a per-combination key
    would let a caller mint a private aggregate ceiling just by adding a
    second tenant id to its token. Enforcing the aggregate against every
    tenant in the set would be stricter still, but their keys hash to
    different Redis Cluster slots, which would cost the atomic reservation.
    """
    tenant_ids = principal.tenant_ids
    tenant_id = next(iter(tenant_ids)) if len(tenant_ids) == 1 else SHARED_TENANT_KEY
    return CapacityScope(
        tenant_id=tenant_id,
        principal_id=principal.subject,
        user_id=principal.user_id,
    )


async def hold_api_capacity(
    request: Request,
    principal: Annotated[Principal, Depends(get_principal)],
) -> AsyncIterator[None]:
    """Hold a user, principal and tenant slot for the full request body.

    Request-scoped (FastAPI's default for yield dependencies): the lease is
    released only after the response -- for ``StreamingResponse`` bodies, after
    the last frame -- so in-flight streams keep counting against the caller.
    """
    limiter: ApiCapacityLimiter = request.app.state.api_capacity_limiter
    scope = capacity_scope_for(principal)
    lease_id = await limiter.reserve(scope)
    try:
        yield
    finally:
        try:
            await limiter.release(scope, lease_id)
        except HarborConnectionError:
            # Redis leases expire automatically. Do not replace an otherwise
            # successful response merely because best-effort cleanup failed.
            logger.exception("Failed to release API capacity lease")


async def require_api_capacity(
    request: Request,
    _lease: Annotated[None, Depends(hold_api_capacity)],
) -> AsyncIterator[None]:
    """Enforce the server deadline on handler execution while a slot is held.

    Must be declared with ``scope="function"`` (see ``ApiCapacityDependency``)
    so the timeout is disarmed once the handler returns. With the default
    request scope it would stay armed across a ``StreamingResponse`` body and
    cancel SSE streams mid-flight with no terminal ``error`` frame; streams
    carry their own deadline instead (``ApiSettings.api_stream_timeout_seconds``).
    """
    settings: ApiSettings = request.app.state.settings
    try:
        async with asyncio.timeout(settings.api_request_timeout_seconds):
            yield
    except TimeoutError as exc:
        raise HarborDeadlineExceeded("API request exceeded its server deadline") from exc


ApiCapacityDependency = Annotated[None, Depends(require_api_capacity, scope="function")]

__all__ = [
    "ApiCapacityDependency",
    "capacity_scope_for",
    "hold_api_capacity",
    "require_api_capacity",
]
