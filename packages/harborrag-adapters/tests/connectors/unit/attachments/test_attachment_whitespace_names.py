"""Regression: attachment titles that end in whitespace keep their extension."""

from __future__ import annotations

import pytest

from harborrag_adapters.connectors.attachments import (
    AttachmentSourceDescriptor,
    AttachmentSourceGateway,
    AttachmentSourcePolicy,
    FileType,
)
from harborrag_adapters.connectors.attachments.classification import (
    attachment_extension,
    classify_attachment,
)
from harborrag_adapters.connectors.file_filters import ExtensionFilter

pytestmark = pytest.mark.unit

BASE_URL = "https://wiki.example.com"
TITLES_ENDING_IN_WHITESPACE = [
    "notes.txt ",
    "notes.txt ",
    "notes.txt ",
    "notes.txt​",
]


def _describe(title: str, extensions: ExtensionFilter) -> AttachmentSourceDescriptor:
    gateway = AttachmentSourceGateway(
        download_fn=lambda _url, max_bytes=None: b"hello world",
        policy=AttachmentSourcePolicy(base_url=BASE_URL, extensions=extensions),
    )
    attachment = {
        "id": "att-1",
        "title": title,
        "mediaType": "text/plain",
        "size": 11,
        "downloadUrl": "/download/att-1",
    }
    [descriptor] = gateway.describe([attachment])
    return descriptor


@pytest.mark.parametrize("title", TITLES_ENDING_IN_WHITESPACE)
def test_attachment_title_ending_in_whitespace_passes_the_allowed_extensions(title: str) -> None:
    """An allowed extension followed by whitespace is still allowed, and the title is trimmed."""
    descriptor = _describe(title, ExtensionFilter.of(allowed=[".txt"]))

    assert descriptor.status == "admitted"
    assert descriptor.title == "notes.txt"


@pytest.mark.parametrize("title", TITLES_ENDING_IN_WHITESPACE)
def test_attachment_title_ending_in_whitespace_is_still_excluded(title: str) -> None:
    """Whitespace after an excluded extension does not slip past the exclusion."""
    descriptor = _describe(title, ExtensionFilter.of(allowed=[], excluded=["txt"]))

    assert descriptor.status == "skipped"


@pytest.mark.parametrize("title", TITLES_ENDING_IN_WHITESPACE)
def test_classification_reads_the_extension_before_trailing_whitespace(title: str) -> None:
    """The classifier routes by the extension a user sees, not by the space after it."""
    assert attachment_extension("application/octet-stream", title) == ".txt"
    assert classify_attachment("application/octet-stream", title) == (FileType.TEXT, "txt")


def test_attachment_gateway_title_drops_a_trailing_zero_width_space() -> None:
    """A described attachment carries the title a user sees, which the parser later routes by."""
    gateway = AttachmentSourceGateway(
        download_fn=lambda _url, max_bytes=None: b"%PDF",
        policy=AttachmentSourcePolicy(base_url=BASE_URL, extensions=ExtensionFilter()),
    )
    attachment = {
        "id": "att-3",
        "title": "report.pdf​",
        "metadata": {"mediaType": "application/octet-stream"},
        "extensions": {"fileSize": 4},
        "_links": {"download": "/download/attachments/3/report.pdf"},
    }

    [descriptor] = gateway.describe([attachment])

    assert descriptor.title == "report.pdf"
