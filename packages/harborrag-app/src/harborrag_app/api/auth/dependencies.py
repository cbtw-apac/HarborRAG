"""FastAPI auth dependencies: principal extraction and role enforcement (ST4)."""

from __future__ import annotations

import logging
from collections.abc import Callable, Mapping
from ipaddress import ip_address
from typing import Annotated

from fastapi import Depends, Request, Security
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from harborrag_app.api.auth.base import BaseTokenVerifier
from harborrag_app.api.auth.hmac import HmacTokenVerifier
from harborrag_app.api.auth.principal import ROLE_ORDER, Principal
from harborrag_app.api.settings import ApiSettings
from harborrag_core.contracts.errors import (
    HarborAuthError,
    HarborCapabilityError,
    HarborConfigurationError,
    HarborNotFoundError,
)
from harborrag_core.domain.member import Role

logger = logging.getLogger("harborrag.app.api.auth")

_bearer_scheme = HTTPBearer(auto_error=False)


def build_token_verifier(settings: ApiSettings) -> BaseTokenVerifier | None:
    """Construct the verifier for the configured auth_mode at factory time.

    none -> None (implicit owner principal); hmac -> HS256 against
    HARBORRAG_AUTH_SECRET; oidc -> HarborCapabilityError until M5.
    Fails closed: auth cannot be disabled when HARBORRAG_ENV=prod.
    """
    if settings.auth_mode == "none":
        if settings.env == "prod":
            raise HarborConfigurationError("auth_mode=none is not allowed when HARBORRAG_ENV=prod")
        if not _is_loopback_host(settings.host) and not settings.allow_insecure_dev:
            raise HarborConfigurationError(
                "auth_mode=none may bind only to a loopback host; set "
                "HARBORRAG_ALLOW_INSECURE_DEV=true to acknowledge an unauthenticated "
                "non-loopback development listener"
            )
        # `env` and `auth_mode` both default to permissive values ("dev" and
        # "none"), so a deployment that forgets to set HARBORRAG_ENV=prod
        # falls through here silently -- the process boots and every request
        # is treated as an implicit owner with unrestricted tenant access,
        # with no signal that auth is off. Log loudly so that's never silent,
        # even though the prod check above can't catch a misconfigured env.
        logger.warning(
            "Starting with HARBORRAG_AUTH_MODE=none: every request is treated "
            "as an implicit owner principal with unrestricted tenant access. "
            "This is a dev-only default -- set HARBORRAG_AUTH_MODE=hmac (or "
            "oidc) before exposing this process beyond a trusted local "
            "environment, and set HARBORRAG_ENV=prod so misconfiguration "
            "fails to start instead of booting open."
        )
        return None
    if settings.auth_mode == "hmac":
        if not settings.auth_secret:
            raise HarborConfigurationError("auth_mode=hmac requires HARBORRAG_AUTH_SECRET")
        secret = settings.auth_secret.get_secret_value()
        if len(secret.encode("utf-8")) < 32:
            raise HarborConfigurationError(
                "HARBORRAG_AUTH_SECRET must contain at least 32 UTF-8 bytes"
            )
        return HmacTokenVerifier(
            secret=secret,
            issuer=settings.auth_issuer,
            audience=settings.auth_audience,
            max_token_lifetime_seconds=settings.auth_max_token_lifetime_seconds,
            clock_skew_seconds=settings.auth_clock_skew_seconds,
        )
    raise HarborCapabilityError("auth_mode=oidc lands in M5")


def _is_loopback_host(host: str) -> bool:
    """Accept explicit loopback names and addresses, never unresolved hostnames."""

    normalized = host.strip().lower().removeprefix("[").removesuffix("]")
    if normalized == "localhost":
        return True
    try:
        return ip_address(normalized).is_loopback
    except ValueError:
        return False


def get_principal(
    request: Request,
    credentials: Annotated[HTTPAuthorizationCredentials | None, Security(_bearer_scheme)] = None,
) -> Principal:
    """Resolve the caller's Principal from the bearer token.

    auth_mode=none yields an implicit owner (dev default); otherwise a missing
    or invalid token raises HarborAuthError (401 envelope).
    """
    settings: ApiSettings = request.app.state.settings
    if settings.auth_mode == "none":
        return Principal(
            subject="dev",
            role="owner",
            tenant_ids=frozenset({"*"}),
            token_kind="none",
        )
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


def authorize_tenant(principal: Principal, tenant_id: str) -> None:
    """Reject cross-tenant access even when the caller has a high global role."""

    if not principal.can_access_tenant(tenant_id):
        raise HarborAuthError("tenant access is not permitted", forbidden=True)


def authorize_task_tenant(principal: Principal, task: Mapping[str, object]) -> None:
    """Hide an ingestion task's existence when its tenant is outside the caller's scope.

    Shared by the v1 and legacy ingestion routes so both authorize identically
    against the same task-store record (``task["tenant"]``).
    """

    if not principal.can_access_tenant(str(task["tenant"])):
        raise HarborNotFoundError("Ingestion task was not found")
