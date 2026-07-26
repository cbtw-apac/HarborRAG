"""Unit tests for SharePoint OAuth client-credentials token acquisition."""

from __future__ import annotations

import pytest
from harbor_test_builders import FakeResponse, FakeSession
from sharepoint_http_test_helpers import (
    client_credentials_sharepoint_client,
    sharepoint_client,
)

pytestmark = [pytest.mark.unit, pytest.mark.graybox]


@pytest.fixture(autouse=True)
def _no_sleep(monkeypatch):
    import time

    monkeypatch.setattr(time, "sleep", lambda *_a, **_k: None)


def test_sharepoint_access_token_uses_configured_token_directly():
    client = sharepoint_client(access_token="configured-token")
    assert client._access_token() == "configured-token"


def test_sharepoint_access_token_fetches_and_caches_via_client_credentials():
    client = client_credentials_sharepoint_client()
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

    client = client_credentials_sharepoint_client()
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

    # max_retries=1 means two total attempts before the error propagates.
    client = client_credentials_sharepoint_client()
    client.session = FakeSession(
        responses=[requests.ConnectionError("boom"), requests.ConnectionError("boom")]
    )
    with pytest.raises(AuthenticationError, match="Microsoft identity request failed") as error:
        client._access_token()
    assert "boom" not in str(error.value)


def test_sharepoint_access_token_retries_transient_failure_then_succeeds():
    import requests

    client = client_credentials_sharepoint_client()
    client.session = FakeSession(
        responses=[
            requests.ConnectionError("boom"),
            FakeResponse(
                status_code=200,
                _json={"access_token": "recovered", "expires_in": 3600},
            ),
        ]
    )
    assert client._access_token() == "recovered"


def test_sharepoint_access_token_retries_429_then_succeeds():
    client = client_credentials_sharepoint_client()
    client.session = FakeSession(
        responses=[
            FakeResponse(status_code=429, text="rate limited"),
            FakeResponse(
                status_code=200,
                _json={"access_token": "recovered", "expires_in": 3600},
            ),
        ]
    )
    assert client._access_token() == "recovered"


def test_sharepoint_access_token_raises_on_error_status():
    from harborrag_adapters.connectors.exceptions import AuthenticationError

    client = client_credentials_sharepoint_client()
    client.session = FakeSession(
        responses=[FakeResponse(status_code=401, text="bad client secret")]
    )
    with pytest.raises(AuthenticationError):
        client._access_token()


def test_sharepoint_access_token_raises_on_non_json_response():
    from harborrag_adapters.connectors.exceptions import AuthenticationError

    client = client_credentials_sharepoint_client()
    client.session = FakeSession(responses=[FakeResponse(status_code=200, text="oops")])
    with pytest.raises(AuthenticationError, match="non-JSON"):
        client._access_token()


def test_sharepoint_access_token_raises_on_non_dict_json():
    from harborrag_adapters.connectors.exceptions import AuthenticationError

    client = client_credentials_sharepoint_client()
    client.session = FakeSession(responses=[FakeResponse(status_code=200, _json=[1, 2])])
    with pytest.raises(AuthenticationError, match="invalid JSON"):
        client._access_token()


def test_sharepoint_access_token_raises_when_token_missing_from_payload():
    from harborrag_adapters.connectors.exceptions import AuthenticationError

    client = client_credentials_sharepoint_client()
    client.session = FakeSession(responses=[FakeResponse(status_code=200, _json={})])
    with pytest.raises(AuthenticationError, match="missing token"):
        client._access_token()
