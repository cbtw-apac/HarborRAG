"""FastAPI dependency applying capacity leases and server-owned deadlines."""

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
from .settings import ApiSettings

logger = logging.getLogger("harborrag.app.api.capacity")


async def require_api_capacity(
    request: Request,
    principal: Annotated[Principal, Depends(get_principal)],
) -> AsyncIterator[None]:
    """Hold a per-principal slot for the full request and enforce its deadline."""
    limiter: ApiCapacityLimiter = request.app.state.api_capacity_limiter
    settings: ApiSettings = request.app.state.settings
    lease_id = await limiter.reserve(principal.subject)
    try:
        try:
            async with asyncio.timeout(settings.api_request_timeout_seconds):
                yield
        except TimeoutError as exc:
            raise HarborDeadlineExceeded("API request exceeded its server deadline") from exc
    finally:
        try:
            await limiter.release(principal.subject, lease_id)
        except HarborConnectionError:
            # Redis leases expire automatically. Do not replace an otherwise
            # successful response merely because best-effort cleanup failed.
            logger.exception("Failed to release API capacity lease")


ApiCapacityDependency = Annotated[None, Depends(require_api_capacity)]

__all__ = ["ApiCapacityDependency", "require_api_capacity"]
