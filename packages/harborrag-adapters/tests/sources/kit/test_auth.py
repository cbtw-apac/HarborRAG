from __future__ import annotations

import asyncio
from datetime import timedelta
from urllib.parse import parse_qs

import httpx
import pytest

from harborrag_adapters.sources._kit.auth import (
    BasicAuthProvider,
    OAuthRefreshProvider,
    StaticTokenProvider,
)
from harborrag_core.sources import AuthError, Transient
from harborrag_core.sources.testing import InMemoryCredentialStore

from .kit_helpers import NOW

KEY = ("conn-1", "oauth")


class TokenServer:
    """Rotating-refresh-token endpoint: each refresh token works exactly once."""

    def __init__(self) -> None:
        self.valid = {"refresh-0"}
        self.posts = 0
        self.issued = 0

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.posts += 1
        form = parse_qs(request.content.decode())
        token = form["refresh_token"][0]
        if token not in self.valid:
            return httpx.Response(400, json={"error": "invalid_grant"})
        self.valid.discard(token)
        self.issued += 1
        self.valid.add(f"refresh-{self.issued}")
        return httpx.Response(
            200,
            json={
                "access_token": f"access-{self.issued}",
                "refresh_token": f"refresh-{self.issued}",
                "expires_in": 3600,
            },
        )


def _provider(
    store: InMemoryCredentialStore, server: TokenServer, *, now: object = NOW
) -> OAuthRefreshProvider:
    return OAuthRefreshProvider(
        store=store,
        connection_id="conn-1",
        token_url="https://auth.example.com/oauth/token",
        client_id="client",
        client_secret="secret",
        http=httpx.AsyncClient(transport=httpx.MockTransport(server)),
        clock=lambda: now,  # type: ignore[arg-type, return-value]
    )


def _store(expires_at: str | None = None) -> InMemoryCredentialStore:
    values = {"access_token": "access-0", "refresh_token": "refresh-0"}
    if expires_at:
        values["expires_at"] = expires_at
    return InMemoryCredentialStore({KEY: values})


@pytest.mark.asyncio
async def test_static_and_basic_headers_hide_secrets() -> None:
    assert await StaticTokenProvider("pat").headers() == {"Authorization": "Bearer pat"}
    basic = BasicAuthProvider("me@example.com", "api-token")
    assert await basic.headers() == {"Authorization": "Basic bWVAZXhhbXBsZS5jb206YXBpLXRva2Vu"}
    assert "api-token" not in repr(basic)
    assert not await basic.refresh()


@pytest.mark.asyncio
async def test_valid_token_is_used_without_refreshing() -> None:
    server = TokenServer()
    provider = _provider(_store((NOW + timedelta(hours=1)).isoformat()), server)
    assert await provider.headers() == {"Authorization": "Bearer access-0"}
    assert server.posts == 0


@pytest.mark.asyncio
async def test_expiring_token_is_refreshed_and_rotation_is_stored() -> None:
    store, server = _store((NOW + timedelta(seconds=30)).isoformat()), TokenServer()
    provider = _provider(store, server)
    assert await provider.headers() == {"Authorization": "Bearer access-1"}
    stored = await store.get(*KEY)
    assert stored.version == 1
    assert stored.values["refresh_token"] == "refresh-1"
    assert stored.values["expires_at"] == (NOW + timedelta(hours=1)).isoformat()


@pytest.mark.asyncio
async def test_a_worker_with_a_stale_token_adopts_the_stored_one_without_posting() -> None:
    store, server = _store(), TokenServer()
    first, second = _provider(store, server), _provider(store, server)
    await first.headers()
    await second.headers()  # both cache version 0
    assert await first.refresh()
    assert await second.refresh()  # sees version 1 in the store: adopt, don't post
    assert server.posts == 1
    assert await second.headers() == {"Authorization": "Bearer access-1"}


@pytest.mark.asyncio
async def test_concurrent_refresh_race_ends_with_one_rotation_and_no_auth_error() -> None:
    store, server = _store(), TokenServer()
    workers = [_provider(store, server) for _ in range(3)]
    for worker in workers:
        await worker.headers()
    await asyncio.gather(*(worker.refresh() for worker in workers))
    headers = {(await worker.headers())["Authorization"] for worker in workers}
    assert headers == {"Bearer access-1"}
    assert store.swaps == 1
    assert (await store.get(*KEY)).values["refresh_token"] == "refresh-1"


@pytest.mark.asyncio
async def test_revoked_grant_raises_auth_error() -> None:
    server = TokenServer()
    server.valid.clear()
    with pytest.raises(AuthError, match="re-authorization"):
        await _provider(_store(), server).refresh()


@pytest.mark.asyncio
async def test_token_endpoint_outage_is_transient() -> None:
    store = _store()
    provider = OAuthRefreshProvider(
        store=store,
        connection_id="conn-1",
        token_url="https://auth.example.com/oauth/token",
        client_id="c",
        client_secret="s",
        http=httpx.AsyncClient(transport=httpx.MockTransport(lambda r: httpx.Response(503))),
    )
    with pytest.raises(Transient):
        await provider.refresh()


@pytest.mark.asyncio
async def test_missing_credential_is_an_auth_error() -> None:
    with pytest.raises(AuthError, match="no oauth credential"):
        await _provider(InMemoryCredentialStore(), TokenServer()).headers()
