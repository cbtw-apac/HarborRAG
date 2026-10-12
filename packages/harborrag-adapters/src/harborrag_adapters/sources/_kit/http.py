"""The shared async HTTP client every source connector talks through.

Retries, rate limits, credential refresh, the SSRF guard, redirect policy,
and the response size cap are implemented here once:

* A 401 refreshes credentials once, then raises :class:`AuthError`.
* Throttling and 5xx responses are retried for idempotent requests only.
* Redirects are followed by hand. Same-origin hops keep credentials; a
  cross-origin hop is refused unless the call allows it, and then it never
  carries the ``Authorization`` header (pre-signed download URLs).
* Status codes map to the run-level errors in ``harborrag_core.sources``.
"""

from __future__ import annotations

import asyncio
import json as jsonlib
from collections.abc import AsyncIterator, Awaitable, Callable, Mapping
from contextlib import asynccontextmanager
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx

from harborrag_core.sources import (
    AuthError,
    ConfigError,
    Forbidden,
    ItemNotFound,
    RateLimited,
    SourceAuthProvider,
    Transient,
)

from .errors import MalformedResponse, ResponseTooLarge, UnexpectedStatus
from .net import SsrfGuard, same_origin
from .rate_limit import RateLimiter, UnlimitedRateLimiter
from .retry import RetryPolicy

MAX_REDIRECTS = 5
DEFAULT_MAX_RESPONSE_BYTES = 32 * 1024 * 1024
_ERROR_DETAIL_BYTES = 2048
_REDIRECT_TO_GET = frozenset({301, 302, 303})


def _utcnow() -> datetime:
    return datetime.now(UTC)


@dataclass(frozen=True, slots=True)
class KitResponse:
    """A fully read response."""

    status_code: int
    headers: Mapping[str, str]
    url: str
    content: bytes

    def json(self) -> Any:
        try:
            return jsonlib.loads(self.content)
        except (ValueError, UnicodeDecodeError) as exc:
            raise MalformedResponse(f"response from {self.url} is not valid JSON") from exc

    @property
    def text(self) -> str:
        return self.content.decode("utf-8", errors="replace")


class KitStream:
    """A response whose body is read incrementally."""

    def __init__(self, response: httpx.Response) -> None:
        self._response = response

    @property
    def status_code(self) -> int:
        return self._response.status_code

    @property
    def headers(self) -> Mapping[str, str]:
        return self._response.headers

    @property
    def url(self) -> str:
        return str(self._response.url)

    async def aiter_bytes(self) -> AsyncIterator[bytes]:
        try:
            async for chunk in self._response.aiter_bytes():
                yield chunk
        except httpx.TransportError as exc:
            raise Transient(f"stream from {self.url} broke: {type(exc).__name__}") from exc


@dataclass(frozen=True, slots=True)
class _Call:
    method: str
    url: str
    params: Mapping[str, Any] | None = None
    json: Any = None
    data: Mapping[str, str] | None = None
    headers: Mapping[str, str] | None = None
    read_only: bool = False
    cost: int = 1
    allow_cross_origin_redirect: bool = False


def _throttled(response: httpx.Response) -> bool:
    if response.status_code == 429:
        return True
    # GitHub signals both primary and secondary rate limits with 403.
    return response.status_code == 403 and (
        response.headers.get("x-ratelimit-remaining") == "0" or "retry-after" in response.headers
    )


def _detail(content: bytes) -> str:
    text = content[:_ERROR_DETAIL_BYTES].decode("utf-8", errors="replace")
    return " ".join(text.split())[:300]


def _status_error(response: httpx.Response, detail: str) -> Exception:
    status = response.status_code
    where = f"{response.request.method} {response.url.copy_with(query=None)}"
    message = f"{status} for {where}"
    if status == 401:
        return AuthError(f"{message}: credentials rejected")
    if status == 403:
        return Forbidden(f"{message}: access denied")
    if status in (404, 410):
        return ItemNotFound(message)
    if status >= 500:
        return Transient(f"{message}: server error")
    return UnexpectedStatus(status, f"{message}: {detail}" if detail else message, detail)


