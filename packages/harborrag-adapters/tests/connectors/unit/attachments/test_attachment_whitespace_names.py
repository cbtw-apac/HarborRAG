"""Regression: inline attachment titles that end in whitespace keep their extension."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from harborrag_adapters.connectors.attachments import (
    AttachmentSourceGateway,
    AttachmentSourcePolicy,
)
from harborrag_adapters.connectors.attachments.processing import (
    AttachmentMetadata,
    AttachmentProcessor,
    FileType,
)
from harborrag_adapters.connectors.file_filters import ExtensionFilter
from harborrag_core.domain.parser import ParseInput

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
    """An allowed extension followed by whitespace is still allowed, and the title is trimmed."""
    metadata = _process(title, ExtensionFilter.of(allowed=[".txt"]))

    assert metadata.status == "processed"
    assert metadata.title == "notes.txt"


@pytest.mark.parametrize("title", TITLES_ENDING_IN_WHITESPACE)
def test_inline_attachment_title_ending_in_whitespace_is_still_excluded(title: str) -> None:
    """Whitespace after an excluded extension does not slip past the exclusion."""
    metadata = _process(title, ExtensionFilter.of(allowed=[], excluded=["txt"]))

    assert metadata.status == "skipped"


class _RecordingParser:
    """Parser registry stand-in that records the file name each parse receives."""

    def __init__(self) -> None:
        self.filenames: list[str | None] = []

    def parse(self, parse_input: ParseInput) -> SimpleNamespace:
        self.filenames.append(parse_input.filename)
        return SimpleNamespace(content="parsed")


def test_inline_attachment_parser_receives_the_trimmed_file_name() -> None:
    """The parser routes by a file name without the zero-width space that followed it."""
    parser = _RecordingParser()
    processor = AttachmentProcessor(
        download_fn=lambda _url: b"%PDF",
        base_url=BASE_URL,
        parser=parser,
        extension_filter=ExtensionFilter.of(allowed=[".docx"]),
    )
    attachment = {
        "id": "att-2",
        "title": "report.docx\u200b",
        "mediaType": "application/octet-stream",
        "size": 4,
        "downloadUrl": "/download/att-2",
    }

    [metadata] = processor.process([attachment])

    assert metadata.status == "processed"
    assert parser.filenames == ["report.docx"]


def test_attachment_gateway_title_drops_a_trailing_zero_width_space() -> None:
    """A described attachment carries the title a user sees, which the parser later routes by."""
    gateway = AttachmentSourceGateway(
        download_fn=lambda _url, max_bytes=None: b"%PDF",
        policy=AttachmentSourcePolicy(base_url=BASE_URL, extensions=ExtensionFilter()),
    )
    attachment = {
        "id": "att-3",
        "title": "Resume.pdf\u200b",
        "metadata": {"mediaType": "application/octet-stream"},
        "extensions": {"fileSize": 4},
        "_links": {"download": "/download/attachments/3/Resume.pdf"},
    }

    [descriptor] = gateway.describe([attachment])

    assert descriptor.title == "Resume.pdf"
