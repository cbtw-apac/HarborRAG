"""Classify source attachments by file name and media type."""

from __future__ import annotations

from enum import StrEnum
from pathlib import Path

from harborrag_adapters.connectors.limits import DEFAULT_MAX_DOCUMENT_BYTES

__all__ = [
    "DEFAULT_MAX_ATTACHMENT_SIZE_BYTES",
    "FileType",
    "attachment_extension",
    "classify_attachment",
]

DEFAULT_MAX_ATTACHMENT_SIZE_BYTES = DEFAULT_MAX_DOCUMENT_BYTES


class FileType(StrEnum):
    """Attachment categories resolved from a file name or media type."""

    IMAGE = "image"
    DOCUMENT = "document"
    TEXT = "text"
    HTML = "html"
    CSV = "csv"
    MARKDOWN = "markdown"
    SPREADSHEET = "spreadsheet"
    PRESENTATION = "presentation"
    PDF = "pdf"
    MESSAGE = "message"
    SVG = "svg"


MEDIA_TYPE_MAP: dict[str, tuple[FileType, str]] = {
    "application/pdf": (FileType.PDF, "pdf"),
    "image/png": (FileType.IMAGE, "png"),
    "image/jpg": (FileType.IMAGE, "jpg"),
    "image/jpeg": (FileType.IMAGE, "jpeg"),
    "image/webp": (FileType.IMAGE, "webp"),
    "image/svg+xml": (FileType.SVG, "svg"),
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document": (
        FileType.DOCUMENT,
        "docx",
    ),
    "application/vnd.oasis.opendocument.text": (FileType.DOCUMENT, "odt"),
    "application/vnd.ms-excel": (FileType.SPREADSHEET, "xls"),
    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet": (
        FileType.SPREADSHEET,
        "xlsx",
    ),
    "text/csv": (FileType.CSV, "csv"),
    "text/html": (FileType.HTML, "html"),
    "text/plain": (FileType.TEXT, "txt"),
    "text/markdown": (FileType.MARKDOWN, "md"),
    "application/vnd.openxmlformats-officedocument.presentationml.presentation": (
        FileType.PRESENTATION,
        "pptx",
    ),
}


_SUFFIX_TYPE_MAP: dict[str, tuple[FileType, str]] = {
    "csv": (FileType.CSV, "csv"),
    "tsv": (FileType.CSV, "tsv"),
    "md": (FileType.MARKDOWN, "md"),
    "markdown": (FileType.MARKDOWN, "markdown"),
    "mdx": (FileType.MARKDOWN, "mdx"),
    "pptx": (FileType.PRESENTATION, "pptx"),
    "ppt": (FileType.PRESENTATION, "ppt"),
    "docx": (FileType.DOCUMENT, "docx"),
    "doc": (FileType.DOCUMENT, "doc"),
    "odt": (FileType.DOCUMENT, "odt"),
    "epub": (FileType.DOCUMENT, "epub"),
    "xlsx": (FileType.SPREADSHEET, "xlsx"),
    "xls": (FileType.SPREADSHEET, "xls"),
    "xlsm": (FileType.SPREADSHEET, "xlsm"),
    "xlsb": (FileType.SPREADSHEET, "xlsb"),
    "pdf": (FileType.PDF, "pdf"),
    "html": (FileType.HTML, "html"),
    "htm": (FileType.HTML, "htm"),
    "xhtml": (FileType.HTML, "xhtml"),
    "png": (FileType.IMAGE, "png"),
    "jpg": (FileType.IMAGE, "jpg"),
    "jpeg": (FileType.IMAGE, "jpeg"),
    "webp": (FileType.IMAGE, "webp"),
    "gif": (FileType.IMAGE, "gif"),
    "bmp": (FileType.IMAGE, "bmp"),
    "tiff": (FileType.IMAGE, "tiff"),
    "svg": (FileType.SVG, "svg"),
    "json": (FileType.TEXT, "json"),
    "jsonl": (FileType.TEXT, "jsonl"),
    "ndjson": (FileType.TEXT, "ndjson"),
    "txt": (FileType.TEXT, "txt"),
    "text": (FileType.TEXT, "text"),
}


def classify_attachment(media_type: str, title: str) -> tuple[FileType, str] | None:
    """Resolve an attachment category from filename suffix and media type.

    Source APIs sometimes report generic or misleading MIME types for user
    uploads, so filename suffixes intentionally take precedence where they are
    more specific.
    """
    suffix = Path(title).suffix.lower().lstrip(".")
    if suffix in _SUFFIX_TYPE_MAP:
        return _SUFFIX_TYPE_MAP[suffix]
    return MEDIA_TYPE_MAP.get(media_type)


def attachment_extension(media_type: str, title: str) -> str:
    """Return the dot-prefixed extension of an attachment, from its title or its media type."""
    suffix = Path(title).suffix.lower()
    if suffix:
        return suffix
    classified = classify_attachment(media_type, title)
    return f".{classified[1]}" if classified else ""
