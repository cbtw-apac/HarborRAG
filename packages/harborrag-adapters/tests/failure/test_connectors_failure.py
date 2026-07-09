"""Failure and recovery tests for connector clients and the base contract.

These exercise the throttle/retry loops and error-classification branches that
are otherwise only hit under real HTTP failures. Two seams are used:

* the connectors' ``client=`` injection point for behavioral tests, and
* swapping a real ``_Requests*Client.session`` for a scripted ``FakeSession``
  to drive the private ``_request`` retry/rate-limit loop directly.

``time.sleep`` is monkeypatched everywhere a retry could otherwise stall, and
rate limiters use ``time.monotonic`` with a high ``requests_per_minute`` so no
real waiting happens.
"""
from __future__ import annotations

from collections.abc import Iterator

import pytest
from harbor_test_builders import FakeResponse, FakeSession

from harborrag_adapters.connectors.base import BaseConnector
from harborrag_adapters.connectors.exceptions import (
    AuthenticationError,
    FetchError,
    RateLimitError,
)
from harborrag_adapters.connectors.github.config import GitHubRepositoryConfig
from harborrag_adapters.connectors.github.connector import _RequestsGitHubClient
from harborrag_adapters.connectors.jira.config import JiraProjectConfig
from harborrag_adapters.connectors.jira.connector import _RequestsJiraClient
from harborrag_adapters.connectors.sharepoint.config import SharePointSiteConfig
from harborrag_adapters.connectors.sharepoint.connector import _RequestsGraphClient
from harborrag_core.domain.raw_document import RawDocument
from harborrag_core.domain.source import SourceRecord


@pytest.fixture(autouse=True)
def _no_sleep(monkeypatch):
    """Neutralize retry/backoff sleeps in every connector client module."""
    for module in (
        "harborrag_adapters.connectors.github.connector",
        "harborrag_adapters.connectors.jira.connector",
        "harborrag_adapters.connectors.sharepoint.connector",
    ):
        monkeypatch.setattr(f"{module}.time.sleep", lambda *_a, **_k: None)


# ---------------------------------------------------------------------------
# GitHub _rate_limited classification
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "response, expected",
    [
        (FakeResponse(status_code=429), True),
        (
            FakeResponse(status_code=403, headers={"X-RateLimit-Remaining": "0"}),
            True,
        ),
        (FakeResponse(status_code=403, headers={"Retry-After": "5"}), True),
        (
            FakeResponse(status_code=403, text="You have hit a secondary rate limit."),
            True,
        ),
        (
            FakeResponse(status_code=403, text="Abuse detection triggered"),
            True,
        ),
        (FakeResponse(status_code=403, text="Bad credentials"), False),
        (FakeResponse(status_code=200), False),
    ],
)
def test_github_rate_limited_classification(response, expected):
    assert _RequestsGitHubClient._rate_limited(response) is expected


# ---------------------------------------------------------------------------
# GitHub _request retry/rate-limit loop via FakeSession
# ---------------------------------------------------------------------------


def _github_config(**overrides) -> GitHubRepositoryConfig:
    values = {
        "owner": "acme",
        "repo": "harbor-rag",
        "requests_per_minute": 6000,
    }
    values.update(overrides)
    return GitHubRepositoryConfig(**values)


def test_github_retries_after_rate_limit_then_succeeds():
    client = _RequestsGitHubClient(_github_config())
    client.session = FakeSession(
        responses=[
            FakeResponse(status_code=429, text="rate limited"),
            FakeResponse(status_code=200, _json={"ok": True}),
        ]
    )

    payload = client.get_json("repos/acme/harbor-rag")

    assert payload == {"ok": True}
    assert len(client.session.calls) == 2  # retried once


def test_github_plain_403_becomes_authentication_error():
    client = _RequestsGitHubClient(_github_config())
    client.session = FakeSession(
        responses=[FakeResponse(status_code=403, text="Bad credentials")]
    )

    with pytest.raises(AuthenticationError):
        client.get_json("repos/acme/harbor-rag")
    assert len(client.session.calls) == 1  # not retried


def test_github_401_becomes_authentication_error():
    client = _RequestsGitHubClient(_github_config())
    client.session = FakeSession(
        responses=[FakeResponse(status_code=401, text="unauthorized")]
    )
    with pytest.raises(AuthenticationError):
        client.get_json("repos/acme/harbor-rag")


def test_github_rate_limit_exhausts_retries_raises_rate_limit_error():
    client = _RequestsGitHubClient(_github_config(max_retries=2))
    client.session = FakeSession(
        responses=[FakeResponse(status_code=429, text="slow down")] * 3
    )
    with pytest.raises(RateLimitError):
        client.get_json("repos/acme/harbor-rag")
    assert len(client.session.calls) == 3  # max_retries + 1 attempts


# ---------------------------------------------------------------------------
# BaseConnector.load_raw_documents on_error policies
# ---------------------------------------------------------------------------


def _record(record_id: str) -> SourceRecord:
    return SourceRecord(record_id, "text/plain", record_id)


