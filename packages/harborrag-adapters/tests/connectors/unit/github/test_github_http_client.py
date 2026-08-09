"""Unit tests for the real GitHub HTTP client wrapper with fake sessions."""

from __future__ import annotations

import pytest
from harbor_test_builders import FakeResponse, FakeSession

pytestmark = [pytest.mark.unit, pytest.mark.graybox]


@pytest.fixture(autouse=True)
def _no_sleep(monkeypatch):
    import time

    monkeypatch.setattr(time, "sleep", lambda *_a, **_k: None)


def test_github_client_close_closes_session():
    from harborrag_adapters.connectors.github.config import GitHubRepositoryConfig
    from harborrag_adapters.connectors.github.connector import _RequestsGitHubClient

    cfg = GitHubRepositoryConfig(owner="o", repo="r", token="t")
    client = _RequestsGitHubClient(cfg)
    closed = []
    client.session.close = lambda: closed.append(True)

    client.close()

    assert closed == [True]


def test_github_client_context_manager_closes_session():
    from harborrag_adapters.connectors.github.config import GitHubRepositoryConfig
    from harborrag_adapters.connectors.github.connector import _RequestsGitHubClient

    cfg = GitHubRepositoryConfig(owner="o", repo="r", token="t")
    closed = []
    with _RequestsGitHubClient(cfg) as client:
        client.session.close = lambda: closed.append(True)

    assert closed == [True]


def test_github_api_url_rejects_cross_origin_absolute():
    from harborrag_adapters.connectors.exceptions import FetchError
    from harborrag_adapters.connectors.github.config import GitHubRepositoryConfig
    from harborrag_adapters.connectors.github.connector import _RequestsGitHubClient

    cfg = GitHubRepositoryConfig(owner="o", repo="r", token="t")
    client = _RequestsGitHubClient(cfg)
    assert client._api_url("repos/o/r/git/trees/main").startswith(cfg.api_url)
    with pytest.raises(FetchError):
        client._api_url("https://evil.example.com/x")


def _github_client(**overrides):
    from harborrag_adapters.connectors.github.config import GitHubRepositoryConfig
    from harborrag_adapters.connectors.github.connector import _RequestsGitHubClient

    values = {
        "owner": "o",
        "repo": "r",
        "token": "t",
        "requests_per_minute": 6000,
        "max_retries": 1,
        "backoff_factor": 0.01,
    }
    values.update(overrides)
    return _RequestsGitHubClient(GitHubRepositoryConfig(**values))


def test_github_get_json_decodes_dict_and_list_payloads():
    client = _github_client()
    client.session = FakeSession(
        responses=[
            FakeResponse(status_code=200, _json={"a": 1}),
            FakeResponse(status_code=200, _json=[{"a": 1}, {"b": 2}]),
        ]
    )
    assert client.get_json("repos/o/r") == {"a": 1}
    assert client.get_json("repos/o/r/git/trees/x") == [{"a": 1}, {"b": 2}]


def test_github_get_json_rejects_non_json_body():
    from harborrag_adapters.connectors.exceptions import FetchError

    client = _github_client()
    client.session = FakeSession(
        responses=[FakeResponse(status_code=200, text="<html>nope</html>")]
    )
    with pytest.raises(FetchError, match="non-JSON"):
        client.get_json("repos/o/r")


def test_github_get_json_reports_blob_specific_response_cap() -> None:
    from harborrag_adapters.connectors.exceptions import FetchError

    client = _github_client()
    response = FakeResponse(status_code=200, _json={"content": "too large"})
    client.session = FakeSession(responses=[response])

    with pytest.raises(FetchError, match="exceeded byte limit"):
        client.get_json("repos/o/r/git/blobs/sha", max_bytes=4)
    assert response.closed is True


def test_github_get_json_rejects_list_with_non_dict_item():
    from harborrag_adapters.connectors.exceptions import FetchError

    client = _github_client()
    client.session = FakeSession(responses=[FakeResponse(status_code=200, _json=[1, 2])])
    with pytest.raises(FetchError, match="invalid JSON"):
        client.get_json("repos/o/r/git/trees/x")


def test_github_get_json_rejects_scalar_payload():
    from harborrag_adapters.connectors.exceptions import FetchError

    client = _github_client()
    client.session = FakeSession(responses=[FakeResponse(status_code=200, _json=5)])
    with pytest.raises(FetchError, match="invalid JSON"):
        client.get_json("repos/o/r")


def test_github_request_raises_authentication_error_on_401():
    from harborrag_adapters.connectors.exceptions import AuthenticationError

    client = _github_client()
    client.session = FakeSession(responses=[FakeResponse(status_code=401, text="bad token")])
    with pytest.raises(AuthenticationError):
        client.get_json("repos/o/r")


