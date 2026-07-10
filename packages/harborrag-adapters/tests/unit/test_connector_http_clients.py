"""Unit tests for real connector HTTP client wrappers with fake sessions."""
from __future__ import annotations

import pytest

from harbor_test_builders import FakeResponse, FakeSession


pytestmark = [pytest.mark.unit, pytest.mark.graybox]


@pytest.fixture(autouse=True)
def _no_sleep(monkeypatch):
    import time

    monkeypatch.setattr(time, "sleep", lambda *_a, **_k: None)


def _confluence_client():
    from harborrag_adapters.connectors.confluence.config import ConfluenceSpaceConfig
    from harborrag_adapters.connectors.confluence.connector import (
        _RequestsConfluenceClient,
    )

    cfg = ConfluenceSpaceConfig(
        space_key="ENG",
        base_url="https://ex.atlassian.net/wiki",
        token="t",
        email="a@b.c",
        requests_per_minute=6000,
    )
    return _RequestsConfluenceClient(cfg)


def test_confluence_get_json_decodes_and_rejects_non_json():
    from harborrag_adapters.connectors.exceptions import FetchError

    client = _confluence_client()
    client.session = FakeSession(
        responses=[
            FakeResponse(status_code=200, _json={"ok": True}),
            FakeResponse(status_code=200, text="<html>not json</html>"),
        ]
    )
    assert client.get_json("content/search") == {"ok": True}
    with pytest.raises(FetchError, match="non-JSON"):
        client.get_json("content/search")


def test_confluence_get_json_rejects_non_dict_payload():
    from harborrag_adapters.connectors.exceptions import FetchError

    client = _confluence_client()
    client.session = FakeSession(responses=[FakeResponse(status_code=200, _json=[1, 2])])
    with pytest.raises(FetchError, match="invalid JSON"):
        client.get_json("content/search")


def test_confluence_download_bytes_rejects_cross_origin():
    from harborrag_adapters.connectors.exceptions import FetchError

    client = _confluence_client()
    with pytest.raises(FetchError, match="origin|scheme"):
        client.download_bytes("https://evil.example.com/secret")


def test_confluence_download_bytes_streams_capped_body():
    client = _confluence_client()
    client.config.max_attachment_size_bytes = 1024
    client.session = FakeSession(
        responses=[FakeResponse(status_code=200, _chunks=[b"hello ", b"world"])]
    )
    assert client.download_bytes("https://ex.atlassian.net/wiki/download/a") == (
        b"hello world"
    )


def test_confluence_download_bytes_returns_none_for_empty_body():
    client = _confluence_client()
    client.session = FakeSession(responses=[FakeResponse(status_code=200, _chunks=[])])
    assert client.download_bytes("https://ex.atlassian.net/wiki/download/a") is None


def test_confluence_download_bytes_raises_fetch_error_when_body_too_large():
    from harborrag_adapters.connectors.exceptions import FetchError

    client = _confluence_client()
    client.config.max_attachment_size_bytes = 4
    client.session = FakeSession(
        responses=[FakeResponse(status_code=200, _chunks=[b"way too big"])]
    )
    with pytest.raises(FetchError, match="exceeds cap"):
        client.download_bytes("https://ex.atlassian.net/wiki/download/a")


def test_confluence_session_uses_bearer_auth_for_datacenter():
    from harborrag_adapters.connectors.confluence.config import (
        ConfluenceDeploymentType,
        ConfluenceSpaceConfig,
    )
    from harborrag_adapters.connectors.confluence.connector import (
        _RequestsConfluenceClient,
    )

    cfg = ConfluenceSpaceConfig(
        space_key="ENG",
        base_url="https://confluence.local",
        token="pat",
        deployment_type=ConfluenceDeploymentType.DATACENTER,
        requests_per_minute=6000,
    )
    client = _RequestsConfluenceClient(cfg)
    assert client.session.auth is None
    assert client.session.headers["Authorization"] == "Bearer pat"


def test_confluence_request_raises_authentication_error_on_401():
    from harborrag_adapters.connectors.exceptions import AuthenticationError

    client = _confluence_client()
    client.session = FakeSession(
        responses=[FakeResponse(status_code=401, text="bad token")]
    )
    with pytest.raises(AuthenticationError):
        client.get_json("content/search")


