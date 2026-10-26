"""Unit tests for the real Microsoft Graph HTTP client wrapper with fake sessions."""

from __future__ import annotations

import pytest
from harbor_test_builders import FakeResponse, FakeSession
from sharepoint_http_test_helpers import sharepoint_client

from harborrag_adapters.connectors.exceptions import FetchError, RateLimitError

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


def test_sharepoint_request_raises_authorization_error_on_403():
    # A 403 is "no access to this resource", not "bad credentials": callers
    # skip one item as ACCESS_DENIED instead of failing the whole run.
    from harborrag_adapters.connectors.exceptions import AuthenticationError, AuthorizationError

    client = sharepoint_client()
    client.session = FakeSession(responses=[FakeResponse(status_code=403, text="forbidden")])
    with pytest.raises(AuthorizationError) as excinfo:
        client.get_json("sites/x")
    assert excinfo.value.status_code == 403
    assert not isinstance(excinfo.value, AuthenticationError)


def test_sharepoint_request_keeps_status_code_on_not_found():
    from harborrag_adapters.connectors.exceptions import FetchError

    client = sharepoint_client()
    client.session = FakeSession(responses=[FakeResponse(status_code=404, text="gone")])
    with pytest.raises(FetchError) as excinfo:
        client.get_json("drives/d/items/x")
    assert excinfo.value.status_code == 404


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


@pytest.mark.parametrize("retry_after", [7, 300])
def test_sharepoint_waits_the_full_retry_after_before_retrying(monkeypatch, retry_after):
    import time

    sleeps: list[float] = []
    monkeypatch.setattr(time, "sleep", sleeps.append)
    client = sharepoint_client(max_retries=1)
    client.session = FakeSession(
        responses=[
            FakeResponse(status_code=429, headers={"Retry-After": str(retry_after)}, text=""),
            FakeResponse(status_code=200, _json={"ok": True}),
        ]
    )

    assert client.get_json("sites/x") == {"ok": True}
    assert any(retry_after <= seconds <= retry_after + 1 for seconds in sleeps)


@pytest.mark.parametrize(("status", "error_type"), [(429, RateLimitError), (503, FetchError)])
def test_sharepoint_gives_up_rather_than_retry_before_a_long_retry_after(
    monkeypatch, status, error_type
):
    import time

    sleeps: list[float] = []
    monkeypatch.setattr(time, "sleep", sleeps.append)
    client = sharepoint_client(max_retries=3)
    client.session = FakeSession(
        responses=[FakeResponse(status_code=status, headers={"Retry-After": "900"}, text="")]
    )

    with pytest.raises(error_type) as excinfo:
        client.get_json("sites/x")

    assert type(excinfo.value) is error_type
    assert excinfo.value.status_code == status
    assert sleeps == []


def test_sharepoint_request_retries_5xx_then_succeeds():
    client = sharepoint_client(max_retries=1, backoff_factor=0.0)
    retried = FakeResponse(status_code=503, text="boom", headers={})
    client.session = FakeSession(
        responses=[
            retried,
            FakeResponse(status_code=200, _json={"ok": True}),
        ]
    )
    assert client.get_json("sites/x") == {"ok": True}
    assert retried.closed is True


def test_sharepoint_get_bytes_closes_retried_response_before_retrying():
    client = sharepoint_client(max_retries=1, backoff_factor=0.0)
    retried = FakeResponse(status_code=503, text="boom", headers={})
    client.session = FakeSession(
        responses=[
            retried,
            FakeResponse(status_code=200, _chunks=[b"hello"]),
        ]
    )
    assert client.get_bytes("drives/d/items/i/content") == b"hello"
    assert retried.closed is True


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
    with pytest.raises(FetchError, match="Microsoft Graph request failed") as error:
        client.get_json("sites/x")
    assert "boom" not in str(error.value)


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


def test_sharepoint_acquire_reserves_distinct_slots_for_concurrent_callers(monkeypatch):
    """Concurrent _acquire() calls must each wait for their own request slot.

    Two threads that both read the same stale "last request" time would both
    proceed together, bursting past the configured requests-per-minute budget.
    The limiter reserves one evenly spaced slot per caller instead.
    """
    import threading
    import time

    # Fix the clock so every call observes the same "now" and the reserved
    # slots alone decide how long each caller waits.
    monkeypatch.setattr(time, "monotonic", lambda: 1000.0)
    waits: list[float] = []
    guard = threading.Lock()

    def recording_sleep(seconds: float) -> None:
        with guard:
            waits.append(seconds)

    monkeypatch.setattr(time, "sleep", recording_sleep)
    client = sharepoint_client(requests_per_minute=1)  # one slot every 60s

    threads = [threading.Thread(target=client._acquire) for _ in range(3)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=3)

    assert sorted(waits) == [60.0, 120.0]
