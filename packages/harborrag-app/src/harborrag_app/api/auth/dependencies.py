"""FastAPI auth dependencies: principal extraction and role enforcement (ST4)."""

from __future__ import annotations

from collections.abc import Callable
from typing import Annotated

from fastapi import Depends, Request, Security
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from harborrag_core.contracts.errors import (
    HarborAuthError,
    HarborCapabilityError,
    HarborConfigurationError,
)
from harborrag_core.domain.member import Role

from harborrag_app.api.auth.base import BaseTokenVerifier
from harborrag_app.api.auth.hmac import HmacTokenVerifier
from harborrag_app.api.auth.principal import ROLE_ORDER, Principal
from harborrag_app.api.settings import ApiSettings

_bearer_scheme = HTTPBearer(auto_error=False)


def build_token_verifier(settings: ApiSettings) -> BaseTokenVerifier | None:
    """Construct the verifier for the configured auth_mode at factory time.

    none -> None (implicit owner principal); hmac -> HS256 against
    HARBORRAG_AUTH_SECRET; oidc -> HarborCapabilityError until M5.
    """
    if settings.auth_mode == "none":
        return None
    if settings.auth_mode == "hmac":
        if not settings.auth_secret:
            raise HarborConfigurationError(
                "auth_mode=hmac requires HARBORRAG_AUTH_SECRET"
            )
        return HmacTokenVerifier(secret=settings.auth_secret)
    raise HarborCapabilityError("auth_mode=oidc lands in M5")


def get_principal(
    request: Request,
    credentials: Annotated[
        HTTPAuthorizationCredentials | None, Security(_bearer_scheme)
    ] = None,
) -> Principal:
    """Resolve the caller's Principal from the bearer token.

    auth_mode=none yields an implicit owner (dev default); otherwise a missing
    or invalid token raises HarborAuthError (401 envelope).
    """
    settings: ApiSettings = request.app.state.settings
    if settings.auth_mode == "none":
        return Principal(subject="dev", role="owner", token_kind="none")
    if credentials is None:
        raise HarborAuthError("missing bearer token")
    verifier: BaseTokenVerifier = request.app.state.token_verifier
    return verifier.verify(credentials.credentials)


def require_role(minimum: Role) -> Callable[..., Principal]:
    """Dependency factory enforcing a minimum role on a route (403 below it)."""

    def dependency(
        principal: Annotated[Principal, Depends(get_principal)],
    ) -> Principal:
        """Pass the principal through when its role clears the minimum."""
        if ROLE_ORDER[principal.role] < ROLE_ORDER[minimum]:
            raise HarborAuthError(f"requires {minimum} role", forbidden=True)
        return principal

    return dependency
