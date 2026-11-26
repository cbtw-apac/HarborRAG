"""A SharePoint download cut off after its headers is a transient fetch failure."""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass

import pytest
from harbor_test_builders import FakeResponse, FakeSession
from requests import exceptions as requests_errors
from sharepoint_http_test_helpers import sharepoint_client

from harborrag_adapters.connectors.exceptions import FetchError

pytestmark = [pytest.mark.unit, pytest.mark.graybox]

_CONTENT_ENDPOINT = "drives/demo-drive/items/demo-item/content"


@dataclass
class _CutOffResponse(FakeResponse):
    cause: Exception | None = None

    def iter_content(self, chunk_size: int = 65536) -> Iterator[bytes]:
        _ = chunk_size
        yield b"partial"
        if self.cause is not None:
            raise self.cause


@pytest.mark.parametrize(
    "cause",
    [
        requests_errors.ChunkedEncodingError(
            "Connection broken: IncompleteRead(7 bytes read, 93 more expected)"
        ),
        requests_errors.ConnectionError("Connection reset by peer"),
    ],
    ids=["short-body", "connection-reset"],
)
def test_sharepoint_download_cut_off_after_headers_raises_a_transient_fetch_error(
    cause: Exception,
) -> None:
    client = sharepoint_client()
    response = _CutOffResponse(status_code=200, cause=cause)
    client.session = FakeSession(responses=[response])

    with pytest.raises(FetchError) as raised:
        client.get_bytes(_CONTENT_ENDPOINT)

    error = raised.value
    assert error.retryable is True
    assert error.status_code is None
    assert error.__cause__ is None
    assert error.__suppress_context__ is True
    assert type(cause).__name__ in str(error)
    assert "demo-item" not in str(error)
    assert client.config.graph_api_url not in str(error)
    assert response.closed is True
