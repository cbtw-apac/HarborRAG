"""A GitHub rate limit that outlasts the retries keeps its status and stays retryable."""

from __future__ import annotations

import time

import pytest
from harbor_test_builders import FakeResponse, FakeSession

from harborrag_adapters.connectors.exceptions import RateLimitError
from harborrag_adapters.connectors.github.client import _RequestsGitHubClient
from harborrag_adapters.connectors.github.config import GitHubRepositoryConfig

pytestmark = [pytest.mark.unit, pytest.mark.graybox]


@pytest.fixture(autouse=True)
def _no_sleep(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(time, "sleep", lambda *_a, **_k: None)


def _client(*responses: FakeResponse) -> _RequestsGitHubClient:
    client = _RequestsGitHubClient(
        GitHubRepositoryConfig(
            owner="demo", repo="engineering", token="t", requests_per_minute=6000, max_retries=1
        )
    )
    client.session = FakeSession(responses=list(responses))
    return client


def test_github_429_after_the_retries_carries_status_429_and_stays_retryable() -> None:
    client = _client(
        FakeResponse(status_code=429, headers={"Retry-After": "1"}, text=""),
        FakeResponse(status_code=429, headers={"Retry-After": "1"}, text="slow down"),
    )

    with pytest.raises(RateLimitError) as raised:
        client.get_json("repos/demo/engineering")

    assert raised.value.status_code == 429
    assert raised.value.retryable is True


def test_github_403_secondary_limit_carries_status_403_and_stays_retryable() -> None:
    client = _client(
        FakeResponse(status_code=403, text="You have exceeded a secondary rate limit"),
        FakeResponse(status_code=403, text="You have exceeded a secondary rate limit"),
    )

    with pytest.raises(RateLimitError) as raised:
        client.get_json("repos/demo/engineering")

    assert raised.value.status_code == 403
    assert raised.value.retryable is True
