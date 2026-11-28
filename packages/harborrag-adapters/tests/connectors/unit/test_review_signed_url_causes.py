"""A failed hop to a signed download URL never carries that URL in the raised error."""

from __future__ import annotations

import re

import pytest
import requests
import requests_mock

from harborrag_adapters.connectors.confluence.client import _RequestsConfluenceClient
from harborrag_adapters.connectors.confluence.config import ConfluenceSpaceConfig
from harborrag_adapters.connectors.exceptions import FetchError
from harborrag_adapters.connectors.sharepoint.client import _RequestsGraphClient
from harborrag_adapters.connectors.sharepoint.config import SharePointSiteConfig

pytestmark = pytest.mark.unit

SIGNATURE = "synthetic-signature-value"
CONFLUENCE_BASE = "https://example.atlassian.net/wiki"
CONFLUENCE_DOWNLOAD = f"{CONFLUENCE_BASE}/download/attachments/123/quarterly-plan.pdf"
MEDIA_URL = f"https://api.media.atlassian.com/file/abc/binary?token={SIGNATURE}"
SHAREPOINT_DOWNLOAD = (
    "https://example.sharepoint.com/sites/eng/_layouts/15/download.aspx"
    f"?UniqueId=1&tempauth={SIGNATURE}"
)


def _connection_error(url: str) -> requests.ConnectionError:
    return requests.ConnectionError(
        f"HTTPSConnectionPool(host='example'): Max retries exceeded with url: {url}"
    )


def _assert_free_of_signature(error: BaseException) -> None:
    assert SIGNATURE not in str(error)
    assert error.__cause__ is None
    assert error.__suppress_context__ is True


def test_sharepoint_download_redirect_failure_drops_the_signed_url() -> None:
    client = _RequestsGraphClient(
        SharePointSiteConfig(
            site_url="https://example.sharepoint.com/sites/eng",
            access_token="tok",
            requests_per_minute=6000,
            max_retries=0,
        )
    )
    with requests_mock.Mocker() as mocker:
        mocker.get(
            re.compile(r"https://graph\.microsoft\.com/.*"),
            status_code=302,
            headers={"Location": SHAREPOINT_DOWNLOAD},
        )
        mocker.get(SHAREPOINT_DOWNLOAD, exc=_connection_error(SHAREPOINT_DOWNLOAD))

        with pytest.raises(FetchError) as raised:
            client.get_bytes("drives/drive1/items/item1/content")

    _assert_free_of_signature(raised.value)


def test_atlassian_media_redirect_failure_drops_the_signed_url() -> None:
    client = _RequestsConfluenceClient(
        ConfluenceSpaceConfig(
            space_key="ENG",
            base_url=CONFLUENCE_BASE,
            token="t",
            email="alex@example.com",
            requests_per_minute=6000,
        )
    )
    with requests_mock.Mocker() as mocker:
        mocker.get(CONFLUENCE_DOWNLOAD, status_code=302, headers={"Location": MEDIA_URL})
        mocker.get(MEDIA_URL, exc=_connection_error(MEDIA_URL))

        with pytest.raises(FetchError) as raised:
            client.download_bytes(CONFLUENCE_DOWNLOAD)

    _assert_free_of_signature(raised.value)


def test_atlassian_request_failure_drops_the_request_url() -> None:
    client = _RequestsConfluenceClient(
        ConfluenceSpaceConfig(
            space_key="ENG",
            base_url=CONFLUENCE_BASE,
            token="t",
            email="alex@example.com",
            requests_per_minute=6000,
            max_retries=0,
        )
    )
    signed = f"{CONFLUENCE_DOWNLOAD}?signature={SIGNATURE}"
    with requests_mock.Mocker() as mocker:
        mocker.get(signed, exc=_connection_error(signed))

        with pytest.raises(FetchError) as raised:
            client.download_bytes(signed)

    _assert_free_of_signature(raised.value)
