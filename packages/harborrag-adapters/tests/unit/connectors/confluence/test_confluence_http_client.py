"""Unit tests for the real Confluence HTTP client wrapper with fake sessions."""

from __future__ import annotations

import pytest
from confluence_http_test_helpers import confluence_client as _confluence_client
from harbor_test_builders import FakeResponse, FakeSession

pytestmark = [pytest.mark.unit, pytest.mark.graybox]


@pytest.fixture(autouse=True)
def _no_sleep(monkeypatch):
    import time

    monkeypatch.setattr(time, "sleep", lambda *_a, **_k: None)


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
    assert client.download_bytes("https://ex.atlassian.net/wiki/download/a") == (b"hello world")


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


def test_confluence_session_uses_basic_auth_for_cloud():
    from harborrag_adapters.connectors.confluence.config import (
        ConfluenceDeploymentType,
        ConfluenceSpaceConfig,
    )
    from harborrag_adapters.connectors.confluence.connector import (
        _RequestsConfluenceClient,
    )

    cfg = ConfluenceSpaceConfig(
        space_key="ENG",
        base_url="https://ex.atlassian.net/wiki",
        token="t",
        email="a@b.c",
        deployment_type=ConfluenceDeploymentType.CLOUD,
        requests_per_minute=6000,
    )
    client = _RequestsConfluenceClient(cfg)
    assert client.session.auth == ("a@b.c", "t")
    assert "Authorization" not in client.session.headers


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
    client.session = FakeSession(responses=[FakeResponse(status_code=401, text="bad token")])
    with pytest.raises(AuthenticationError):
        client.get_json("content/search")


def test_confluence_request_maps_403_to_skippable_fetch_error():
    from harborrag_adapters.connectors.exceptions import FetchError

    client = _confluence_client()
    client.session = FakeSession(responses=[FakeResponse(status_code=403, text="restricted page")])

    with pytest.raises(FetchError, match="403"):
        client.get_json("content/1")


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