def _doc(record_id: str) -> RawDocument:
    return RawDocument(record_id, record_id, b"data", "text/plain")


class _FlakyConnector(BaseConnector):
    """Yields three records; the middle one fails to load."""

    provider_name = "flaky"

    def __init__(self, fail_with: Exception) -> None:
        self._fail_with = fail_with

    def discover(self, query=None) -> Iterator[SourceRecord]:
        yield _record("a")
        yield _record("boom")
        yield _record("c")

    def load(self, record: SourceRecord) -> RawDocument:
        if record.id == "boom":
            raise self._fail_with
        return _doc(record.id)


def test_load_raw_documents_skip_isolates_failed_record():
    connector = _FlakyConnector(FetchError("transient"))
    docs = list(connector.load_raw_documents(on_error="skip"))
    assert [doc.id for doc in docs] == ["a", "c"]


def test_load_raw_documents_raise_propagates_first_failure():
    connector = _FlakyConnector(FetchError("transient"))
    with pytest.raises(FetchError):
        list(connector.load_raw_documents(on_error="raise"))


def test_load_raw_documents_authentication_error_always_propagates():
    connector = _FlakyConnector(AuthenticationError("bad token"))
    with pytest.raises(AuthenticationError):
        list(connector.load_raw_documents(on_error="skip"))


def test_load_raw_documents_rejects_unknown_policy():
    connector = _FlakyConnector(FetchError("x"))
    with pytest.raises(ValueError, match="on_error"):
        list(connector.load_raw_documents(on_error="bogus"))


# ---------------------------------------------------------------------------
# Streaming cap enforcement through a real client + FakeSession
# ---------------------------------------------------------------------------


def _sharepoint_config(**overrides) -> SharePointSiteConfig:
    values = {
        "site_id": "site-123",
        "access_token": "test-token",
        "requests_per_minute": 6000,
        "max_file_size_bytes": 10,
    }
    values.update(overrides)
    return SharePointSiteConfig(**values)


def test_sharepoint_get_bytes_oversized_stream_raises_fetch_error():
    client = _RequestsGraphClient(_sharepoint_config())
    client.session = FakeSession(
        responses=[FakeResponse(status_code=200, _chunks=[b"x" * 50])]
    )
    with pytest.raises(FetchError):
        client.get_bytes("drives/d/items/i/content")


def test_sharepoint_get_bytes_rejects_oversized_content_length_before_read():
    # A declared Content-Length over the configured cap (10 bytes) is rejected
    # up front and surfaced as FetchError, without streaming the body.
    client = _RequestsGraphClient(_sharepoint_config())
    client.session = FakeSession(
        responses=[
            FakeResponse(
                status_code=200,
                headers={"Content-Length": "9999"},
                _chunks=[b"small"],
            )
        ]
    )
    with pytest.raises(FetchError):
        client.get_bytes("drives/d/items/i/content")


# ---------------------------------------------------------------------------
# JIRA _request retry loop: recovery and exhaustion
# ---------------------------------------------------------------------------


def _jira_config(**overrides) -> JiraProjectConfig:
    values = {
        "base_url": "https://jira.example.com",
        "token": "secret-token",
        "email": "user@example.com",
        "deployment_type": "cloud",
        "requests_per_minute": 6000,
    }
    values.update(overrides)
    return JiraProjectConfig(**values)


def test_jira_retries_transient_500_then_succeeds():
    client = _RequestsJiraClient(_jira_config())
    client.session = FakeSession(
        responses=[
            FakeResponse(status_code=500, text="server error"),
            FakeResponse(status_code=200, _json={"issues": []}),
        ]
    )

    payload = client.get_json("search")

    assert payload == {"issues": []}
    assert len(client.session.calls) == 2


def test_jira_retry_exhaustion_raises_fetch_error():
    client = _RequestsJiraClient(_jira_config(max_retries=2))
    client.session = FakeSession(
        responses=[FakeResponse(status_code=503, text="unavailable")] * 3
    )
    with pytest.raises(FetchError):
        client.get_json("search")
    assert len(client.session.calls) == 3


def test_jira_403_is_authentication_error_not_retried():
    client = _RequestsJiraClient(_jira_config())
    client.session = FakeSession(
        responses=[FakeResponse(status_code=403, text="forbidden")]
    )
    with pytest.raises(AuthenticationError):
        client.get_json("search")
    assert len(client.session.calls) == 1


def test_jira_download_bytes_oversized_stream_raises_fetch_error():
    client = _RequestsJiraClient(_jira_config(max_attachment_size_bytes=10))
    client.session = FakeSession(
        responses=[FakeResponse(status_code=200, _chunks=[b"x" * 50])]
    )
    with pytest.raises(FetchError):
        client.download_bytes("https://jira.example.com/secure/attachment/1")


def test_jira_download_bytes_rejects_cross_origin_url():
    client = _RequestsJiraClient(_jira_config())
    # No session interaction expected: origin check fails before the request.
    client.session = FakeSession(responses=[])
    with pytest.raises(FetchError):
        client.download_bytes("https://evil.example.com/secure/attachment/1")
    assert client.session.calls == []
