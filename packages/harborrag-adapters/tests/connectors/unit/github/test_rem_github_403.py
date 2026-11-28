"""A GitHub 403 reads its streamed body once, as a real ``requests.Response`` allows."""

from __future__ import annotations

import io
import json

import pytest
import requests
from harbor_test_builders import FakeResponse, FakeSession
from requests.structures import CaseInsensitiveDict

from harborrag_adapters.connectors.exceptions import AuthorizationError, RateLimitError
from harborrag_adapters.connectors.github.config import GitHubRepositoryConfig
from harborrag_adapters.connectors.github.connector import _RequestsGitHubClient

pytestmark = [pytest.mark.unit, pytest.mark.graybox]


@pytest.fixture(autouse=True)
def _no_sleep(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "harborrag_adapters.connectors._http.client.time.sleep", lambda *_a, **_k: None
    )


def _streamed(status: int, body: bytes, headers: dict[str, str] | None = None) -> requests.Response:
    """Build a real response whose body streams from ``raw`` and can be read once."""
    response = requests.Response()
    response.status_code = status
    response.headers = CaseInsensitiveDict(headers or {})
    response.raw = io.BytesIO(body)
    response.url = "https://api.github.com/repos/demo-org/demo-repo"
    return response


def _client(*responses: requests.Response, max_retries: int = 0) -> _RequestsGitHubClient:
    client = _RequestsGitHubClient(
        GitHubRepositoryConfig(
            owner="demo-org",
            repo="demo-repo",
            token="t",
            requests_per_minute=6000,
            max_retries=max_retries,
            backoff_factor=0,
        )
    )
    client.session = FakeSession(responses=list(responses))
    return client


@pytest.mark.parametrize(
    "message",
    [
        "Resource not accessible by personal access token",
        "Resource protected by organization SAML enforcement.",
        "The organization has an IP allow list enabled.",
    ],
    ids=["token-scope", "saml", "ip-allowlist"],
)
def test_github_plain_403_on_a_real_response_raises_authorization_error(message: str) -> None:
    client = _client(_streamed(403, json.dumps({"message": message}).encode()))

    with pytest.raises(AuthorizationError) as raised:
        client.get_json("repos/demo-org/demo-repo")

    assert raised.value.status_code == 403
    assert message in str(raised.value)


def test_github_secondary_limit_on_the_final_attempt_stays_retryable() -> None:
    body = b'{"message": "You have exceeded a secondary rate limit."}'
    client = _client(_streamed(403, body))

    with pytest.raises(RateLimitError) as raised:
        client.get_json("repos/demo-org/demo-repo")

    assert raised.value.retryable is True
    assert raised.value.status_code == 403
    assert "secondary rate limit" in str(raised.value)


def test_github_secondary_limit_then_success_on_real_responses() -> None:
    client = _client(
        _streamed(403, b'{"message": "You have exceeded a secondary rate limit."}'),
        _streamed(200, b'{"full_name": "demo-org/demo-repo"}'),
        max_retries=1,
    )

    assert client.get_json("repos/demo-org/demo-repo") == {"full_name": "demo-org/demo-repo"}


@pytest.mark.parametrize("read", ["text", "content", "json"])
def test_fake_response_refuses_a_body_read_after_streaming(read: str) -> None:
    response = FakeResponse(status_code=403, text="forbidden")
    assert b"".join(response.iter_content()) == b"forbidden"

    with pytest.raises(RuntimeError, match="already consumed"):
        value = getattr(response, read)
        if callable(value):
            value()
