"""GitHub error bodies are read once, and 403 and 404 map to permanent errors."""

from __future__ import annotations

import base64
import io
import logging
from typing import Any

import pytest
import requests
from github_test_helpers import FakeGitHubClient, add_repo_and_commit, config, tree_item
from requests.structures import CaseInsensitiveDict

from harborrag_adapters.connectors import GitHubConnector
from harborrag_adapters.connectors.exceptions import (
    AuthorizationError,
    ItemNotFoundError,
    RateLimitError,
)
from harborrag_adapters.connectors.github.client import _RequestsGitHubClient
from harborrag_adapters.connectors.local.skips import path_digest
from harborrag_core.domain.source import SourceRecord

pytestmark = pytest.mark.unit

API_URL = "https://api.github.com/repos/acme/harbor-rag"
SYNTHETIC_PATH = "quarterly-plan.md"


def _streamed_response(status_code: int, body: bytes, headers: dict[str, str] | None = None):
    """Return a real ``requests.Response`` whose body can be read only once."""
    response = requests.Response()
    response.status_code = status_code
    response.raw = io.BytesIO(body)
    response.headers = CaseInsensitiveDict(headers or {})
    response.url = API_URL
    return response


class _ScriptedSession:
    """Session stand-in that answers each request with the next scripted response."""

    def __init__(self, *responses: requests.Response) -> None:
        self.responses = list(responses)
        self.calls = 0

    def request(self, *_args: Any, **_kwargs: Any) -> requests.Response:
        self.calls += 1
        return self.responses.pop(0)


def _client(*responses: requests.Response, max_retries: int = 0) -> _RequestsGitHubClient:
    client = _RequestsGitHubClient(config(max_retries=max_retries, backoff_factor=0))
    client.session = _ScriptedSession(*responses)  # type: ignore[assignment]
    return client


def test_permission_403_raises_authorization_error_with_the_body() -> None:
    body = b'{"message":"Resource not accessible by personal access token"}'
    client = _client(_streamed_response(403, body))

    with pytest.raises(AuthorizationError) as raised:
        client.get_json("repos/acme/harbor-rag")

    assert raised.value.status_code == 403
    assert "Resource not accessible" in str(raised.value)


def test_secondary_rate_limit_on_the_last_attempt_stays_retryable() -> None:
    body = b'{"message":"You have exceeded a secondary rate limit."}'
    client = _client(_streamed_response(403, body))

    with pytest.raises(RateLimitError) as raised:
        client.get_json("repos/acme/harbor-rag")

    assert raised.value.retryable is True
    assert "secondary rate limit" in str(raised.value)


def test_secondary_rate_limit_is_retried() -> None:
    client = _client(
        _streamed_response(403, b'{"message":"You have exceeded a secondary rate limit."}'),
        _streamed_response(200, b'{"ok": true}'),
        max_retries=1,
    )

    assert client.get_json("repos/acme/harbor-rag") == {"ok": True}


def test_404_raises_item_not_found() -> None:
    client = _client(_streamed_response(404, b'{"message":"Not Found"}'))

    with pytest.raises(ItemNotFoundError) as raised:
        client.get_json("repos/acme/harbor-rag/contents/missing.md")

    assert raised.value.status_code == 404
    assert raised.value.retryable is False


def test_load_logs_the_path_digest_not_the_path(caplog: pytest.LogCaptureFixture) -> None:
    client = FakeGitHubClient()
    add_repo_and_commit(client)
    client.add(
        "repos/acme/harbor-rag/git/trees/tree-root",
        {"sha": "tree-root", "truncated": False, "tree": [tree_item(SYNTHETIC_PATH, "sha-plan")]},
    )
    content = b"# Plan"
    client.add(
        "repos/acme/harbor-rag/git/blobs/sha-plan",
        {
            "sha": "sha-plan",
            "size": len(content),
            "encoding": "base64",
            "content": base64.b64encode(content).decode("ascii"),
        },
    )
    connector = GitHubConnector(config(), client=client)
    record = SourceRecord(
        f"github://acme/harbor-rag/{SYNTHETIC_PATH}",
        "text/markdown",
        SYNTHETIC_PATH,
        metadata={"path": SYNTHETIC_PATH, "sha": "sha-plan", "size": len(content)},
        checksum="sha-plan",
    )

    with caplog.at_level(logging.INFO, logger="harborrag.adapters.connectors.github"):
        document = connector.load(record)

    assert document.content == content
    assert f"path_digest={path_digest(SYNTHETIC_PATH)}" in caplog.text
    assert "quarterly-plan" not in caplog.text
