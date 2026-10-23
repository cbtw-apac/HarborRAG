"""Delegated (signed-in user) Microsoft Graph auth via device code flow.

An operator signs in once with ``python -m
harborrag_adapters.connectors.sharepoint.login``; the refresh token is stored
in a local cache file. Connector runs then redeem that refresh token for
short-lived access tokens without further interaction, and write back the
rotated refresh token Microsoft identity returns.
"""

from __future__ import annotations

import contextlib
import json
import os
import tempfile
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import requests

from harborrag_adapters.connectors.exceptions import AuthenticationError

from .permissions import token_claims

# ``.default`` requests exactly the delegated permissions an admin consented
# for the app (e.g. ``Sites.Selected``); ``offline_access`` adds the refresh
# token so connector runs do not need a fresh sign-in.
DELEGATED_SCOPE = "https://graph.microsoft.com/.default offline_access"
DEVICE_CODE_GRANT = "urn:ietf:params:oauth:grant-type:device_code"
LOGIN_HINT = "run `python -m harborrag_adapters.connectors.sharepoint.login` to sign in"


def identity_url(tenant_id: str, endpoint: str) -> str:
    """Return a Microsoft identity platform v2.0 OAuth endpoint for a tenant."""
    return f"https://login.microsoftonline.com/{tenant_id}/oauth2/v2.0/{endpoint}"


def default_token_cache_path(tenant_id: str, client_id: str) -> Path:
    """Return the per-user cache file for one tenant/app pair."""
    return Path.home() / ".harborrag" / "sharepoint" / f"token-{tenant_id}-{client_id}.json"


@dataclass(slots=True)
class DelegatedTokenCache:
    """Refresh token storage for one tenant/app pair, readable only by its owner."""

    path: Path
    tenant_id: str
    client_id: str

    def load_refresh_token(self) -> str:
        """Return the stored refresh token or explain how to obtain one."""
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


@dataclass(slots=True)
class DeviceCodeLogin:
    """Interactive device code sign-in that seeds a ``DelegatedTokenCache``."""

    cache: DelegatedTokenCache
    session: Any = None
    timeout_seconds: float = 60.0
    sleep: Callable[[float], None] = time.sleep

    def __post_init__(self) -> None:
        if self.session is None:
            self.session = requests.Session()

    def run(self, prompt: Callable[[str], None] = print) -> str | None:
        """Show the sign-in instructions, wait for the user, and store the token.

        Returns the signed-in account name when Microsoft identity reports one.
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
                allow_pending=True,
            )
            error = payload.get("error")
            if error == "authorization_pending":
                continue
            if error == "slow_down":
                interval += 5
                continue
            if error:
                raise AuthenticationError(_identity_error(payload))
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
        self, endpoint: str, data: dict[str, str], *, allow_pending: bool = False
    ) -> dict[str, Any]:
        try:
            response = self.session.post(
                identity_url(self.cache.tenant_id, endpoint),
                data=data,
                timeout=self.timeout_seconds,
            )
        except requests.RequestException as exc:
            raise AuthenticationError("Microsoft identity request failed") from exc
        try:
            payload = response.json()
        except ValueError as exc:
            raise AuthenticationError("Microsoft identity returned non-JSON response") from exc
        if not isinstance(payload, dict):
            raise AuthenticationError("Microsoft identity returned invalid JSON response")
        if response.status_code >= 400 and not (allow_pending and "error" in payload):
            raise AuthenticationError(_identity_error(payload))
        return payload


def _identity_error(payload: dict[str, Any]) -> str:
    """Return the OAuth error code and first line of its description."""
    description = str(payload.get("error_description") or "").splitlines()
    detail = f": {description[0]}" if description else ""
    return f"Microsoft identity sign-in failed ({payload.get('error', 'unknown_error')}){detail}"


def _account_name(token: Any) -> str | None:
    claims = token_claims(str(token)) if token else None
    if not claims:
        return None
    name = claims.get("preferred_username") or claims.get("upn") or claims.get("unique_name")
    return str(name) if name else None