def test_github_request_raises_authentication_error_on_plain_403():
    from harborrag_adapters.connectors.exceptions import AuthenticationError

    client = _github_client()
    client.session = FakeSession(
        responses=[FakeResponse(status_code=403, text="forbidden", headers={})]
    )
    with pytest.raises(AuthenticationError):
        client.get_json("repos/o/r")


def test_github_request_raises_rate_limit_error_after_exhausting_retries():
    from harborrag_adapters.connectors.exceptions import RateLimitError

    client = _github_client(max_retries=1)
    client.session = FakeSession(
        responses=[
            FakeResponse(
                status_code=403,
                text="secondary rate limit exceeded",
                headers={},
            ),
            FakeResponse(
                status_code=403,
                headers={"X-RateLimit-Remaining": "0"},
                text="",
            ),
        ]
    )
    with pytest.raises(RateLimitError):
        client.get_json("repos/o/r")


def test_github_request_retries_429_then_succeeds():
    client = _github_client(max_retries=1)
    retry_response = FakeResponse(status_code=429, headers={"Retry-After": "1"}, text="")
    client.session = FakeSession(
        responses=[
            retry_response,
            FakeResponse(status_code=200, _json={"ok": True}),
        ]
    )
    assert client.get_json("repos/o/r") == {"ok": True}
    assert retry_response.closed is True


def test_github_request_retries_5xx_then_succeeds():
    client = _github_client(max_retries=2, backoff_factor=0)
    retry_response = FakeResponse(status_code=500, text="boom", headers={})
    client.session = FakeSession(
        responses=[
            retry_response,
            FakeResponse(status_code=200, _json={"ok": True}),
        ]
    )
    assert client.get_json("repos/o/r") == {"ok": True}
    assert retry_response.closed is True


def test_github_request_raises_fetch_error_on_non_retryable_4xx():
    from harborrag_adapters.connectors.exceptions import FetchError

    client = _github_client()
    client.session = FakeSession(
        responses=[FakeResponse(status_code=404, text="missing", headers={})]
    )
    with pytest.raises(FetchError, match="404"):
        client.get_json("repos/o/r/nope")


def test_github_request_retries_connection_errors_then_succeeds():
    import requests

    client = _github_client(max_retries=1)
    client.session = FakeSession(
        responses=[
            requests.ConnectionError("boom"),
            FakeResponse(status_code=200, _json={"ok": True}),
        ]
    )
    assert client.get_json("repos/o/r") == {"ok": True}


def test_github_request_raises_fetch_error_after_exhausting_connection_errors():
    import requests

    from harborrag_adapters.connectors.exceptions import FetchError

    client = _github_client(max_retries=0)
    client.session = FakeSession(responses=[requests.ConnectionError("boom")])
    with pytest.raises(FetchError, match="GitHub request failed") as captured:
        client.get_json("repos/o/r")
    assert "boom" not in str(captured.value)


def test_github_client_omits_authorization_header_without_token(monkeypatch):
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    client = _github_client(token=None)
    assert "Authorization" not in client.session.headers


def test_github_config_rejects_negative_max_retries():
    with pytest.raises(ValueError, match="max_retries"):
        _github_client(max_retries=-1)


def test_github_rate_limited_static_predicate_branches():
    from harborrag_adapters.connectors.github.connector import _RequestsGitHubClient

    assert _RequestsGitHubClient._rate_limited(FakeResponse(status_code=429, headers={}, text=""))
    assert not _RequestsGitHubClient._rate_limited(
        FakeResponse(status_code=200, headers={}, text="")
    )
    assert _RequestsGitHubClient._rate_limited(
        FakeResponse(status_code=403, headers={"X-RateLimit-Remaining": "0"}, text="")
    )
    assert _RequestsGitHubClient._rate_limited(
        FakeResponse(status_code=403, headers={"Retry-After": "5"}, text="")
    )
    assert _RequestsGitHubClient._rate_limited(
        FakeResponse(status_code=403, headers={}, text="Abuse Detection triggered")
    )
    assert not _RequestsGitHubClient._rate_limited(
        FakeResponse(status_code=403, headers={}, text="just forbidden")
    )


def test_github_acquire_sleeps_when_requests_arrive_faster_than_budget(monkeypatch):
    import time

    sleeps: list[float] = []
    monkeypatch.setattr(time, "sleep", lambda seconds: sleeps.append(seconds))

    client = _github_client(requests_per_minute=1)
    client.session = FakeSession(
        responses=[
            FakeResponse(status_code=200, _json={"a": 1}),
            FakeResponse(status_code=200, _json={"b": 2}),
        ]
    )
    client.get_json("repos/o/r")
    client.get_json("repos/o/r")

    assert any(seconds > 0 for seconds in sleeps)