def test_confluence_request_raises_rate_limit_error_after_exhausting_retries():
    from harborrag_adapters.connectors.exceptions import RateLimitError

    client = _confluence_client()
    client.config.max_retries = 0
    client.session = FakeSession(
        responses=[FakeResponse(status_code=429, headers={}, text="slow down")]
    )
    with pytest.raises(RateLimitError):
        client.get_json("content/search")


def test_confluence_request_retries_5xx_then_succeeds():
    client = _confluence_client()
    client.config.max_retries = 1
    client.config.backoff_factor = 0.0
    client.session = FakeSession(
        responses=[
            FakeResponse(status_code=500, text="boom", headers={}),
            FakeResponse(status_code=200, _json={"ok": True}),
        ]
    )
    assert client.get_json("content/search") == {"ok": True}


def test_confluence_request_raises_fetch_error_on_non_retryable_4xx():
    from harborrag_adapters.connectors.exceptions import FetchError

    client = _confluence_client()
    client.session = FakeSession(
        responses=[FakeResponse(status_code=404, text="missing", headers={})]
    )
    with pytest.raises(FetchError, match="404"):
        client.get_json("content/nope")


def test_confluence_request_retries_connection_errors_then_succeeds():
    import requests

    client = _confluence_client()
    client.config.max_retries = 1
    client.session = FakeSession(
        responses=[
            requests.ConnectionError("boom"),
            FakeResponse(status_code=200, _json={"ok": True}),
        ]
    )
    assert client.get_json("content/search") == {"ok": True}


def test_confluence_request_raises_fetch_error_after_exhausting_connection_errors():
    import requests

    from harborrag_adapters.connectors.exceptions import FetchError

    client = _confluence_client()
    client.config.max_retries = 0
    client.session = FakeSession(responses=[requests.ConnectionError("boom")])
    with pytest.raises(FetchError, match="boom"):
        client.get_json("content/search")


def test_confluence_request_raises_fetch_error_when_max_retries_is_negative():
    from harborrag_adapters.connectors.exceptions import FetchError

    client = _confluence_client()
    client.config.max_retries = -1
    client.session = FakeSession(responses=[])
    with pytest.raises(FetchError):
        client.get_json("content/search")


def test_confluence_acquire_sleeps_when_requests_arrive_faster_than_budget(monkeypatch):
    import time

    sleeps: list[float] = []
    monkeypatch.setattr(time, "sleep", lambda seconds: sleeps.append(seconds))

    client = _confluence_client()
    client.config.requests_per_minute = 1
    client._min_interval = 60.0 / 1
    client.session = FakeSession(
        responses=[
            FakeResponse(status_code=200, _json={"a": 1}),
            FakeResponse(status_code=200, _json={"b": 2}),
        ]
    )
    client.get_json("content/search")
    client.get_json("content/search")

    assert any(seconds > 0 for seconds in sleeps)


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
    client.session = FakeSession(
        responses=[FakeResponse(status_code=401, text="bad token")]
    )
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
    client.session = FakeSession(
        responses=[
            FakeResponse(status_code=429, headers={"Retry-After": "1"}, text=""),
            FakeResponse(status_code=200, _json={"ok": True}),
        ]
    )
    assert client.get_json("repos/o/r") == {"ok": True}


def test_github_request_retries_5xx_then_succeeds():
    client = _github_client(max_retries=2, backoff_factor=0)
    client.session = FakeSession(
        responses=[
            FakeResponse(status_code=500, text="boom", headers={}),
            FakeResponse(status_code=200, _json={"ok": True}),
        ]
    )
    assert client.get_json("repos/o/r") == {"ok": True}


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
    with pytest.raises(FetchError, match="boom"):
        client.get_json("repos/o/r")


def test_github_client_omits_authorization_header_without_token():
    client = _github_client(token=None)
    assert "Authorization" not in client.session.headers


def test_github_config_rejects_negative_max_retries():
    with pytest.raises(ValueError, match="max_retries"):
        _github_client(max_retries=-1)


def test_github_rate_limited_static_predicate_branches():
    from harborrag_adapters.connectors.github.connector import _RequestsGitHubClient

    assert _RequestsGitHubClient._rate_limited(
        FakeResponse(status_code=429, headers={}, text="")
    )
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


