"""Credential providers that implement ``SourceAuthProvider``.

``OAuthRefreshProvider`` handles rotating refresh tokens. When two workers
refresh the same grant, the loser's refresh token is already revoked, so the
provider:

1. adopts a credential another worker already stored instead of refreshing;
2. writes its own result with compare-and-swap, adopting the winner's on
   conflict;
3. treats ``invalid_grant`` as a lost race when the store has moved on, and
   only as a revoked grant when it has not.

Vendor-specific providers (GitHub App installation tokens, MSAL client
credentials) join this module in the phases that need them.
"""

from __future__ import annotations

import asyncio
import base64
from collections.abc import Callable, Mapping
from datetime import UTC, datetime, timedelta

import httpx

from harborrag_core.sources import AuthError, Credential, CredentialStore, Transient


def _utcnow() -> datetime:
    return datetime.now(UTC)


class StaticTokenProvider:
    """A fixed token: API tokens, personal access tokens, Data Center PATs."""

    supports_refresh = False

    def __init__(self, token: str, *, scheme: str = "Bearer") -> None:
        if not token:
            raise ValueError("token must not be empty")
        self._header = f"{scheme} {token}"

    def __repr__(self) -> str:
        return "StaticTokenProvider(<redacted>)"

    async def headers(self) -> Mapping[str, str]:
        return {"Authorization": self._header}

    async def refresh(self) -> bool:
        return False


class BasicAuthProvider:
    """HTTP basic auth: Atlassian Cloud email plus API token."""

    supports_refresh = False

    def __init__(self, username: str, secret: str) -> None:
        if not username or not secret:
            raise ValueError("username and secret must not be empty")
        encoded = base64.b64encode(f"{username}:{secret}".encode()).decode("ascii")
        self._header = f"Basic {encoded}"

    def __repr__(self) -> str:
        return "BasicAuthProvider(<redacted>)"

    async def headers(self) -> Mapping[str, str]:
        return {"Authorization": self._header}

    async def refresh(self) -> bool:
        return False


class OAuthRefreshProvider:
    """OAuth 2.0 access token kept fresh with a stored, rotating refresh token.

    The stored credential holds ``access_token``, ``refresh_token``, and an
    optional ISO ``expires_at``.
    """

    supports_refresh = True

    def __init__(  # noqa: PLR0913 - keyword-only OAuth client settings
        self,
        *,
        store: CredentialStore,
        connection_id: str,
        token_url: str,
        client_id: str,
        client_secret: str,
        http: httpx.AsyncClient,
        name: str = "oauth",
        clock: Callable[[], datetime] = _utcnow,
        skew: timedelta = timedelta(seconds=60),
    ) -> None:
        self._store = store
        self._connection_id = connection_id
        self._name = name
        self._token_url = token_url
        self._client_id = client_id
        self._client_secret = client_secret
        self._http = http
        self._clock = clock
        self._skew = skew
        self._credential: Credential | None = None
        self._lock = asyncio.Lock()

    def __repr__(self) -> str:
        return f"OAuthRefreshProvider(connection_id={self._connection_id!r})"

    async def headers(self) -> Mapping[str, str]:
        credential = await self._current()
        if self._expiring(credential):
            await self.refresh()
            credential = await self._current()
        return {"Authorization": f"Bearer {credential.require('access_token')}"}

    async def refresh(self) -> bool:
        async with self._lock:
            seen = self._credential
            latest = await self._load()
            if seen is not None and latest.version != seen.version:
                self._credential = latest
                return True
            self._credential = await self._exchange(latest)
            return True

    async def _current(self) -> Credential:
        if self._credential is None:
            async with self._lock:
                if self._credential is None:
                    self._credential = await self._load()
        return self._credential

    async def _load(self) -> Credential:
        try:
            return await self._store.get(self._connection_id, self._name)
        except KeyError as exc:
            raise AuthError(f"no {self._name} credential stored for this connection") from exc

    def _expiring(self, credential: Credential) -> bool:
        raw = credential.values.get("expires_at")
        if not raw:
            return False
        try:
            expires_at = datetime.fromisoformat(raw)
        except ValueError:
            return True
        if expires_at.tzinfo is None:
            expires_at = expires_at.replace(tzinfo=UTC)
        return expires_at - self._skew <= self._clock()

    async def _exchange(self, latest: Credential) -> Credential:
        payload = await self._post_refresh(latest.require("refresh_token"))
        if payload is None:
            again = await self._load()
            if again.version != latest.version:
                return again  # another worker rotated the grant first
            raise AuthError("the refresh token was revoked; the connection needs re-authorization")
        values = self._values(payload, latest)
        swapped = await self._store.compare_and_swap(
            self._connection_id, self._name, expected_version=latest.version, values=values
        )
        return swapped if swapped is not None else await self._load()

    async def _post_refresh(self, refresh_token: str) -> Mapping[str, object] | None:
        """POST the grant; ``None`` means ``invalid_grant``."""

        try:
            response = await self._http.post(
                self._token_url,
                data={
                    "grant_type": "refresh_token",
                    "refresh_token": refresh_token,
                    "client_id": self._client_id,
                    "client_secret": self._client_secret,
                },
            )
        except httpx.TransportError as exc:
            raise Transient(f"token refresh failed: {type(exc).__name__}") from exc
        if response.status_code >= 500:
            raise Transient(f"token endpoint returned {response.status_code}")
        try:
            payload = response.json()
        except ValueError as exc:
            raise Transient("token endpoint returned a non-JSON body") from exc
        if response.status_code in (400, 401) and payload.get("error") == "invalid_grant":
            return None
        if response.status_code != 200 or not payload.get("access_token"):
            raise AuthError(f"token refresh was rejected ({response.status_code})")
        return payload  # type: ignore[no-any-return]

    def _values(self, payload: Mapping[str, object], latest: Credential) -> dict[str, str]:
        values = dict(latest.values)
        values["access_token"] = str(payload["access_token"])
        if payload.get("refresh_token"):
            values["refresh_token"] = str(payload["refresh_token"])
        expires_in = payload.get("expires_in")
        if isinstance(expires_in, (int, float)) and expires_in > 0:
            expires_at = self._clock() + timedelta(seconds=float(expires_in))
            values["expires_at"] = expires_at.isoformat()
        else:
            values.pop("expires_at", None)
        return values
