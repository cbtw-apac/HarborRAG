from __future__ import annotations

from datetime import timedelta

import httpx
import pytest

from harborrag_adapters.sources._kit.errors import (
    MalformedResponse,
    ResponseTooLarge,
    UnexpectedStatus,
)
from harborrag_adapters.sources._kit.net import SsrfGuard
from harborrag_adapters.sources._kit.retry import RetryPolicy
from harborrag_core.sources import (
    AuthError,
    ConfigError,
    Forbidden,
    ItemNotFound,
    RateLimited,
    Transient,
)

from .kit_helpers import NOW, FakeAuth, Sleeps, make_client


@pytest.mark.asyncio
async def test_get_sends_auth_and_returns_parsed_json() -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json={"ok": True})

    async with make_client(handler) as client:
        response = await client.request("GET", "/rest/api/3/myself", params={"a": "1"})
    assert response.json() == {"ok": True}
    assert seen[0].headers["authorization"] == "Bearer token-0"
    assert str(seen[0].url) == "https://jira.example.com/rest/api/3/myself?a=1"


@pytest.mark.asyncio
async def test_retries_server_errors_with_backoff_then_succeeds() -> None:
    statuses = iter([503, 502, 200])
    sleeps = Sleeps()

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(next(statuses), json={})

    async with make_client(handler, sleeps=sleeps) as client:
        assert (await client.request("GET", "/x")).status_code == 200
    assert sleeps.calls == [0.5, 1.0]


@pytest.mark.asyncio
async def test_exhausted_server_errors_raise_transient() -> None:
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(500, text="boom")

    async with make_client(handler, retry=RetryPolicy(attempts=3, rng=lambda: 0)) as client:
        with pytest.raises(Transient, match="500"):
            await client.request("GET", "/x")
    assert calls == 3


@pytest.mark.asyncio
async def test_post_is_not_retried_unless_marked_read_only() -> None:
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(503) if calls < 3 else httpx.Response(200, json=[])

    async with make_client(handler) as client:
        with pytest.raises(Transient):
            await client.request("POST", "/search", json={})
        assert calls == 1
        response = await client.request("POST", "/search", json={}, read_only=True)
    assert response.status_code == 200 and calls == 3


@pytest.mark.asyncio
async def test_throttling_honours_retry_after_then_escalates_long_waits() -> None:
    responses = iter(
        [
            httpx.Response(429, headers={"Retry-After": "5"}),
            httpx.Response(200, json={}),
            httpx.Response(429, headers={"Retry-After": "3600"}),
        ]
    )
    sleeps = Sleeps()

    async with make_client(lambda request: next(responses), sleeps=sleeps) as client:
        await client.request("GET", "/x")
        with pytest.raises(RateLimited) as caught:
            await client.request("GET", "/x")
    assert sleeps.calls == [5.0]
    assert caught.value.retry_at == NOW + timedelta(seconds=3600)


@pytest.mark.asyncio
async def test_github_style_403_rate_limit_is_throttling_not_forbidden() -> None:
    responses = iter(
        [
            httpx.Response(403, headers={"x-ratelimit-remaining": "0", "retry-after": "2"}),
            httpx.Response(200, json={}),
        ]
    )
    async with make_client(lambda request: next(responses)) as client:
        assert (await client.request("GET", "/x")).status_code == 200


@pytest.mark.asyncio
async def test_persistent_throttling_raises_rate_limited_not_transient() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(429)

    async with make_client(handler, retry=RetryPolicy(attempts=2, rng=lambda: 0)) as client:
        with pytest.raises(RateLimited):
            await client.request("GET", "/x")


@pytest.mark.asyncio
async def test_401_refreshes_once_then_raises() -> None:
    auth = FakeAuth()
    tokens: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        tokens.append(request.headers["authorization"])
        return httpx.Response(200, json={}) if len(tokens) == 2 else httpx.Response(401)

    async with make_client(handler, auth=auth) as client:
        await client.request("GET", "/x")
        assert tokens == ["Bearer token-0", "Bearer token-1"]
        with pytest.raises(AuthError):
            await client.request("GET", "/x")
    assert auth.refreshes == 2  # one per request, never twice for one request
    assert tokens[2:] == ["Bearer token-1", "Bearer token-2"]


@pytest.mark.asyncio
async def test_401_without_refresh_support_raises_immediately() -> None:
    auth = FakeAuth(supports_refresh=False)
    async with make_client(lambda request: httpx.Response(401), auth=auth) as client:
        with pytest.raises(AuthError):
            await client.request("GET", "/x")
    assert auth.refreshes == 0


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("status", "error"),
    [(403, Forbidden), (404, ItemNotFound), (410, ItemNotFound), (400, UnexpectedStatus)],
)
async def test_client_errors_map_to_run_level_errors(status: int, error: type[Exception]) -> None:
    async with make_client(lambda request: httpx.Response(status, text="bad jql")) as client:
        with pytest.raises(error):
            await client.request("GET", "/x")