def test_sharepoint_api_url_and_non_json():
    from harborrag_adapters.connectors.exceptions import FetchError
    from harborrag_adapters.connectors.sharepoint.config import SharePointSiteConfig
    from harborrag_adapters.connectors.sharepoint.connector import _RequestsGraphClient

    cfg = SharePointSiteConfig(
        site_url="https://ex.sharepoint.com/sites/s",
        access_token="tok",
        max_file_size_bytes=1024,
    )
    client = _RequestsGraphClient(cfg)
    client.session = FakeSession(
        responses=[FakeResponse(status_code=200, text="not json")]
    )
    with pytest.raises(FetchError, match="non-JSON"):
        client.get_json("sites/x")


def _sharepoint_client(**overrides):
    from harborrag_adapters.connectors.sharepoint.config import SharePointSiteConfig
    from harborrag_adapters.connectors.sharepoint.connector import _RequestsGraphClient

    values = {
        "site_url": "https://ex.sharepoint.com/sites/s",
        "access_token": "tok",
        "requests_per_minute": 6000,
        "max_retries": 1,
        "backoff_factor": 0.01,
    }
    values.update(overrides)
    return _RequestsGraphClient(SharePointSiteConfig(**values))


def test_sharepoint_get_json_decodes_dict_payload():
    client = _sharepoint_client()
    client.session = FakeSession(
        responses=[FakeResponse(status_code=200, _json={"value": []})]
    )
    assert client.get_json("sites/x/drives") == {"value": []}


def test_sharepoint_get_json_rejects_non_dict_payload():
    from harborrag_adapters.connectors.exceptions import FetchError

    client = _sharepoint_client()
    client.session = FakeSession(responses=[FakeResponse(status_code=200, _json=[1, 2])])
    with pytest.raises(FetchError, match="invalid JSON"):
        client.get_json("sites/x/drives")


def test_sharepoint_get_bytes_streams_capped_body():
    client = _sharepoint_client()
    client.config.max_file_size_bytes = 1024
    client.session = FakeSession(
        responses=[FakeResponse(status_code=200, _chunks=[b"hello ", b"world"])]
    )
    assert client.get_bytes("drives/d/items/i/content") == b"hello world"


def test_sharepoint_get_bytes_raises_fetch_error_when_body_too_large():
    from harborrag_adapters.connectors.exceptions import FetchError

    client = _sharepoint_client()
    client.config.max_file_size_bytes = 4
    client.session = FakeSession(
        responses=[FakeResponse(status_code=200, _chunks=[b"way too big"])]
    )
    with pytest.raises(FetchError, match="exceeds cap"):
        client.get_bytes("drives/d/items/i/content")


def test_sharepoint_api_url_rejects_cross_origin_absolute():
    from harborrag_adapters.connectors.exceptions import FetchError

    client = _sharepoint_client()
    with pytest.raises(FetchError):
        client._api_url("https://evil.example.com/x")


def test_sharepoint_api_url_allows_same_origin_absolute():
    client = _sharepoint_client()
    url = f"{client.config.graph_api_url}/sites/x"
    assert client._api_url(url) == url


def test_sharepoint_api_url_joins_relative_endpoint():
    client = _sharepoint_client()
    assert client._api_url("sites/x") == f"{client.config.graph_api_url}/sites/x"
    assert client._api_url("/sites/x") == f"{client.config.graph_api_url}/sites/x"


def test_sharepoint_request_raises_authentication_error_on_401():
    from harborrag_adapters.connectors.exceptions import AuthenticationError

    client = _sharepoint_client()
    client.session = FakeSession(
        responses=[FakeResponse(status_code=401, text="bad token")]
    )
    with pytest.raises(AuthenticationError):
        client.get_json("sites/x")


def test_sharepoint_request_raises_authentication_error_on_403():
    from harborrag_adapters.connectors.exceptions import AuthenticationError

    client = _sharepoint_client()
    client.session = FakeSession(
        responses=[FakeResponse(status_code=403, text="forbidden")]
    )
    with pytest.raises(AuthenticationError):
        client.get_json("sites/x")


def test_sharepoint_request_raises_rate_limit_error_after_exhausting_retries():
    from harborrag_adapters.connectors.exceptions import RateLimitError

    client = _sharepoint_client(max_retries=0)
    client.session = FakeSession(
        responses=[FakeResponse(status_code=429, headers={}, text="slow down")]
    )
    with pytest.raises(RateLimitError):
        client.get_json("sites/x")


