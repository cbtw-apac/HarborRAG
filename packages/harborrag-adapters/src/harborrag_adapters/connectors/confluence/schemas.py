from __future__ import annotations
 
from dataclasses import dataclass
from enum import Enum
from typing import Callable, Literal
import re

AttachmentStatus = Literal["processed", "skipped", "unsupported", "failed"]

class FileType(Enum):
    """Attachment categories a custom parser can be registered against."""

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


CustomAttachmentParser = Callable[[bytes, str], str]


@dataclass
class AttachmentMetadata:
    """Everything downstream chunking/citation needs about one attachment,
    kept separate from the parent page's metadata so a citation can point
    at "this attachment on this page" instead of losing that provenance by
    being flattened into the page body string.
    """

    id: str
    title: str
    media_type: str
    size_bytes: int
    download_url: str
    status: AttachmentStatus
    text: str | None = None
    reason: str | None = None


_MEDIA_TYPE_MAP: dict[str, tuple[FileType, str]] = {
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
    "application/vnd.ms-excel": (FileType.SPREADSHEET, "xls"),
    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet": (
        FileType.SPREADSHEET,
        "xlsx",
    ),
    "application/vnd.ms-excel.sheet.macroenabled.12": (FileType.SPREADSHEET, "xlsm"),
    "application/vnd.ms-excel.sheet.binary.macroenabled.12": (
        FileType.SPREADSHEET,
        "xlsb",
    ),
    "text/csv": (FileType.CSV, "csv"),
    "application/vnd.ms-outlook": (FileType.MESSAGE, "msg"),
    "text/html": (FileType.HTML, "html"),
    "text/plain": (FileType.TEXT, "txt"),
    "text/markdown": (FileType.MARKDOWN, "md"),
    "application/vnd.openxmlformats-officedocument.presentationml.presentation": (
        FileType.PRESENTATION,
        "pptx",
    ),
    "application/vnd.ms-powerpoint.presentation.macroenabled.12": (
        FileType.PRESENTATION,
        "pptx",
    ),
}

_ALLOWED_TOKEN_RE = re.compile(r"^[A-Za-z0-9_-]+$")

DEFAULT_PAGE_SIZE = 25

CONTENT_EXPAND = (
    "body.storage,"
    "body.export_view,"
    "version,"
    "metadata.labels,"
    "history,space,"
    "extensions.position,"
    "ancestors,"
    "children.page"
)

LIGHT_EXPAND = "metadata.labels"