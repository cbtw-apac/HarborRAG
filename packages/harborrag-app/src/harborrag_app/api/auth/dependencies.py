"""Who is calling: bearer tokens, API keys and the role and tenant checks on a principal.

Handles:
- Verifying a user token (HS256 JWT) or an API key into a ``Principal``
- One ``401 unauthenticated`` message for every rejected credential, whatever check failed
- Role and tenant checks on a principal that handlers call for fields in a body

Does NOT order the checks of a route; ``auth.access.require_role`` does.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Mapping
from ipaddress import ip_address
from typing import Annotated, Protocol, cast

from fastapi import FastAPI, Request, Security
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from harborrag_app.api.auth.base import BaseTokenVerifier
from harborrag_app.api.auth.hmac import HmacTokenVerifier
from harborrag_app.api.auth.principal import Principal
from harborrag_app.api.settings import ApiSettings
from harborrag_app.operations.errors import TenantForbiddenError
from harborrag_core.contracts.errors import (
    HarborAuthError,
    HarborCapabilityError,
    HarborConfigurationError,
    HarborConnectionError,
    HarborNotFoundError,
)
from harborrag_core.domain.identity import DEFAULT_USER
from harborrag_core.domain.member import Role
from harborrag_core.ports.api_keys import AuthStoreUnavailable
from harborrag_core.security.api_keys import parse_key

logger = logging.getLogger("harborrag.app.api.auth")

_bearer_scheme = HTTPBearer(auto_error=False)
UNAUTHENTICATED = "missing or invalid credentials"
API_KEY_ROLE: Role = "reader"


class VerifiedApiKey(Protocol):
    @property
    def key_id(self) -> str: ...

    @property
    def tenant_id(self) -> str: ...

    @property
    def owner(self) -> str: ...


class ApiKeyVerifier(Protocol):
    async def verify_key(self, raw_key: str) -> VerifiedApiKey | None: ...


def build_token_verifier(settings: ApiSettings) -> BaseTokenVerifier | None:
    """Construct the verifier for the configured auth_mode at factory time.

    none -> None (implicit owner principal); hmac -> HS256 against
    HARBORRAG_AUTH_SECRET; oidc -> HarborCapabilityError until M5.
    Fails closed: auth cannot be disabled when HARBORRAG_ENV is staging or prod.
    """
    if settings.auth_mode == "none":
        if settings.env != "dev":
            raise HarborConfigurationError(
                f"auth_mode=none is not allowed when HARBORRAG_ENV={settings.env}"
            )
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
            user_id_claim=settings.auth_user_id_claim,
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


def _arrived_on_loopback(request: Request) -> bool:
    """Whether the connection's local address is a loopback interface.

    The startup check reads ``HARBORRAG_HOST``, but uvicorn binds whatever
    ``--host`` it is given, so the socket is checked per request. The ASGI
    server fills ``scope["server"]`` from the socket, never from a header. A
    non-IP value (a Unix socket path, or a test transport) is not a network
    listener and is left to the startup check.
    """

    server = request.scope.get("server")
    if not server:
        return True
    host = str(server[0])
    try:
        ip_address(host.removeprefix("[").removesuffix("]"))
    except ValueError:
        return True
    return _is_loopback_host(host)


async def _api_key_verifier(app: FastAPI) -> ApiKeyVerifier:
    """The app's API key verifier, built over the control database on first use."""
    verifier: ApiKeyVerifier | None = getattr(app.state, "api_key_verifier", None)
    if verifier is not None:
        return verifier
    lock = getattr(app.state, "api_key_verifier_lock", None)
    if lock is None:
        lock = app.state.api_key_verifier_lock = asyncio.Lock()
    async with lock:
        verifier = getattr(app.state, "api_key_verifier", None)
        if verifier is None:
            from harborrag_runtime.composition.mcp_auth import build_api_key_verification
            from harborrag_runtime.config.settings import RuntimeSettings

            verifier = build_api_key_verification(RuntimeSettings())
            app.state.api_key_verifier = verifier
    return cast(ApiKeyVerifier, verifier)


async def _api_key_principal(app: FastAPI, raw_key: str) -> Principal:
    """Verify an API key into a reader principal of its one tenant."""
    verifier = await _api_key_verifier(app)
    try:
        key = await verifier.verify_key(raw_key)
    except AuthStoreUnavailable as exc:
        raise HarborConnectionError("the API key store is unavailable") from exc
    if key is None:
        raise HarborAuthError("unknown, revoked or expired API key")
    return Principal(
        subject=key.key_id,
        role=API_KEY_ROLE,
        tenant_ids=frozenset({key.tenant_id}),
        token_kind="api_key",
        user_id=key.owner,
        name=key.owner,
    )


async def _verified_principal(request: Request, token: str) -> Principal:
    """Verify an API key or a user token."""
    if parse_key(token) is not None:
        return await _api_key_principal(request.app, token)
    verifier: BaseTokenVerifier = request.app.state.token_verifier
    return verifier.verify(token)


async def get_principal(
    request: Request,
    credentials: Annotated[HTTPAuthorizationCredentials | None, Security(_bearer_scheme)] = None,
) -> Principal:
    """Resolve the caller's ``Principal`` from the bearer credential.

    ``auth_mode=none`` yields an implicit owner on loopback connections. Every other
    mode verifies the credential and answers one message whichever check failed.

    Args:
        request: The incoming request.
        credentials: The ``Authorization: Bearer`` credential, if any.

    Returns:
        The authenticated principal.

    Raises:
        HarborAuthError: The credential is missing or invalid (401), or ``auth_mode=none``
            served a non-loopback connection (403).
        HarborConnectionError: The API key store is unreachable.
    """
    settings: ApiSettings = request.app.state.settings
    if settings.auth_mode == "none":
        if not settings.allow_insecure_dev and not _arrived_on_loopback(request):
            raise HarborAuthError(
                "auth_mode=none serves loopback connections only; set "
                "HARBORRAG_ALLOW_INSECURE_DEV=true to acknowledge an unauthenticated "
                "non-loopback development listener",
                forbidden=True,
            )
        return Principal(
            subject="dev",
            role="owner",
            tenant_ids=frozenset({"*"}),
            token_kind="none",
            user_id=DEFAULT_USER,
            name="dev",
        )
    if credentials is None:
        raise HarborAuthError(UNAUTHENTICATED)
    try:
        return await _verified_principal(request, credentials.credentials)
    except HarborAuthError as exc:
        if exc.forbidden:
            raise
        logger.info("Rejected a bearer credential: %s", exc)
        raise HarborAuthError(UNAUTHENTICATED) from None


def authorize_tenant(principal: Principal, tenant_id: str) -> None:
    """Reject a tenant named in a body or query that the credential does not hold.

    Raises:
        TenantForbiddenError: The credential does not hold ``tenant_id``.
    """
    if not principal.can_access_tenant(tenant_id):
        raise TenantForbiddenError()


def authorize_task_tenant(principal: Principal, task: Mapping[str, object]) -> None:
    """Hide an ingestion task's existence when its tenant is outside the caller's scope.

    Shared by the v1 and legacy ingestion routes so both authorize identically
    against the same task-store record (``task["tenant"]``).
    """

    if not principal.can_access_tenant(str(task["tenant"])):
        raise HarborNotFoundError("Ingestion task was not found")