def test_sharepoint_request_retries_429_then_succeeds():
    client = _sharepoint_client(max_retries=1)
    client.session = FakeSession(
        responses=[
            FakeResponse(status_code=429, headers={"Retry-After": "1"}, text=""),
            FakeResponse(status_code=200, _json={"ok": True}),
        ]
    )
    assert client.get_json("sites/x") == {"ok": True}


def test_sharepoint_request_retries_5xx_then_succeeds():
    client = _sharepoint_client(max_retries=1, backoff_factor=0.0)
    client.session = FakeSession(
        responses=[
            FakeResponse(status_code=503, text="boom", headers={}),
            FakeResponse(status_code=200, _json={"ok": True}),
        ]
    )
    assert client.get_json("sites/x") == {"ok": True}


def test_sharepoint_request_raises_fetch_error_on_non_retryable_4xx():
    from harborrag_adapters.connectors.exceptions import FetchError

    client = _sharepoint_client()
    client.session = FakeSession(
        responses=[FakeResponse(status_code=404, text="missing", headers={})]
    )
    with pytest.raises(FetchError, match="404"):
        client.get_json("sites/x/nope")


def test_sharepoint_request_retries_connection_errors_then_succeeds():
    import requests

    client = _sharepoint_client(max_retries=1)
    client.session = FakeSession(
        responses=[
            requests.ConnectionError("boom"),
            FakeResponse(status_code=200, _json={"ok": True}),
        ]
    )
    assert client.get_json("sites/x") == {"ok": True}


def test_sharepoint_request_raises_fetch_error_after_exhausting_connection_errors():
    import requests

    from harborrag_adapters.connectors.exceptions import FetchError

    client = _sharepoint_client(max_retries=0)
    client.session = FakeSession(responses=[requests.ConnectionError("boom")])
    with pytest.raises(FetchError, match="boom"):
        client.get_json("sites/x")


def test_sharepoint_config_rejects_negative_max_retries():
    with pytest.raises(ValueError, match="max_retries"):
        _sharepoint_client(max_retries=-1)


def test_sharepoint_acquire_sleeps_when_requests_arrive_faster_than_budget(monkeypatch):
    import time

    sleeps: list[float] = []
    monkeypatch.setattr(time, "sleep", lambda seconds: sleeps.append(seconds))

    client = _sharepoint_client(requests_per_minute=1)
    client.session = FakeSession(
        responses=[
            FakeResponse(status_code=200, _json={"a": 1}),
            FakeResponse(status_code=200, _json={"b": 2}),
        ]
    )
    client.get_json("sites/x")
    client.get_json("sites/x")

    assert any(seconds > 0 for seconds in sleeps)


def test_sharepoint_access_token_uses_configured_token_directly():
    client = _sharepoint_client(access_token="configured-token")
    assert client._access_token() == "configured-token"


def _client_credentials_sharepoint_client(**overrides):
    values = {
        "site_url": "https://ex.sharepoint.com/sites/s",
        "access_token": None,
        "tenant_id": "tid",
        "client_id": "cid",
        "client_secret": "secret",
        "requests_per_minute": 6000,
        "max_retries": 1,
        "backoff_factor": 0.01,
    }
    values.update(overrides)
    return _sharepoint_client(**values)


def test_sharepoint_access_token_fetches_and_caches_via_client_credentials():
    client = _client_credentials_sharepoint_client()
    client.session = FakeSession(
        responses=[
            FakeResponse(
                status_code=200,
                _json={"access_token": "fresh-token", "expires_in": 3600},
            )
        ]
    )
    assert client._access_token() == "fresh-token"
    # Cached: no further responses queued, so a second call must not re-request.
    assert client._access_token() == "fresh-token"


def test_sharepoint_access_token_refreshes_when_expired():
    import time

    client = _client_credentials_sharepoint_client()
    client.session = FakeSession(
        responses=[
            FakeResponse(
                status_code=200,
                _json={"access_token": "first", "expires_in": 3600},
            ),
            FakeResponse(
                status_code=200,
                _json={"access_token": "second", "expires_in": 3600},
            ),
        ]
    )
    assert client._access_token() == "first"
    client._token_expires_at = time.monotonic() - 1
    assert client._access_token() == "second"