@pytest.mark.asyncio
async def test_unexpected_status_keeps_code_and_detail_for_vendor_quirks() -> None:
    async with make_client(lambda request: httpx.Response(400, text=" bad\n jql ")) as client:
        with pytest.raises(UnexpectedStatus) as caught:
            await client.request("GET", "/x")
    assert caught.value.status_code == 400
    assert caught.value.detail == "bad jql"
    assert isinstance(caught.value, ConfigError)


@pytest.mark.asyncio
async def test_network_errors_retry_then_raise_transient() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectTimeout("slow", request=request)

    async with make_client(handler, retry=RetryPolicy(attempts=2, rng=lambda: 0)) as client:
        with pytest.raises(Transient, match="ConnectTimeout"):
            await client.request("GET", "/x")


@pytest.mark.asyncio
async def test_response_size_cap_applies_to_declared_and_streamed_bodies() -> None:
    async with make_client(
        lambda request: httpx.Response(200, content=b"x" * 2048), max_response_bytes=1024
    ) as client:
        with pytest.raises(ResponseTooLarge):
            await client.request("GET", "/x")

    async def chunked(request: httpx.Request) -> httpx.Response:
        async def body():  # no content-length header: the cap must apply while reading
            for _ in range(4):
                yield b"y" * 512

        return httpx.Response(200, content=body())

    async with make_client(chunked, max_response_bytes=1024) as client:
        with pytest.raises(ResponseTooLarge):
            await client.request("GET", "/x")


@pytest.mark.asyncio
async def test_malformed_json_raises_malformed_response() -> None:
    async with make_client(lambda request: httpx.Response(200, content=b"{oops")) as client:
        with pytest.raises(MalformedResponse):
            (await client.request("GET", "/x")).json()


@pytest.mark.asyncio
async def test_same_origin_redirect_keeps_credentials() -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        if request.url.path == "/old":
            return httpx.Response(302, headers={"Location": "/new?x=1"})
        return httpx.Response(200, json={})

    async with make_client(handler) as client:
        await client.request("GET", "/old", params={"a": "b"})
    assert [str(request.url) for request in seen] == [
        "https://jira.example.com/old?a=b",
        "https://jira.example.com/new?x=1",
    ]
    assert all(request.headers["authorization"] for request in seen)


@pytest.mark.asyncio
async def test_cross_origin_redirect_is_refused_unless_allowed_and_never_sends_auth() -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        if request.url.host == "jira.example.com":
            return httpx.Response(302, headers={"Location": "https://media.example.net/f?sig=1"})
        return httpx.Response(200, content=b"file-bytes")

    async with make_client(handler) as client:
        with pytest.raises(ConfigError, match="refusing a redirect"):
            await client.request("GET", "/download")
        async with client.stream("GET", "/download", allow_cross_origin_redirect=True) as stream:
            body = b"".join([chunk async for chunk in stream.aiter_bytes()])
    assert body == b"file-bytes"
    assert seen[-1].url.host == "media.example.net"
    assert "authorization" not in seen[-1].headers


@pytest.mark.asyncio
async def test_redirect_loops_stop() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(302, headers={"Location": "/again"})

    async with make_client(handler, retry=RetryPolicy(attempts=1)) as client:
        with pytest.raises(Transient, match="redirects"):
            await client.request("GET", "/x")


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "address", ["10.0.0.5", "127.0.0.1", "169.254.169.254", "::1", "::ffff:192.168.1.4"]
)
async def test_ssrf_guard_blocks_non_public_addresses(address: str) -> None:
    async def resolver(host: str, port: int) -> list[str]:
        return [address]

    guard = SsrfGuard(resolver=resolver)
    with pytest.raises(ConfigError, match="non-public"):
        await guard.check(httpx.URL("https://jira.internal/x"))


@pytest.mark.asyncio
async def test_ssrf_guard_allowlist_scheme_and_literal_ips() -> None:
    async def resolver(host: str, port: int) -> list[str]:
        raise AssertionError("allowlisted and literal hosts are not resolved")

    guard = SsrfGuard(["jira.corp.local"], resolver=resolver)
    await guard.check(httpx.URL("https://JIRA.corp.local/x"))
    with pytest.raises(ConfigError, match="non-public"):
        await guard.check(httpx.URL("https://192.168.0.10/x"))
    with pytest.raises(ConfigError, match="only https"):
        await guard.check(httpx.URL("http://jira.corp.local/x"))
    await SsrfGuard(["jira.corp.local"], allow_http=True).check(
        httpx.URL("http://jira.corp.local/x")
    )


@pytest.mark.asyncio
async def test_ssrf_guard_resolver_failures_are_transient() -> None:
    async def resolver(host: str, port: int) -> list[str]:
        raise OSError("resolver down")

    with pytest.raises(Transient):
        await SsrfGuard(resolver=resolver).check(httpx.URL("https://jira.example.com"))


@pytest.mark.asyncio
async def test_client_refuses_private_targets_before_sending() -> None:
    async def private(host: str, port: int) -> list[str]:
        return ["10.1.2.3"]

    def handler(request: httpx.Request) -> httpx.Response:
        raise AssertionError("must not be sent")

    async with make_client(handler, guard=SsrfGuard(resolver=private)) as client:
        with pytest.raises(ConfigError):
            await client.request("GET", "/x")
