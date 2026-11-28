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

    def __init__(
        self,
        script: list[Any],
        *,
        policy: RetryPolicy,
        refreshable: bool = False,
        secrets: tuple[str, ...] = (),
    ):
        super().__init__(label="Example", logger=logging.getLogger("test"))
        self.script = script
        self.policy = policy
        self.refreshable = refreshable
        self.secrets = secrets
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

    def _known_secrets(self) -> tuple[str, ...]:
        return self.secrets


class _BodySignalledLimitClient(_ScriptedClient):
    """Treats a 403 whose body says "slow down" as a rate limit."""

    def _rate_limited(self, response: Any, detail: str) -> bool:
        return response.status_code == 429 or "slow down" in detail


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


def test_a_connection_failure_never_carries_the_request_url() -> None:
    signed = "https://example.com/file?signature=synthetic-signature-value"
    client = _client([requests.ConnectionError(f"Max retries exceeded with url: {signed}")] * 3)

    with pytest.raises(FetchError) as raised:
        client.request("GET", signed)

    assert "synthetic-signature-value" not in str(raised.value)
    assert raised.value.__cause__ is None
    assert raised.value.__suppress_context__ is True


def test_error_text_never_shows_a_known_secret() -> None:
    secret = "token-ExampleValue42"
    client = _client([FakeResponse(status_code=400, text=f"rejected {secret}")], secrets=(secret,))

    with pytest.raises(FetchError) as raised:
        client.request("GET", "https://example.com/x")

    assert secret not in str(raised.value)
    assert raised.value.status_code == 400


def test_a_refused_body_is_read_once_for_the_rate_limit_check_and_the_error() -> None:
    policy = RetryPolicy(max_retries=1, backoff_factor=0.0)
    client = _BodySignalledLimitClient(
        [
            FakeResponse(status_code=403, text="slow down"),
            FakeResponse(status_code=403, text="slow down"),
        ],
        policy=policy,
    )

    with pytest.raises(RateLimitError) as raised:
        client.request("GET", "https://example.com/x")

    assert client.attempts == 2
    assert raised.value.status_code == 403
    assert "slow down" in str(raised.value)


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
