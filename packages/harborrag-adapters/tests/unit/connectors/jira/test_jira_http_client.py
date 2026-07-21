"""Unit tests for the real Jira HTTP client wrapper with fake sessions."""

from __future__ import annotations

import pytest
from harbor_test_builders import FakeResponse, FakeSession
from jira_http_test_helpers import jira_client as _jira_client

pytestmark = [pytest.mark.unit, pytest.mark.graybox]


@pytest.fixture(autouse=True)
def _no_sleep(monkeypatch):
    import time

    monkeypatch.setattr(time, "sleep", lambda *_a, **_k: None)


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


def test_jira_session_uses_basic_auth_for_cloud():
    from harborrag_adapters.connectors.jira.config import (
        JiraDeploymentType,
        JiraProjectConfig,
    )
    from harborrag_adapters.connectors.jira.connector import _RequestsJiraClient

    cfg = JiraProjectConfig(
        base_url="https://ex.atlassian.net",
        email="a@b.c",
        token="t",
        deployment_type=JiraDeploymentType.CLOUD,
        requests_per_minute=6000,
    )
    client = _RequestsJiraClient(cfg)
    assert client.session.auth == ("a@b.c", "t")
    assert "Authorization" not in client.session.headers


def test_jira_session_uses_bearer_auth_for_datacenter():
    from harborrag_adapters.connectors.jira.config import (
        JiraDeploymentType,
        JiraProjectConfig,
    )
    from harborrag_adapters.connectors.jira.connector import _RequestsJiraClient

    cfg = JiraProjectConfig(
        base_url="https://jira.local",
        token="pat",
        deployment_type=JiraDeploymentType.DATACENTER,
        requests_per_minute=6000,
    )
    client = _RequestsJiraClient(cfg)
    assert client.session.auth is None
    assert client.session.headers["Authorization"] == "Bearer pat"


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


@pytest.mark.parametrize("method", ["get", "post"])
def test_jira_json_methods_reject_non_dict_payload(method):
    from harborrag_adapters.connectors.exceptions import FetchError

    client = _jira_client()
    client.session = FakeSession(responses=[FakeResponse(status_code=200, _json=[])])

    with pytest.raises(FetchError, match="invalid JSON"):
        if method == "get":
            client.get_json("issue/ENG-1")
        else:
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
    assert client.session.calls[0]["url"] == ("https://ex.atlassian.net/secure/a?redirect=false")


def test_jira_cloud_download_disables_redirect_without_losing_query_parameters():
    client = _jira_client()
    client.session = FakeSession(responses=[FakeResponse(status_code=200, _chunks=[b"content"])])

    assert (
        client.download_bytes(
            "https://ex.atlassian.net/rest/api/3/attachment/content/1"
            "?version=2&empty=&redirect=true"
        )
        == b"content"
    )
    assert client.session.calls[0]["url"] == (
        "https://ex.atlassian.net/rest/api/3/attachment/content/1?version=2&empty=&redirect=false"
    )


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
    client.session = FakeSession(responses=[FakeResponse(status_code=401, text="bad token")])
    with pytest.raises(AuthenticationError):
        client.get_json("issue/ENG-1")


def test_jira_request_maps_403_to_skippable_fetch_error():
    from harborrag_adapters.connectors.exceptions import FetchError

    client = _jira_client()
    client.session = FakeSession(responses=[FakeResponse(status_code=403, text="restricted issue")])

    with pytest.raises(FetchError, match="403"):
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
