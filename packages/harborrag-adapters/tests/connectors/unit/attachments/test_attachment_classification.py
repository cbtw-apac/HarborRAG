"""White-box unit tests for attachment classification."""

from __future__ import annotations

import pytest

from harborrag_adapters.connectors.attachments.classification import (
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
