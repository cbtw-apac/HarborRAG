from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import TYPE_CHECKING, Literal

from harborrag_core.security.redaction import redact_secrets

from harborrag_adapters.connectors.exceptions import FetchError
from harborrag_adapters.parsers import ParseInput

if TYPE_CHECKING:
    from harborrag_adapters.parsers import HarborParser

from ..utils.http import require_same_origin_url

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


logger = logging.getLogger("harborrag.adapters.connectors.shared.attachments")

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
        process_attachment_callback: (Callable[[str, int, str], tuple[bool, str]] | None) = None,
        max_attachment_size_bytes: int | None = DEFAULT_MAX_ATTACHMENT_SIZE_BYTES,
        fail_on_error: bool = False,
        logger_: logging.Logger | None = None,
    ) -> None:
        """Configure attachment download, parsing, limits, and error policy.

        ``parser`` should be an explicitly constructed/injected ``HarborParser``
        (e.g. the runtime's profile-configured instance). This class never
        constructs a default parser itself, since a silent default would
        bypass runtime-configured PDF backends and parser policies. Passing
        ``None`` is only safe when every attachment type is covered by
        ``custom_parsers``; otherwise parsing an uncovered attachment raises
        (caught by the per-attachment failure boundary below).
        """
        self._download = download_fn
        self.base_url = base_url.rstrip("/")
        self.parser = parser
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

        metadata = AttachmentMetadata(
            id=attachment_id,
            title=title,
            media_type=media_type,
            size_bytes=0,
            download_url="",
            status="skipped",
        )
        try:
            # Everything below (size/URL normalization, the caller callback,
            # size limits, classification, download, and parsing) runs inside
            # this one boundary so a malformed provider value or a raising
            # callback degrades to a per-attachment failure instead of
            # aborting every remaining attachment.
            metadata.size_bytes = self._size_bytes(attachment)

            try:
                metadata.download_url = self._download_url(attachment)
            except ValueError as exc:
                metadata.reason = str(exc)
                return metadata

            if self.process_attachment_callback:
                should_process, reason = self.process_attachment_callback(
                    media_type,
                    metadata.size_bytes,
                    title,
                )
                if not should_process:
                    metadata.reason = reason
                    return metadata

            if (
                self.max_attachment_size_bytes is not None
                and metadata.size_bytes > self.max_attachment_size_bytes
            ):
                metadata.reason = (
                    f"size {metadata.size_bytes} exceeds max_attachment_size_bytes "
                    f"{self.max_attachment_size_bytes}"
                )
                return metadata

            classified = classify_attachment(media_type, title)
            if classified is None:
                metadata.status = "unsupported"
                metadata.reason = f"no handler for media_type {media_type!r}"
                return metadata
            file_type, extension = classified

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
            elif self.parser is not None:
                text = self.parser.parse(
                    ParseInput(
                        content=content,
                        filename=title or f"attachment.{extension}",
                        content_type=media_type or None,
                    )
                ).content
            else:
                raise ValueError(
                    f"No parser configured for attachment type {file_type!r}; "
                    "pass an explicit `parser` or a matching custom_parsers entry"
                )

            metadata.status = "processed"
            metadata.text = text
            return metadata
        except Exception as exc:  # noqa: BLE001 - attachment boundary
            safe_reason = redact_secrets(str(exc))
            self.logger.warning(
                "Failed to process attachment %s (%s): %s",
                title,
                attachment_id,
                safe_reason,
            )
            if self.fail_on_error:
                raise FetchError(safe_reason) from exc
            metadata.status = "failed"
            metadata.reason = safe_reason
            return metadata

    @staticmethod
    def _title(attachment: dict) -> str:
        return str(
            attachment.get("title") or attachment.get("filename") or attachment.get("name") or ""
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
