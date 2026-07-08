from __future__ import annotations

import logging
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Callable, Literal

from harborrag_adapters.parsers import HarborParser, ParseInput

from .http_utils import require_same_origin_url


AttachmentStatus = Literal["processed", "skipped", "unsupported", "failed"]
CustomAttachmentParser = Callable[[bytes, str], str]
DEFAULT_MAX_ATTACHMENT_SIZE_BYTES = 50 * 1024 * 1024


class FileType(StrEnum):
    """Attachment categories used for parser routing and custom overrides."""

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


@dataclass(slots=True)
class AttachmentMetadata:
    """Result of attempting to download and parse one source attachment."""

    id: str
    title: str
    media_type: str
    size_bytes: int
    download_url: str
    status: AttachmentStatus
    text: str | None = None
    reason: str | None = None


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


logger = logging.getLogger("harborrag.adapters.connectors.attachments")


def classify_attachment(media_type: str, title: str) -> tuple[FileType, str] | None:
    """Resolve an attachment category from filename suffix and media type.

    Source APIs sometimes report generic or misleading MIME types for user
    uploads, so filename suffixes intentionally take precedence where they are
    more specific.
    """
    suffix = Path(title).suffix.lower().lstrip(".")
    if suffix == "csv":
        return FileType.CSV, "csv"
    if suffix in {"md", "mdx"}:
        return FileType.MARKDOWN, suffix
    if suffix in {"pptx", "ppt"}:
        return FileType.PRESENTATION, suffix
    if suffix in {"docx", "doc"}:
        return FileType.DOCUMENT, suffix
    if suffix in {"xlsx", "xls", "xlsm", "xlsb"}:
        return FileType.SPREADSHEET, suffix
    if suffix == "pdf":
        return FileType.PDF, "pdf"
    return MEDIA_TYPE_MAP.get(media_type)


class AttachmentProcessor:
    """Download source attachments and extract text through Harbor parsers.

    This class is shared by Confluence and JIRA. It keeps attachment handling
    outside provider connectors while preserving their safety controls: trusted
    origin checks, pre-download size limits, optional custom parsers, and
    best-effort per-attachment failures.
    """

    def __init__(
        self,
        *,
        download_fn: Callable[[str], bytes | None],
        base_url: str,
        parser: HarborParser | None = None,
        custom_parsers: dict[FileType, CustomAttachmentParser] | None = None,
        process_attachment_callback: Callable[[str, int, str], tuple[bool, str]]
        | None = None,
        max_attachment_size_bytes: int | None = DEFAULT_MAX_ATTACHMENT_SIZE_BYTES,
        fail_on_error: bool = False,
        logger_: logging.Logger | None = None,
    ) -> None:
        self._download = download_fn
        self.base_url = base_url.rstrip("/")
        self.parser = parser or HarborParser()
        self.custom_parsers = custom_parsers or {}
        self.process_attachment_callback = process_attachment_callback
        self.max_attachment_size_bytes = max_attachment_size_bytes
        self.fail_on_error = fail_on_error
        self.logger = logger_ or logger

    def process(self, attachments: list[dict]) -> list[AttachmentMetadata]:
        """Process a provider API attachment list into normalized metadata."""
        return [self._process_one(attachment) for attachment in attachments]

    def _process_one(self, attachment: dict) -> AttachmentMetadata:
        attachment_id = str(attachment.get("id") or "")
        title = self._title(attachment)
        media_type = self._media_type(attachment)
        size_bytes = self._size_bytes(attachment)
        download_url = ""

        metadata = AttachmentMetadata(
            id=attachment_id,
            title=title,
            media_type=media_type,
            size_bytes=size_bytes,
            download_url=download_url,
            status="skipped",
        )
        try:
            metadata.download_url = self._download_url(attachment)
        except ValueError as exc:
            metadata.reason = str(exc)
            return metadata

        if self.process_attachment_callback:
            should_process, reason = self.process_attachment_callback(
                media_type,
                size_bytes,
                title,
            )
            if not should_process:
                metadata.reason = reason
                return metadata

        if (
            self.max_attachment_size_bytes is not None
            and size_bytes > self.max_attachment_size_bytes
        ):
            metadata.reason = (
                f"size {size_bytes} exceeds max_attachment_size_bytes "
                f"{self.max_attachment_size_bytes}"
            )
            return metadata

        classified = classify_attachment(media_type, title)
        if classified is None:
            metadata.status = "unsupported"
            metadata.reason = f"no handler for media_type {media_type!r}"
            return metadata

        file_type, extension = classified
        try:
            content = self._download(metadata.download_url)
            if not content:
                metadata.status = "failed"
                metadata.reason = "download failed or returned no content"
                return metadata
            if (
                self.max_attachment_size_bytes is not None
                and len(content) > self.max_attachment_size_bytes
            ):
                metadata.reason = (
                    f"downloaded size {len(content)} exceeds "
                    f"max_attachment_size_bytes {self.max_attachment_size_bytes}"
                )
                return metadata

            if file_type in self.custom_parsers:
                text = self.custom_parsers[file_type](content, extension)
            else:
                text = self.parser.parse(
                    ParseInput(
                        content=content,
                        filename=title or f"attachment.{extension}",
                        content_type=media_type or None,
                    )
                ).content

            metadata.status = "processed"
            metadata.text = text
            return metadata
        except Exception as exc:  # noqa: BLE001 - attachment boundary
            self.logger.warning(
                "Failed to process attachment %s (%s): %s",
                title,
                attachment_id,
                exc,
            )
            if self.fail_on_error:
                raise
            metadata.status = "failed"
            metadata.reason = str(exc)
            return metadata

    @staticmethod
    def _title(attachment: dict) -> str:
        return str(
            attachment.get("title")
            or attachment.get("filename")
            or attachment.get("name")
            or ""
        )

    @staticmethod
    def _media_type(attachment: dict) -> str:
        return str(
            attachment.get("mimeType")
            or attachment.get("mediaType")
            or attachment.get("metadata", {}).get("mediaType")
            or ""
        )

    @staticmethod
    def _size_bytes(attachment: dict) -> int:
        return int(
            attachment.get("size")
            or attachment.get("sizeBytes")
            or attachment.get("extensions", {}).get("fileSize")
            or 0
        )

    def _download_url(self, attachment: dict) -> str:
        """Build a trusted absolute download URL from provider attachment data."""
        download = str(
            attachment.get("content")
            or attachment.get("downloadUrl")
            or attachment.get("_links", {}).get("download")
            or ""
        )
        if not download:
            raise ValueError("Attachment is missing a download URL")
        if download.startswith("http://") or download.startswith("https://"):
            return require_same_origin_url(
                download,
                self.base_url,
                label="attachment download",
            )
        if download.startswith("/"):
            return f"{self.base_url}{download}"
        return f"{self.base_url}/{download}"