def test_sharepoint_access_token_raises_on_request_exception():
    import requests

    from harborrag_adapters.connectors.exceptions import AuthenticationError

    client = _client_credentials_sharepoint_client()
    client.session = FakeSession(responses=[requests.ConnectionError("boom")])
    with pytest.raises(AuthenticationError, match="boom"):
        client._access_token()


def test_sharepoint_access_token_raises_on_error_status():
    from harborrag_adapters.connectors.exceptions import AuthenticationError

    client = _client_credentials_sharepoint_client()
    client.session = FakeSession(
        responses=[FakeResponse(status_code=401, text="bad client secret")]
    )
    with pytest.raises(AuthenticationError):
        client._access_token()


def test_sharepoint_access_token_raises_on_non_json_response():
    from harborrag_adapters.connectors.exceptions import AuthenticationError

    client = _client_credentials_sharepoint_client()
    client.session = FakeSession(responses=[FakeResponse(status_code=200, text="oops")])
    with pytest.raises(AuthenticationError, match="non-JSON"):
        client._access_token()


def test_sharepoint_access_token_raises_on_non_dict_json():
    from harborrag_adapters.connectors.exceptions import AuthenticationError

    client = _client_credentials_sharepoint_client()
    client.session = FakeSession(responses=[FakeResponse(status_code=200, _json=[1, 2])])
    with pytest.raises(AuthenticationError, match="invalid JSON"):
        client._access_token()


def test_sharepoint_access_token_raises_when_token_missing_from_payload():
    from harborrag_adapters.connectors.exceptions import AuthenticationError

    client = _client_credentials_sharepoint_client()
    client.session = FakeSession(responses=[FakeResponse(status_code=200, _json={})])
    with pytest.raises(AuthenticationError, match="missing token"):
        client._access_token()


def test_jira_api_version_by_deployment():
    from harborrag_adapters.connectors.jira.config import (
        JiraDeploymentType,
        JiraProjectConfig,
    )
    from harborrag_adapters.connectors.jira.connector import _RequestsJiraClient

    cloud = _RequestsJiraClient(
        JiraProjectConfig(
            base_url="https://ex.atlassian.net",
            email="a@b.c",
            token="t",
            deployment_type=JiraDeploymentType.CLOUD,
            requests_per_minute=6000,
        )
    )
    dc = _RequestsJiraClient(
        JiraProjectConfig(
            base_url="https://jira.local",
            token="pat",
            deployment_type=JiraDeploymentType.DATACENTER,
            requests_per_minute=6000,
        )
    )
    assert cloud.api_version == "3"
    assert dc.api_version == "2"
    assert cloud._api_url("search/jql").endswith("/rest/api/3/search/jql")
    assert dc._api_url("search").endswith("/rest/api/2/search")


def _jira_client(**overrides):
    from harborrag_adapters.connectors.jira.config import (
        JiraDeploymentType,
        JiraProjectConfig,
    )
    from harborrag_adapters.connectors.jira.connector import _RequestsJiraClient

    values = {
        "base_url": "https://ex.atlassian.net",
        "email": "a@b.c",
        "token": "t",
        "deployment_type": JiraDeploymentType.CLOUD,
        "requests_per_minute": 6000,
        "max_retries": 1,
        "backoff_factor": 0.01,
    }
    values.update(overrides)
    return _RequestsJiraClient(JiraProjectConfig(**values))


def test_jira_get_json_decodes_and_rejects_non_json():
    from harborrag_adapters.connectors.exceptions import FetchError

    client = _jira_client()
    client.session = FakeSession(
        responses=[
            FakeResponse(status_code=200, _json={"ok": True}),
            FakeResponse(status_code=200, text="<html>not json</html>"),
        ]
    )
    assert client.get_json("issue/ENG-1") == {"ok": True}
    with pytest.raises(FetchError, match="non-JSON"):
        client.get_json("issue/ENG-1")


def test_jira_post_json_decodes_and_rejects_non_json():
    from harborrag_adapters.connectors.exceptions import FetchError

    client = _jira_client()
    client.session = FakeSession(
        responses=[
            FakeResponse(status_code=200, _json={"issues": []}),
            FakeResponse(status_code=200, text="not json"),
        ]
    )
    assert client.post_json("search/jql", json={}) == {"issues": []}
    with pytest.raises(FetchError, match="non-JSON"):
        client.post_json("search/jql", json={})


