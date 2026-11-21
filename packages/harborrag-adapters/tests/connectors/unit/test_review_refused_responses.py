"""Oversized bodies, refused redirects and off-origin URLs are never retried (CON-5)."""

from __future__ import annotations

import re

import pytest
import requests_mock

from harborrag_adapters.connectors.confluence.client import _RequestsConfluenceClient
from harborrag_adapters.connectors.confluence.config import ConfluenceSpaceConfig
from harborrag_adapters.connectors.exceptions import FetchError, RefusedResponseError
from harborrag_adapters.connectors.sharepoint.client import _RequestsGraphClient
from harborrag_adapters.connectors.sharepoint.config import SharePointSiteConfig
from harborrag_core.ingestion.policies import IngestionFailureClassifier

pytestmark = pytest.mark.unit

CONFLUENCE_BASE = "https://example.atlassian.net/wiki"
CONFLUENCE_DOWNLOAD = f"{CONFLUENCE_BASE}/download/attachments/123/quarterly-plan.pdf"
MEDIA_URL = "https://api.media.atlassian.com/file/abc/binary"
GRAPH = re.compile(r"https://graph\.microsoft\.com/.*")
OVERSIZED = {"Content-Length": str(64 * 1024 * 1024)}


def _confluence() -> _RequestsConfluenceClient:
    return _RequestsConfluenceClient(
        ConfluenceSpaceConfig(
            space_key="ENG",
            base_url=CONFLUENCE_BASE,
            token="t",
            email="alex@example.com",
            requests_per_minute=6000,
            max_retries=0,
        )
    )


def _graph() -> _RequestsGraphClient:
    return _RequestsGraphClient(
        SharePointSiteConfig(
            site_url="https://example.sharepoint.com/sites/eng",
            access_token="tok",
            requests_per_minute=6000,
            max_retries=0,
        )
    )


def _assert_never_retried(error: FetchError) -> None:
    assert isinstance(error, RefusedResponseError)
    assert error.retryable is False
    assert IngestionFailureClassifier().classify("fetch", error).retryable is False


def test_redirect_to_an_untrusted_origin_is_not_retried() -> None:
    with requests_mock.Mocker() as mocker:
        mocker.get(
            CONFLUENCE_DOWNLOAD, status_code=302, headers={"Location": "https://evil.example/x"}
        )
        with pytest.raises(FetchError) as raised:
            _confluence().download_bytes(CONFLUENCE_DOWNLOAD)

    _assert_never_retried(raised.value)


def test_chained_redirect_is_not_retried() -> None:
    with requests_mock.Mocker() as mocker:
        mocker.get(CONFLUENCE_DOWNLOAD, status_code=302, headers={"Location": MEDIA_URL})
        mocker.get(MEDIA_URL, status_code=302, headers={"Location": f"{MEDIA_URL}/again"})
        with pytest.raises(FetchError) as raised:
            _confluence().download_bytes(CONFLUENCE_DOWNLOAD)

    _assert_never_retried(raised.value)


def test_download_outside_the_configured_origin_is_not_retried() -> None:
    with pytest.raises(FetchError) as raised:
        _confluence().download_bytes("https://other.example/download/attachments/1/a.pdf")

    _assert_never_retried(raised.value)


def test_oversized_attachment_is_not_retried() -> None:
    with requests_mock.Mocker() as mocker:
        mocker.get(CONFLUENCE_DOWNLOAD, content=b"%PDF", headers={"Content-Length": "4096"})
        with pytest.raises(FetchError) as raised:
            _confluence().download_bytes(CONFLUENCE_DOWNLOAD, max_bytes=1024)

    _assert_never_retried(raised.value)


def test_oversized_json_is_not_retried_but_malformed_json_is() -> None:
    with requests_mock.Mocker() as mocker:
        mocker.get(f"{CONFLUENCE_BASE}/rest/api/space", text="{}", headers=OVERSIZED)
        with pytest.raises(FetchError) as oversized:
            _confluence().get_json("space")
        mocker.get(GRAPH, text="<html>gateway busy</html>")
        with pytest.raises(FetchError) as malformed:
            _graph().get_json("sites/root")

    _assert_never_retried(oversized.value)
    assert malformed.value.retryable is True


def test_oversized_graph_responses_are_not_retried() -> None:
    with requests_mock.Mocker() as mocker:
        mocker.get(GRAPH, text="{}", headers=OVERSIZED)
        with pytest.raises(FetchError) as json_body:
            _graph().get_json("sites/root")
        mocker.get(GRAPH, content=b"x", headers={"Content-Length": str(2**40)})
        with pytest.raises(FetchError) as file_body:
            _graph().get_bytes("drives/d1/items/i1/content")

    _assert_never_retried(json_body.value)
    _assert_never_retried(file_body.value)
