"""Unit tests for the real Microsoft Graph HTTP client wrapper with fake sessions."""

from __future__ import annotations

import time as _time_module

import pytest
from harbor_test_builders import FakeResponse, FakeSession
from sharepoint_http_test_helpers import sharepoint_client

# Captured at import time, before the autouse fixture below patches
# time.sleep to a no-op for every test in this module.
_REAL_SLEEP = _time_module.sleep

pytestmark = [pytest.mark.unit, pytest.mark.graybox]


@pytest.fixture(autouse=True)
def _no_sleep(monkeypatch):
    import time

    monkeypatch.setattr(time, "sleep", lambda *_a, **_k: None)


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
    client.session = FakeSession(responses=[FakeResponse(status_code=200, text="not json")])
    with pytest.raises(FetchError, match="non-JSON"):
        client.get_json("sites/x")


def test_sharepoint_get_json_decodes_dict_payload():
    client = sharepoint_client()
    client.session = FakeSession(responses=[FakeResponse(status_code=200, _json={"value": []})])
    assert client.get_json("sites/x/drives") == {"value": []}


def test_sharepoint_get_json_rejects_non_dict_payload():
    from harborrag_adapters.connectors.exceptions import FetchError

    client = sharepoint_client()
    client.session = FakeSession(responses=[FakeResponse(status_code=200, _json=[1, 2])])
    with pytest.raises(FetchError, match="invalid JSON"):
        client.get_json("sites/x/drives")


def test_sharepoint_get_bytes_streams_capped_body():
    client = sharepoint_client()
    client.config.max_file_size_bytes = 1024
    client.session = FakeSession(
        responses=[FakeResponse(status_code=200, _chunks=[b"hello ", b"world"])]
    )
    assert client.get_bytes("drives/d/items/i/content") == b"hello world"


def test_sharepoint_get_bytes_raises_fetch_error_when_body_too_large():
    from harborrag_adapters.connectors.exceptions import FetchError

    client = sharepoint_client()
    client.config.max_file_size_bytes = 4
    client.session = FakeSession(
        responses=[FakeResponse(status_code=200, _chunks=[b"way too big"])]
    )
    with pytest.raises(FetchError, match="exceeds cap"):
        client.get_bytes("drives/d/items/i/content")


def test_sharepoint_api_url_rejects_cross_origin_absolute():
    from harborrag_adapters.connectors.exceptions import FetchError

    client = sharepoint_client()
    with pytest.raises(FetchError):
        client._api_url("https://evil.example.com/x")


def test_sharepoint_api_url_allows_same_origin_absolute():
    client = sharepoint_client()
    url = f"{client.config.graph_api_url}/sites/x"
    assert client._api_url(url) == url


def test_sharepoint_api_url_joins_relative_endpoint():
    client = sharepoint_client()
    assert client._api_url("sites/x") == f"{client.config.graph_api_url}/sites/x"
    assert client._api_url("/sites/x") == f"{client.config.graph_api_url}/sites/x"


def test_sharepoint_request_raises_authentication_error_on_401():
    from harborrag_adapters.connectors.exceptions import AuthenticationError

    client = sharepoint_client()
    client.session = FakeSession(responses=[FakeResponse(status_code=401, text="bad token")])
    with pytest.raises(AuthenticationError):
        client.get_json("sites/x")


def test_sharepoint_request_raises_authentication_error_on_403():
    from harborrag_adapters.connectors.exceptions import AuthenticationError

    client = sharepoint_client()
    client.session = FakeSession(responses=[FakeResponse(status_code=403, text="forbidden")])
    with pytest.raises(AuthenticationError):
        client.get_json("sites/x")


def test_sharepoint_request_raises_rate_limit_error_after_exhausting_retries():
    from harborrag_adapters.connectors.exceptions import RateLimitError

    client = sharepoint_client(max_retries=0)
    client.session = FakeSession(
        responses=[FakeResponse(status_code=429, headers={}, text="slow down")]
    )
    with pytest.raises(RateLimitError):
        client.get_json("sites/x")


def test_sharepoint_request_retries_429_then_succeeds():
    client = sharepoint_client(max_retries=1)
    client.session = FakeSession(
        responses=[
            FakeResponse(status_code=429, headers={"Retry-After": "1"}, text=""),
            FakeResponse(status_code=200, _json={"ok": True}),
        ]
    )
    assert client.get_json("sites/x") == {"ok": True}


def test_sharepoint_request_retries_5xx_then_succeeds():
    client = sharepoint_client(max_retries=1, backoff_factor=0.0)
    client.session = FakeSession(
        responses=[
            FakeResponse(status_code=503, text="boom", headers={}),
            FakeResponse(status_code=200, _json={"ok": True}),
        ]
    )
    assert client.get_json("sites/x") == {"ok": True}


def test_sharepoint_request_raises_fetch_error_on_non_retryable_4xx():
    from harborrag_adapters.connectors.exceptions import FetchError

    client = sharepoint_client()
    client.session = FakeSession(
        responses=[FakeResponse(status_code=404, text="missing", headers={})]
    )
    with pytest.raises(FetchError, match="404"):
        client.get_json("sites/x/nope")


def test_sharepoint_request_retries_connection_errors_then_succeeds():
    import requests

    client = sharepoint_client(max_retries=1)
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

    client = sharepoint_client(max_retries=0)
    client.session = FakeSession(responses=[requests.ConnectionError("boom")])
    with pytest.raises(FetchError, match="boom"):
        client.get_json("sites/x")


def test_sharepoint_config_rejects_negative_max_retries():
    with pytest.raises(ValueError, match="max_retries"):
        sharepoint_client(max_retries=-1)


def test_sharepoint_acquire_sleeps_when_requests_arrive_faster_than_budget(monkeypatch):
    import time

    sleeps: list[float] = []
    monkeypatch.setattr(time, "sleep", lambda seconds: sleeps.append(seconds))

    client = sharepoint_client(requests_per_minute=1)
    client.session = FakeSession(
        responses=[
            FakeResponse(status_code=200, _json={"a": 1}),
            FakeResponse(status_code=200, _json={"b": 2}),
        ]
    )
    client.get_json("sites/x")
    client.get_json("sites/x")

    assert any(seconds > 0 for seconds in sleeps)


def test_sharepoint_acquire_serializes_concurrent_callers(monkeypatch):
    """Concurrent _acquire() calls must not overlap inside the critical section.

    Without the rate-limit lock, two threads can both read the same stale
    _last_request_at, both decide to sleep, and both proceed together --
    bursting past the configured requests-per-minute budget.
    """
    import threading
    import time

    client = sharepoint_client(requests_per_minute=1)  # min_interval = 60s
    # Fix the clock so every call after the first observes a fresh
    # _last_request_at and unambiguously decides to sleep.
    monkeypatch.setattr(time, "monotonic", lambda: 1000.0)

    active = 0
    max_active = 0
    guard = threading.Lock()

    def instrumented_sleep(_seconds: float) -> None:
        nonlocal active, max_active
        with guard:
            active += 1
            max_active = max(max_active, active)
        # Hold the "critical section" open briefly (real time, unaffected by
        # the monotonic patch above) so a racing thread would have a chance
        # to pile in here if _acquire did not hold its lock during sleep.
        _REAL_SLEEP(0.05)
        with guard:
            active -= 1

    monkeypatch.setattr(time, "sleep", instrumented_sleep)

    threads = [threading.Thread(target=client._acquire) for _ in range(3)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=3)

    assert max_active == 1
