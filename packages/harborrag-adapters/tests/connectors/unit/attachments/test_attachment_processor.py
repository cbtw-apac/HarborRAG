"""White-box unit tests for attachment classification and processing."""

from __future__ import annotations

import pytest

from harborrag_adapters.connectors.attachments.processing import (
    AttachmentProcessor,
    FileType,
    classify_attachment,
)
from harborrag_adapters.connectors.exceptions import FetchError

pytestmark = [pytest.mark.unit, pytest.mark.whitebox]


def test_classify_prefers_filename_suffix_over_media_type():
    assert classify_attachment("application/octet-stream", "data.csv") == (
        FileType.CSV,
        "csv",
    )


def test_classify_falls_back_to_media_type_map():
    assert classify_attachment("image/png", "screenshot") == (FileType.IMAGE, "png")


def test_classify_unknown_returns_none():
    assert classify_attachment("application/x-mystery", "thing.bin") is None


def test_classify_presentation_suffix():
    assert classify_attachment("application/octet-stream", "deck.pptx") == (
        FileType.PRESENTATION,
        "pptx",
    )


def test_classify_document_suffix():
    assert classify_attachment("application/octet-stream", "report.docx") == (
        FileType.DOCUMENT,
        "docx",
    )


def test_classify_spreadsheet_suffix():
    assert classify_attachment("application/octet-stream", "sheet.xlsx") == (
        FileType.SPREADSHEET,
        "xlsx",
    )


def test_classify_pdf_suffix():
    assert classify_attachment("application/octet-stream", "doc.pdf") == (
        FileType.PDF,
        "pdf",
    )


def test_classify_odt_suffix():
    assert classify_attachment("application/octet-stream", "notes.odt") == (
        FileType.DOCUMENT,
        "odt",
    )


def test_classify_odt_media_type():
    assert classify_attachment("application/vnd.oasis.opendocument.text", "notes") == (
        FileType.DOCUMENT,
        "odt",
    )


@pytest.mark.parametrize(
    ("title", "expected"),
    [
        ("notes.txt", (FileType.TEXT, "txt")),
        ("page.html", (FileType.HTML, "html")),
        ("image.png", (FileType.IMAGE, "png")),
        ("data.json", (FileType.TEXT, "json")),
        ("book.epub", (FileType.DOCUMENT, "epub")),
        ("notes.odt", (FileType.DOCUMENT, "odt")),
    ],
)
def test_classify_routes_generic_mime_attachments_by_suffix(title, expected):
    # Providers often report a generic/incorrect MIME type (application/
    # octet-stream) for these formats; the suffix must still route correctly.
    assert classify_attachment("application/octet-stream", title) == expected


BASE_URL = "https://wiki.example.com"


def _text_processor(**overrides) -> AttachmentProcessor:
    defaults = {
        "download_fn": lambda url: b"hello world",
        "base_url": BASE_URL,
        "custom_parsers": {FileType.TEXT: lambda content, ext: content.decode()},
    }
    defaults.update(overrides)
    return AttachmentProcessor(**defaults)


def _attachment(**overrides) -> dict:
    data = {
        "id": "att-1",
        "title": "notes.txt",
        "mediaType": "text/plain",
        "size": 11,
        "downloadUrl": "/download/notes.txt",
    }
    data.update(overrides)
    return data


def test_attachment_processed_happy_path():
    processor = _text_processor()
    [result] = processor.process([_attachment()])

    assert result.status == "processed"
    assert result.text == "hello world"
    assert result.download_url == f"{BASE_URL}/download/notes.txt"
    assert result.id == "att-1"


def test_attachment_skipped_when_size_exceeds_max():
    downloaded: list[str] = []

    def download(url: str) -> bytes:
        downloaded.append(url)
        return b"x"

    processor = _text_processor(
        download_fn=download,
        max_attachment_size_bytes=5,
    )
    [result] = processor.process([_attachment(size=5000)])

    assert result.status == "skipped"
    assert "exceeds max_attachment_size_bytes" in result.reason
    assert downloaded == []


def test_attachment_unsupported_media_type():
    processor = _text_processor()
    [result] = processor.process(
        [_attachment(title="mystery.bin", mediaType="application/x-mystery")]
    )

    assert result.status == "unsupported"
    assert "no handler" in result.reason


