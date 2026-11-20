"""Delegated (signed-in user) Microsoft Graph auth through the device code flow.

An operator signs in once with ``python -m
harborrag_adapters.connectors.sharepoint.login``, which stores a refresh token
in an owner-only cache file. Connector runs redeem it for short-lived access
tokens and write back the rotated one. The delegated scope asks for exactly the
permissions an admin consented for the app, plus ``offline_access`` for the
refresh token. Error messages never contain a token or device code.
"""

from __future__ import annotations

import contextlib
import json
import os
import re
import tempfile
import time
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from typing import Any

import requests

from harborrag_adapters.connectors.exceptions import AuthenticationError
from harborrag_adapters.connectors.policies.http import (
    ResponseTooLargeError,
    read_capped_json,
    safe_error_detail,
)
from harborrag_adapters.connectors.policies.network import DestinationPolicy, guarded_session

from .permissions import token_claims

DELEGATED_SCOPE = "https://graph.microsoft.com/.default offline_access"
DEVICE_CODE_GRANT = "urn:ietf:params:oauth:grant-type:device_code"
LOGIN_HINT = (
    "run `python -m harborrag_adapters.connectors.sharepoint.login` to sign in "
    "(for the Temporal worker: `scripts/deployment/dev.sh sharepoint-login`)"
)


_PATH_SEGMENT = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}")


def require_plain_id(label: str, value: str) -> None:
    """Refuse an id that is not a plain name, so it cannot change a path.

    Raises:
        ValueError: ``value`` is not a GUID or a domain-like name.
    """
    if not _PATH_SEGMENT.fullmatch(value) or ".." in value:
        raise ValueError(f"{label} must be a plain id of letters, digits, '.', '_' or '-'")


def identity_url(tenant_id: str, endpoint: str) -> str:
    """Return a Microsoft identity platform v2.0 OAuth endpoint for a tenant.

    Raises:
        ValueError: ``tenant_id`` is not a GUID or a domain-like name.
    """
    require_plain_id("tenant_id", tenant_id)
    return f"https://login.microsoftonline.com/{tenant_id}/oauth2/v2.0/{endpoint}"


def default_token_cache_path(harborrag_tenant_id: str, tenant_id: str, client_id: str) -> Path:
    """Return the cache file of one tenant/app pair inside one HarborRAG tenant.

    Each HarborRAG tenant gets its own folder, so two tenants that enter the
    same Entra tenant and app never share a signed-in refresh token.

    Args:
        harborrag_tenant_id: HarborRAG tenant that owns the sign-in.
        tenant_id: Entra tenant of the app registration.
        client_id: Client ID of the app registration.

    Returns:
        Path of the token cache file under the user's home folder.

    Raises:
        ValueError: An id is not a plain name, so it could leave the cache folder.
    """
    for label, value in (
        ("HarborRAG tenant", harborrag_tenant_id),
        ("tenant_id", tenant_id),
        ("client_id", client_id),
    ):
        require_plain_id(label, value)
    folder = Path.home() / ".harborrag" / "sharepoint" / harborrag_tenant_id
    return folder / f"token-{tenant_id}-{client_id}.json"


@dataclass(slots=True)
class DelegatedTokenCache:
    """Refresh token storage for one tenant/app pair, readable only by its owner.

    Attributes:
        path: Cache file that holds the refresh token.
        tenant_id: Entra tenant the stored sign-in belongs to.
        client_id: App registration the stored sign-in belongs to.
    """

    path: Path
    tenant_id: str
    client_id: str

    def load_refresh_token(self) -> str:
        """Return the stored refresh token.

        Returns:
            The refresh token saved by the last sign-in or token rotation.

        Raises:
            AuthenticationError: No usable sign-in is cached for this tenant and
                app. The message says how to sign in.
        """
        try:
            payload = json.loads(self.path.read_text(encoding="utf-8"))
        except FileNotFoundError as exc:
            raise AuthenticationError(f"No SharePoint sign-in found; {LOGIN_HINT}") from exc
        except (OSError, ValueError) as exc:
            raise AuthenticationError(
                f"SharePoint token cache is unreadable; {LOGIN_HINT}"
            ) from exc
        if not isinstance(payload, dict):
            raise AuthenticationError(f"SharePoint token cache is invalid; {LOGIN_HINT}")
        if payload.get("tenant_id") != self.tenant_id or payload.get("client_id") != self.client_id:
            raise AuthenticationError(
                f"SharePoint token cache belongs to another tenant or app; {LOGIN_HINT}"
            )
        token = payload.get("refresh_token")
        if not isinstance(token, str) or not token:
            raise AuthenticationError(f"SharePoint token cache has no refresh token; {LOGIN_HINT}")
        return token

    def save(self, refresh_token: str, *, account: str | None = None) -> None:
        """Atomically write the refresh token with owner-only file permissions.

        Each write gets its own exclusively created, owner-only temporary file,
        so concurrent saves never share or truncate one, and a file another
        user pre-created in the cache directory is never written through.

        Args:
            refresh_token: Refresh token to store.
            account: Signed-in account name stored with the token, when known.
        """
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "tenant_id": self.tenant_id,
            "client_id": self.client_id,
            "account": account,
            "refresh_token": refresh_token,
            "saved_at": int(time.time()),
        }
        descriptor, temporary = tempfile.mkstemp(
            dir=self.path.parent, prefix=f"{self.path.name}.", suffix=".tmp"
        )
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
                json.dump(payload, handle)
            os.replace(temporary, self.path)
        except BaseException:
            with contextlib.suppress(OSError):
                os.unlink(temporary)
            raise

    def clear(self) -> None:
        """Delete the stored sign-in, e.g. when access is revoked."""
        self.path.unlink(missing_ok=True)