def test_jira_download_bytes_rejects_cross_origin():
    from harborrag_adapters.connectors.exceptions import FetchError

    client = _jira_client()
    with pytest.raises(FetchError, match="origin|scheme"):
        client.download_bytes("https://evil.example.com/secret")


def test_jira_download_bytes_streams_capped_body():
    client = _jira_client()
    client.config.max_attachment_size_bytes = 1024
    client.session = FakeSession(
        responses=[FakeResponse(status_code=200, _chunks=[b"hello ", b"world"])]
    )
    assert client.download_bytes("https://ex.atlassian.net/secure/a") == b"hello world"


def test_jira_download_bytes_returns_none_for_empty_body():
    client = _jira_client()
    client.session = FakeSession(responses=[FakeResponse(status_code=200, _chunks=[])])
    assert client.download_bytes("https://ex.atlassian.net/secure/a") is None


def test_jira_download_bytes_raises_fetch_error_when_body_too_large():
    from harborrag_adapters.connectors.exceptions import FetchError

    client = _jira_client()
    client.config.max_attachment_size_bytes = 4
    client.session = FakeSession(
        responses=[FakeResponse(status_code=200, _chunks=[b"way too big"])]
    )
    with pytest.raises(FetchError, match="exceeds cap"):
        client.download_bytes("https://ex.atlassian.net/secure/a")


def test_jira_request_raises_authentication_error_on_401():
    from harborrag_adapters.connectors.exceptions import AuthenticationError

    client = _jira_client()
    client.session = FakeSession(
        responses=[FakeResponse(status_code=401, text="bad token")]
    )
    with pytest.raises(AuthenticationError):
        client.get_json("issue/ENG-1")


def test_jira_request_raises_rate_limit_error_after_exhausting_retries():
    from harborrag_adapters.connectors.exceptions import RateLimitError

    client = _jira_client(max_retries=0)
    client.session = FakeSession(
        responses=[FakeResponse(status_code=429, headers={}, text="slow down")]
    )
    with pytest.raises(RateLimitError):
        client.get_json("issue/ENG-1")


def test_jira_request_retries_5xx_then_succeeds_with_zero_backoff():
    client = _jira_client(max_retries=1, backoff_factor=0.0)
    client.session = FakeSession(
        responses=[
            FakeResponse(status_code=500, text="boom", headers={}),
            FakeResponse(status_code=200, _json={"ok": True}),
        ]
    )
    assert client.get_json("issue/ENG-1") == {"ok": True}


def test_jira_request_retries_connection_errors_then_succeeds():
    import requests

    client = _jira_client(max_retries=1)
    client.session = FakeSession(
        responses=[
            requests.ConnectionError("boom"),
            FakeResponse(status_code=200, _json={"ok": True}),
        ]
    )
    assert client.get_json("issue/ENG-1") == {"ok": True}


def test_jira_request_raises_fetch_error_after_exhausting_connection_errors():
    import requests

    from harborrag_adapters.connectors.exceptions import FetchError

    client = _jira_client(max_retries=0)
    client.session = FakeSession(responses=[requests.ConnectionError("boom")])
    with pytest.raises(FetchError, match="boom"):
        client.get_json("issue/ENG-1")


def test_jira_request_raises_fetch_error_on_non_retryable_4xx():
    from harborrag_adapters.connectors.exceptions import FetchError

    client = _jira_client()
    client.session = FakeSession(
        responses=[FakeResponse(status_code=404, text="missing", headers={})]
    )
    with pytest.raises(FetchError, match="404"):
        client.get_json("issue/ENG-1/nope")


def test_jira_config_rejects_negative_max_retries():
    with pytest.raises(ValueError, match="max_retries"):
        _jira_client(max_retries=-1)


def test_jira_acquire_sleeps_when_requests_arrive_faster_than_budget(monkeypatch):
    import time

    sleeps: list[float] = []
    monkeypatch.setattr(time, "sleep", lambda seconds: sleeps.append(seconds))

    client = _jira_client(requests_per_minute=1)
    client.session = FakeSession(
        responses=[
            FakeResponse(status_code=200, _json={"a": 1}),
            FakeResponse(status_code=200, _json={"b": 2}),
        ]
    )
    client.get_json("issue/ENG-1")
    client.get_json("issue/ENG-1")

    assert any(seconds > 0 for seconds in sleeps)