def test_attachment_failed_when_download_returns_none():
    processor = _text_processor(download_fn=lambda url: None)
    [result] = processor.process([_attachment()])

    assert result.status == "failed"
    assert "download failed" in result.reason


def test_attachment_fail_on_error_reraises():
    def boom(content: bytes, ext: str) -> str:
        raise RuntimeError("parser exploded")

    processor = _text_processor(
        custom_parsers={FileType.TEXT: boom},
        fail_on_error=True,
    )
    with pytest.raises(FetchError, match="parser exploded"):
        processor.process([_attachment()])


def test_attachment_fail_on_error_redacts_secret_in_reraised_error():
    def boom(content: bytes, ext: str) -> str:
        raise RuntimeError("token=SECRET123 leaked")

    processor = _text_processor(
        custom_parsers={FileType.TEXT: boom},
        fail_on_error=True,
    )
    with pytest.raises(FetchError) as excinfo:
        processor.process([_attachment()])

    assert "SECRET123" not in str(excinfo.value)


def test_attachment_error_without_fail_on_error_marks_failed():
    def boom(content: bytes, ext: str) -> str:
        raise RuntimeError("parser exploded")

    processor = _text_processor(custom_parsers={FileType.TEXT: boom})
    [result] = processor.process([_attachment()])

    assert result.status == "failed"
    assert "parser exploded" in result.reason


def test_attachment_without_parser_or_matching_custom_parser_marks_failed():
    # No general-purpose `parser` and no custom_parsers entry for this
    # FileType: must fail the single attachment, not crash the whole batch.
    processor = AttachmentProcessor(
        download_fn=lambda url: b"hello world",
        base_url=BASE_URL,
    )
    [result] = processor.process([_attachment()])

    assert result.status == "failed"
    assert "No parser configured" in result.reason


def test_attachment_malformed_size_does_not_crash_the_batch():
    processor = _text_processor()
    [result] = processor.process([_attachment(size="not-a-number")])

    assert result.status == "failed"


def test_attachment_callback_exception_does_not_crash_the_batch():
    def raising_callback(media_type: str, size_bytes: int, title: str):
        raise RuntimeError("callback exploded")

    processor = _text_processor(process_attachment_callback=raising_callback)
    [result] = processor.process([_attachment()])

    assert result.status == "failed"
    assert "callback exploded" in result.reason


def test_attachment_failure_reason_is_redacted():
    def boom(content: bytes, ext: str) -> str:
        raise RuntimeError("token=abc123secret")

    processor = _text_processor(custom_parsers={FileType.TEXT: boom})
    [result] = processor.process([_attachment()])

    assert result.status == "failed"
    assert "abc123secret" not in result.reason


def test_attachment_cross_origin_download_url_rejected():
    processor = _text_processor()
    [result] = processor.process([_attachment(downloadUrl="https://evil.example.com/steal")])

    assert result.status == "skipped"
    assert "outside trusted origin" in result.reason


def test_attachment_callback_can_skip():
    processor = _text_processor(
        process_attachment_callback=lambda media, size, title: (False, "policy denied"),
    )
    [result] = processor.process([_attachment()])

    assert result.status == "skipped"
    assert result.reason == "policy denied"


def test_attachment_downloaded_body_over_cap_is_skipped():
    processor = _text_processor(
        download_fn=lambda url: b"x" * 100,
        max_attachment_size_bytes=10,
    )
    [result] = processor.process([_attachment(size=1)])

    assert result.status == "skipped"
    assert "downloaded size" in result.reason


def test_attachment_callback_can_allow():
    processor = _text_processor(
        process_attachment_callback=lambda media, size, title: (True, ""),
    )
    [result] = processor.process([_attachment()])

    assert result.status == "processed"


def test_attachment_missing_download_url_is_skipped():
    processor = _text_processor()
    [result] = processor.process([_attachment(downloadUrl="")])

    assert result.status == "skipped"
    assert "missing a download URL" in result.reason


def test_attachment_relative_download_url_without_leading_slash():
    processor = _text_processor()
    [result] = processor.process([_attachment(downloadUrl="download/notes.txt")])

    assert result.status == "processed"
    assert result.download_url == f"{BASE_URL}/download/notes.txt"