class _ErrorPayload(StrEnum):
    """What `DeviceCodeLogin._post` does with an OAuth error answer."""

    RAISE = "raise"
    RETURN = "return"


@dataclass(slots=True)
class DeviceCodeLogin:
    """Interactive device code sign-in that seeds a ``DelegatedTokenCache``.

    Args:
        cache: Where the refresh token is stored; its tenant ID names the sign-in endpoint.
        session: HTTP session to use; by default one that obeys ``destination_policy``.
        destination_policy: Which non-public hosts the default session may reach; the
            default reaches public addresses only.
        timeout_seconds: Timeout of each request to Microsoft identity.
        sleep: Waits between polls for the user's sign-in.

    Attributes:
        cache: Where the refresh token is stored.
        session: HTTP session used for Microsoft identity requests.
        destination_policy: Which non-public hosts the default session may reach.
        timeout_seconds: Timeout of each request to Microsoft identity.
        sleep: Waits between polls for the user's sign-in.
    """

    cache: DelegatedTokenCache
    session: Any = None
    destination_policy: DestinationPolicy = field(default_factory=DestinationPolicy)
    timeout_seconds: float = 60.0
    sleep: Callable[[float], None] = time.sleep

    def __post_init__(self) -> None:
        if self.session is None:
            self.session = guarded_session(self.destination_policy)

    def run(self, prompt: Callable[[str], None] = print) -> str | None:
        """Show the sign-in instructions, wait for the user, and store the token.

        Args:
            prompt: Function that shows the sign-in instructions to the operator.

        Returns:
            The signed-in account name, or ``None`` when Microsoft identity
            reports none.

        Raises:
            AuthenticationError: Sign-in failed, timed out or returned no
                refresh token.
            BlockedDestinationError: The identity host resolves to an address the
                destination policy refuses.
            ValueError: The cache's tenant ID is not a GUID or a domain-like name.
        """
        flow = self._post(
            "devicecode",
            {"client_id": self.cache.client_id, "scope": DELEGATED_SCOPE},
        )
        device_code = flow.get("device_code")
        if not device_code:
            raise AuthenticationError("Microsoft identity did not return a device code")
        fallback = f"Enter code {flow.get('user_code')} at {flow.get('verification_uri')}"
        prompt(str(flow.get("message") or fallback))

        interval = float(flow.get("interval") or 5)
        deadline = time.monotonic() + float(flow.get("expires_in") or 900)
        while time.monotonic() < deadline:
            self.sleep(interval)
            payload = self._post(
                "token",
                {
                    "grant_type": DEVICE_CODE_GRANT,
                    "client_id": self.cache.client_id,
                    "device_code": device_code,
                },
                error_payload=_ErrorPayload.RETURN,
            )
            error = payload.get("error")
            if error == "authorization_pending":
                continue
            if error == "slow_down":
                interval += 5
                continue
            if error:
                raise AuthenticationError(
                    _identity_error(payload, known_secrets=(str(device_code),))
                )
            refresh_token = payload.get("refresh_token")
            if not refresh_token:
                raise AuthenticationError(
                    "Sign-in returned no refresh token; the app needs the offline_access permission"
                )
            account = _account_name(payload.get("id_token") or payload.get("access_token"))
            self.cache.save(str(refresh_token), account=account)
            return account
        raise AuthenticationError("Device code sign-in timed out")

    def _post(
        self,
        endpoint: str,
        data: dict[str, str],
        *,
        error_payload: _ErrorPayload = _ErrorPayload.RAISE,
    ) -> dict[str, Any]:
        """POST a form to a Microsoft identity endpoint and return its JSON object.

        The body is streamed under the connector JSON size cap, and an error
        message never contains the ``device_code`` posted in ``data``.

        Args:
            endpoint: Identity endpoint name, such as ``devicecode`` or ``token``.
            data: Form fields to post.
            error_payload: Whether an OAuth error answer is raised or returned,
                so the caller can keep polling while sign-in is pending.

        Returns:
            The decoded JSON object.

        Raises:
            AuthenticationError: The request failed, the body is too large or
                not a JSON object, or the endpoint answered with an error.
        """
        try:
            response = self.session.post(
                identity_url(self.cache.tenant_id, endpoint),
                data=data,
                timeout=self.timeout_seconds,
                stream=True,
            )
        except requests.RequestException as exc:
            raise AuthenticationError("Microsoft identity request failed") from exc
        try:
            payload = read_capped_json(response)
        except ResponseTooLargeError as exc:
            raise AuthenticationError("Microsoft identity response is too large") from exc
        except ValueError as exc:
            raise AuthenticationError("Microsoft identity returned non-JSON response") from exc
        if not isinstance(payload, dict):
            raise AuthenticationError("Microsoft identity returned invalid JSON response")
        returns_errors = error_payload is _ErrorPayload.RETURN
        if response.status_code >= 400 and not (returns_errors and "error" in payload):
            raise AuthenticationError(
                _identity_error(payload, known_secrets=(data.get("device_code"),))
            )
        return payload


def _identity_error(payload: Mapping[str, object], *, known_secrets: Iterable[str | None]) -> str:
    """Return the redacted OAuth error code and first line of its description."""
    description = str(payload.get("error_description") or "").splitlines()
    detail = f": {description[0]}" if description else ""
    message = f"Microsoft identity sign-in failed ({payload.get('error', 'unknown_error')}){detail}"
    return safe_error_detail(message, known_secrets=known_secrets)


def _account_name(token: object) -> str | None:
    claims = token_claims(str(token)) if token else None
    if not claims:
        return None
    name = claims.get("preferred_username") or claims.get("upn") or claims.get("unique_name")
    return str(name) if name else None
