"""Shared fakes for kit tests: no DNS, no sleeping, no network."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from datetime import UTC, datetime
from typing import Any

import httpx

from harborrag_adapters.sources._kit.http import HttpxSourceClient
from harborrag_adapters.sources._kit.net import SsrfGuard
from harborrag_adapters.sources._kit.retry import RetryPolicy

NOW = datetime(2026, 10, 12, 9, 0, tzinfo=UTC)
PUBLIC_IP = "93.184.216.34"


async def public_resolver(host: str, port: int) -> list[str]:
    del host, port
    return [PUBLIC_IP]


class FakeAuth:
    """Bearer auth whose token changes on every refresh."""

    def __init__(self, *, supports_refresh: bool = True, refresh_changes: bool = True) -> None:
        self.supports_refresh = supports_refresh
        self.refresh_changes = refresh_changes
        self.token = "token-0"
        self.refreshes = 0

    async def headers(self) -> Mapping[str, str]:
        return {"Authorization": f"Bearer {self.token}"}

    async def refresh(self) -> bool:
        self.refreshes += 1
        if self.refresh_changes:
            self.token = f"token-{self.refreshes}"
        return self.refresh_changes


class Sleeps:
    def __init__(self) -> None:
        self.calls: list[float] = []

    async def __call__(self, seconds: float) -> None:
        self.calls.append(seconds)


def make_client(  # noqa: PLR0913 - keyword-only test seams
    handler: Callable[[httpx.Request], Any],
    *,
    auth: FakeAuth | None = None,
    retry: RetryPolicy | None = None,
    sleeps: Sleeps | None = None,
    guard: SsrfGuard | None = None,
    max_response_bytes: int = 1024 * 1024,
    base_url: str = "https://jira.example.com",
) -> HttpxSourceClient:
    return HttpxSourceClient(
        base_url=base_url,
        auth=auth or FakeAuth(),
        retry=retry or RetryPolicy(rng=lambda: 1.0),
        guard=guard or SsrfGuard(resolver=public_resolver),
        max_response_bytes=max_response_bytes,
        transport=httpx.MockTransport(handler),
        sleep=sleeps or Sleeps(),
        clock=lambda: NOW,
    )
