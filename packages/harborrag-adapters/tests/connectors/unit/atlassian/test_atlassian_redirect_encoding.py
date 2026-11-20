"""Regression: Atlassian download redirects follow the Location the server sent."""

from __future__ import annotations

import pytest
import requests_mock

from harborrag_adapters.connectors.confluence.client import _RequestsConfluenceClient
from harborrag_adapters.connectors.confluence.config import ConfluenceSpaceConfig
from harborrag_adapters.connectors.exceptions import FetchError

pytestmark = pytest.mark.unit

BASE_URL = "https://example.atlassian.net/wiki"
DOWNLOAD_URL = f"{BASE_URL}/download/attachments/123/report.pdf"
MEDIA_URL = "https://api.media.atlassian.com/file/abc/binary"
ATTACHMENT_BYTES = b"synthetic attachment bytes"


def _client() -> _RequestsConfluenceClient:
    config = ConfluenceSpaceConfig(
        space_key="ENG",
        base_url=BASE_URL,
        token="t",
        email="alex@example.com",
        requests_per_minute=6000,
    )
    return _RequestsConfluenceClient(config)


def _as_http_client_decodes(location: str) -> str:
    return location.encode("utf-8").decode("latin-1")


@pytest.mark.parametrize(
    ("location", "expected_url"),
    [
        (
            _as_http_client_decodes(f"{MEDIA_URL}?name=Q3\u00a0review.pdf"),
            f"{MEDIA_URL}?name=Q3%C2%A0review.pdf",
        ),
        (f"{MEDIA_URL}?name=Q3%20review.pdf", f"{MEDIA_URL}?name=Q3%20review.pdf"),
        (f"{MEDIA_URL}?name=Q3 review.pdf", f"{MEDIA_URL}?name=Q3%20review.pdf"),
    ],
    ids=["raw-utf8", "percent-encoded", "ascii-space"],
)
def test_download_bytes_redirect_location_is_followed_as_sent(
    location: str,
    expected_url: str,
) -> None:
    """Raw UTF-8, percent-encoded and space-containing targets reach the wire intact."""
    client = _client()
    with requests_mock.Mocker() as mocker:
        mocker.get(DOWNLOAD_URL, status_code=302, headers={"Location": location})
        mocker.get(MEDIA_URL, content=ATTACHMENT_BYTES)

        body = client.download_bytes(DOWNLOAD_URL)

    assert body == ATTACHMENT_BYTES
    assert mocker.request_history[1].url == expected_url


def test_download_bytes_redirect_location_not_utf8_is_followed_as_received() -> None:
    """A Location whose bytes are not UTF-8 is followed to exactly those bytes.

    http.client hands the single byte 0xE9 over as the character U+00E9. Requests
    decodes it as UTF-8 while sending even without following redirects, so the real
    session is used here rather than a stand-in.
    """
    client = _client()
    location = f"{MEDIA_URL}?name=caf\xe9.pdf"
    with requests_mock.Mocker() as mocker:
        mocker.get(DOWNLOAD_URL, status_code=302, headers={"Location": location})
        mocker.get(MEDIA_URL, content=ATTACHMENT_BYTES)

        body = client.download_bytes(DOWNLOAD_URL)

    assert body == ATTACHMENT_BYTES
    assert mocker.request_history[1].url == f"{MEDIA_URL}?name=caf%E9.pdf"


def test_download_bytes_untrusted_raw_utf8_redirect_is_refused() -> None:
    """An untrusted target is refused without the URL in the error message."""
    client = _client()
    location = _as_http_client_decodes("https://files.example.com/Q3\u00a0review.pdf")
    with requests_mock.Mocker() as mocker:
        mocker.get(DOWNLOAD_URL, status_code=302, headers={"Location": location})

        with pytest.raises(FetchError, match="refused HTTP redirect 302") as caught:
            client.download_bytes(DOWNLOAD_URL)

    assert mocker.call_count == 1
    assert "review" not in str(caught.value)
