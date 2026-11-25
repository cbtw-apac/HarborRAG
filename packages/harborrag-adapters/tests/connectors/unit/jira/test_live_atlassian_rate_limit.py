"""An Atlassian 429 that outlasts the retries carries its status and stays retryable."""

from __future__ import annotations

import time

import pytest
from harbor_test_builders import FakeResponse, FakeSession
from jira_http_test_helpers import jira_client

from harborrag_adapters.connectors.exceptions import RateLimitError

pytestmark = [pytest.mark.unit, pytest.mark.graybox]


@pytest.fixture(autouse=True)
def _no_sleep(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(time, "sleep", lambda *_a, **_k: None)


def test_atlassian_429_after_the_retries_carries_status_429_and_stays_retryable() -> None:
    client = jira_client(max_retries=0)
    client.session = FakeSession(
        responses=[FakeResponse(status_code=429, headers={}, text="slow down")]
    )

    with pytest.raises(RateLimitError) as raised:
        client.get_json("issue/ENG-1")

    assert raised.value.status_code == 429
    assert raised.value.retryable is True
