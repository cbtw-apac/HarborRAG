from __future__ import annotations

import logging
from typing import Any

import pytest
import requests
from harbor_test_builders import FakeResponse

from harborrag_adapters.connectors._http import RetryingHttpClient, RetryPolicy
from harborrag_adapters.connectors.exceptions import (
    AuthenticationError,
    AuthorizationError,
    FetchError,
    ItemNotFoundError,
    RateLimitError,
)

pytestmark = pytest.mark.unit


class _ScriptedClient(RetryingHttpClient):
    """Replays responses or exceptions, one per attempt."""

    def __init__(self, script: list[Any], *, policy: RetryPolicy, refreshable: bool = False):
        super().__init__(label="Example", logger=logging.getLogger("test"))
        self.script = script
        self.policy = policy
        self.refreshable = refreshable
        self.attempts = 0
        self.refreshes = 0

    def _retry_policy(self) -> RetryPolicy:
        return self.policy

    def _send(self, method: str, url: str, **options: Any) -> Any:
        self.attempts += 1
        step = self.script.pop(0)
        if isinstance(step, Exception):
            raise step
        return step

    def _refresh_credentials(self) -> bool:
        self.refreshes += 1
        return self.refreshable


@pytest.fixture(autouse=True)
def _no_sleep(monkeypatch) -> None:
    import time

    monkeypatch.setattr(time, "sleep", lambda seconds: None)


def _client(script: list[Any], **options: Any) -> _ScriptedClient:
    policy = options.pop("policy", RetryPolicy(max_retries=2, backoff_factor=0.0))
    return _ScriptedClient(script, policy=policy, **options)


def test_transient_failures_are_retried_until_a_response_succeeds() -> None:
    client = _client(
        [requests.ConnectionError("reset"), FakeResponse(status_code=503), FakeResponse()]
    )

    assert client.request("GET", "https://example.com/x").status_code == 200
    assert client.attempts == 3


@pytest.mark.parametrize(
    ("status", "error_type"),
    [
        (401, AuthenticationError),
        (403, AuthorizationError),
        (404, ItemNotFoundError),
        (400, FetchError),
    ],
)
def test_a_permanent_status_raises_its_error_without_retrying(
    status: int, error_type: type[Exception]
) -> None:
    client = _client([FakeResponse(status_code=status, text="no")])

    with pytest.raises(error_type) as raised:
        client.request("GET", "https://example.com/x")

    assert getattr(raised.value, "status_code", None) == status
    assert client.attempts == 1


def test_rate_limiting_after_the_last_retry_is_a_rate_limit_error() -> None:
    client = _client([FakeResponse(status_code=429)] * 3)

    with pytest.raises(RateLimitError):
        client.request("GET", "https://example.com/x")

    assert client.attempts == 3


def test_connection_errors_after_the_last_retry_are_a_fetch_error() -> None:
    client = _client([requests.ConnectionError("reset")] * 3)

    with pytest.raises(FetchError, match="Example request failed"):
        client.request("GET", "https://example.com/x")


def test_a_401_is_resent_once_with_fresh_credentials_without_using_a_retry() -> None:
    policy = RetryPolicy(max_retries=0, backoff_factor=0.0)
    client = _client(
        [FakeResponse(status_code=401), FakeResponse()], policy=policy, refreshable=True
    )

    assert client.request("GET", "https://example.com/x").status_code == 200
    assert client.refreshes == 1


def test_a_second_401_after_refreshing_is_an_authentication_error() -> None:
    client = _client(
        [FakeResponse(status_code=401), FakeResponse(status_code=401)], refreshable=True
    )

    with pytest.raises(AuthenticationError):
        client.request("GET", "https://example.com/x")

    assert client.refreshes == 1


def test_a_retry_after_beyond_the_policy_ends_the_request() -> None:
    policy = RetryPolicy(max_retries=3, backoff_factor=0.0, max_retry_after_seconds=60)
    client = _client([FakeResponse(status_code=429, headers={"Retry-After": "120"})], policy=policy)

    with pytest.raises(RateLimitError):
        client.request("GET", "https://example.com/x")

    assert client.attempts == 1