class HttpxSourceClient:
    """``SourceHttpClient`` over ``httpx.AsyncClient``; one per connection."""

    def __init__(  # noqa: PLR0913 - keyword-only collaborators with test seams
        self,
        *,
        base_url: str,
        auth: SourceAuthProvider,
        limiter: RateLimiter | None = None,
        retry: RetryPolicy | None = None,
        guard: SsrfGuard | None = None,
        max_response_bytes: int = DEFAULT_MAX_RESPONSE_BYTES,
        timeout: float = 30.0,
        headers: Mapping[str, str] | None = None,
        transport: httpx.AsyncBaseTransport | None = None,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        clock: Callable[[], datetime] = _utcnow,
    ) -> None:
        self._base = httpx.URL(base_url)
        self._auth = auth
        self._limiter = limiter or UnlimitedRateLimiter()
        self._retry = retry or RetryPolicy()
        self._guard = guard or SsrfGuard()
        self._max_bytes = max_response_bytes
        self._sleep = sleep
        self._clock = clock
        self._client = httpx.AsyncClient(
            base_url=self._base,
            headers={"User-Agent": "HarborRAG", **(headers or {})},
            timeout=timeout,
            follow_redirects=False,
            transport=transport,
        )

    async def __aenter__(self) -> HttpxSourceClient:
        return self

    async def __aexit__(self, *exc_info: object) -> None:
        await self.aclose()

    async def aclose(self) -> None:
        await self._client.aclose()

    async def request(  # noqa: PLR0913 - keyword-only request options
        self,
        method: str,
        url: str,
        *,
        params: Mapping[str, Any] | None = None,
        json: Any = None,
        data: Mapping[str, str] | None = None,
        headers: Mapping[str, str] | None = None,
        read_only: bool = False,
        cost: int = 1,
    ) -> KitResponse:
        call = _Call(method.upper(), url, params, json, data, headers, read_only, cost)
        response = await self._send(call)
        try:
            content = await self._read_capped(response)
        finally:
            await response.aclose()
        return KitResponse(response.status_code, response.headers, str(response.url), content)

    @asynccontextmanager
    async def stream(
        self,
        method: str,
        url: str,
        *,
        params: Mapping[str, Any] | None = None,
        headers: Mapping[str, str] | None = None,
        allow_cross_origin_redirect: bool = False,
    ) -> AsyncIterator[KitStream]:
        call = _Call(
            method.upper(),
            url,
            params,
            headers=headers,
            allow_cross_origin_redirect=allow_cross_origin_redirect,
        )
        response = await self._send(call)
        try:
            yield KitStream(response)
        finally:
            await response.aclose()

    async def _send(self, call: _Call) -> httpx.Response:
        """Return an open 2xx response, or raise the mapped run-level error."""

        attempt, refreshed = 0, False
        while True:
            attempt += 1
            try:
                response = await self._follow(call)
            except httpx.TransportError as exc:
                await self._backoff_after_network_error(call, attempt, exc)
                continue
            if response.is_success:
                return response
            detail = _detail(await self._read_prefix(response))
            if response.status_code == 401 and not refreshed and self._auth.supports_refresh:
                refreshed = True
                if await self._auth.refresh():
                    attempt -= 1
                    continue
            await self._backoff_or_raise(call, attempt, response, detail)

    async def _backoff_after_network_error(
        self, call: _Call, attempt: int, error: httpx.TransportError
    ) -> None:
        if not self._can_retry(call, attempt):
            raise Transient(f"{call.method} {call.url} failed: {type(error).__name__}") from error
        await self._sleep(self._retry.backoff(attempt))

    async def _backoff_or_raise(
        self, call: _Call, attempt: int, response: httpx.Response, detail: str
    ) -> None:
        throttled = _throttled(response)
        if not (throttled or self._retry.retryable_status(response.status_code)):
            raise _status_error(response, detail)
        now = self._clock()
        delay = self._retry.delay(attempt, response.headers, now, throttled=throttled)
        if self._can_retry(call, attempt):
            await self._sleep(delay)
            return
        if throttled:
            wait = max(delay, self._retry.max_retry_after)
            raise RateLimited(now + timedelta(seconds=wait), f"still throttled: {detail}")
        raise _status_error(response, detail)

    def _can_retry(self, call: _Call, attempt: int) -> bool:
        idempotent = self._retry.is_idempotent(call.method, read_only=call.read_only)
        return idempotent and attempt < self._retry.attempts

    async def _follow(self, call: _Call) -> httpx.Response:
        """Send ``call``, following redirects under the origin policy."""

        url = self._client.build_request(call.method, call.url).url
        for _ in range(MAX_REDIRECTS + 1):
            response = await self._send_once(call, url)
            if not response.is_redirect:
                return response
            await response.aclose()
            target = response.url.join(response.headers["location"])
            if not same_origin(target, self._base) and not call.allow_cross_origin_redirect:
                raise ConfigError(f"refusing a redirect from {self._base.host} to {target.host}")
            if response.status_code in _REDIRECT_TO_GET and call.method not in ("GET", "HEAD"):
                call = replace(call, method="GET", json=None, data=None)
            call = replace(call, params=None)
            url = target
        raise Transient(f"more than {MAX_REDIRECTS} redirects for {call.url}")

    async def _send_once(self, call: _Call, url: httpx.URL) -> httpx.Response:
        await self._guard.check(url)
        await self._limiter.acquire(call.cost)
        headers = dict(call.headers or {})
        if same_origin(url, self._base):
            headers.update(await self._auth.headers())
        request = self._client.build_request(
            call.method,
            url,
            params=call.params,
            json=call.json,
            data=call.data,
            headers=headers,
        )
        return await self._client.send(request, stream=True)

    async def _read_capped(self, response: httpx.Response) -> bytes:
        declared = response.headers.get("content-length")
        if declared and declared.isdigit() and int(declared) > self._max_bytes:
            raise ResponseTooLarge(self._max_bytes)
        body = bytearray()
        try:
            async for chunk in response.aiter_bytes():
                body.extend(chunk)
                if len(body) > self._max_bytes:
                    raise ResponseTooLarge(self._max_bytes)
        except httpx.TransportError as exc:
            raise Transient(f"reading {response.url} failed: {type(exc).__name__}") from exc
        return bytes(body)

    @staticmethod
    async def _read_prefix(response: httpx.Response) -> bytes:
        body = bytearray()
        try:
            async for chunk in response.aiter_bytes():
                body.extend(chunk)
                if len(body) >= _ERROR_DETAIL_BYTES:
                    break
        except httpx.TransportError:
            pass
        finally:
            await response.aclose()
        return bytes(body)
