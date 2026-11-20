"""Regression: inline attachment titles that end in whitespace keep their extension."""

from __future__ import annotations

import pytest

from harborrag_adapters.connectors.attachments.processing import (
    AttachmentMetadata,
    AttachmentProcessor,
    FileType,
)
from harborrag_adapters.connectors.file_filters import ExtensionFilter

pytestmark = pytest.mark.unit

BASE_URL = "https://wiki.example.com"
TITLES_ENDING_IN_WHITESPACE = [
    "notes.txt ",
    "notes.txt\u00a0",
    "notes.txt\u202f",
    "notes.txt\u200b",
]


def _process(title: str, extension_filter: ExtensionFilter) -> AttachmentMetadata:
    processor = AttachmentProcessor(
        download_fn=lambda _url: b"hello world",
        base_url=BASE_URL,
        custom_parsers={FileType.TEXT: lambda content, _extension: content.decode()},
        extension_filter=extension_filter,
    )
    attachment = {
        "id": "att-1",
        "title": title,
        "mediaType": "text/plain",
        "size": 11,
        "downloadUrl": "/download/att-1",
    }
    [metadata] = processor.process([attachment])
    return metadata


@pytest.mark.parametrize("title", TITLES_ENDING_IN_WHITESPACE)
def test_inline_attachment_title_ending_in_whitespace_passes_the_allowed_extensions(
    title: str,
) -> None:
    """An allowed extension followed by whitespace is still allowed.

    The title is trimmed of whitespace, as the gateway path trims it.
    """
    metadata = _process(title, ExtensionFilter.of(allowed=[".txt"]))

    assert metadata.status == "processed"
    assert metadata.title == title.strip()


@pytest.mark.parametrize("title", TITLES_ENDING_IN_WHITESPACE)
def test_inline_attachment_title_ending_in_whitespace_is_still_excluded(title: str) -> None:
    """Whitespace after an excluded extension does not slip past the exclusion."""
    metadata = _process(title, ExtensionFilter.of(allowed=[], excluded=["txt"]))

    assert metadata.status == "skipped"
