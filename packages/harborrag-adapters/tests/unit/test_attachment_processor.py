"""White-box unit tests for attachment classification and processing."""
from __future__ import annotations

import pytest

from harborrag_adapters.connectors.attachments import (
    AttachmentProcessor,
    FileType,
    classify_attachment,
)


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


BASE_URL = "https://wiki.example.com"


def _text_processor(**overrides) -> AttachmentProcessor:
    defaults = dict(
        download_fn=lambda url: b"hello world",
        base_url=BASE_URL,
        custom_parsers={FileType.TEXT: lambda content, ext: content.decode()},
    )
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
    with pytest.raises(RuntimeError, match="parser exploded"):
        processor.process([_attachment()])


def test_attachment_error_without_fail_on_error_marks_failed():
    def boom(content: bytes, ext: str) -> str:
        raise RuntimeError("parser exploded")

    processor = _text_processor(custom_parsers={FileType.TEXT: boom})
    [result] = processor.process([_attachment()])

    assert result.status == "failed"
    assert "parser exploded" in result.reason


def test_attachment_cross_origin_download_url_rejected():
    processor = _text_processor()
    [result] = processor.process(
        [_attachment(downloadUrl="https://evil.example.com/steal")]
    )

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
