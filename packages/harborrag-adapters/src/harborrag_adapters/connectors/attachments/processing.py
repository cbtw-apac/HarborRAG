"""Inline attachment processing for connectors that fold attachment text into the parent.

Handles:
- Attachment type classification by suffix and media type
- Size, origin and callback checks before download
- Download and parsing into text, with a status per attachment

Does NOT handle attachments dispatched as their own records; ``sources.py`` does.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import TYPE_CHECKING, Literal

from harborrag_adapters.connectors.exceptions import FetchError
from harborrag_core.domain.parser import ParseInput
from harborrag_core.security.redaction import redact_secrets

if TYPE_CHECKING:
    from harborrag_adapters.parsers import HarborParserRegistry

from ..policies.http import require_same_origin_url

AttachmentStatus = Literal["processed", "skipped", "unsupported", "failed"]
CustomAttachmentParser = Callable[[bytes, str], str]
DEFAULT_MAX_ATTACHMENT_SIZE_BYTES = 50 * 1024 * 1024


class FileType(StrEnum):
    """Attachment categories used for parser routing and custom parsers."""

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
    """Outcome of downloading and parsing one attachment.

    Attributes:
        text: Extracted text, set only when ``status`` is ``processed``.
        reason: Why the attachment was not processed, else ``None``.
        created_at: Provider upload time, as reported.
    """

    id: str
    title: str
    media_type: str
    size_bytes: int
    download_url: str
    status: AttachmentStatus
    text: str | None = None
    reason: str | None = None
    created_at: str | None = None


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


logger = logging.getLogger("harborrag.adapters.connectors.attachments.processing")

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
    """Resolve an attachment's file type and extension; the filename suffix wins.

    Args:
        media_type: Media type reported by the provider.
        title: Attachment filename.

    Returns:
        File type and extension, or ``None`` when neither suffix nor media type is known.
    """
    suffix = Path(title).suffix.lower().lstrip(".")
    if suffix in _SUFFIX_TYPE_MAP:
        return _SUFFIX_TYPE_MAP[suffix]
    return MEDIA_TYPE_MAP.get(media_type)


class AttachmentProcessor:
    """Processor that downloads attachments and extracts their text with Harbor parsers.

    Shared by Confluence and Jira. A processing failure is recorded on its attachment
    unless ``fail_on_error`` is set.
    """

    def __init__(
        self,
        *,
        download_fn: Callable[[str], bytes | None],
        base_url: str,
        parser: HarborParserRegistry | None = None,
        custom_parsers: dict[FileType, CustomAttachmentParser] | None = None,
        process_attachment_callback: (Callable[[str, int, str], tuple[bool, str]] | None) = None,
        max_attachment_size_bytes: int | None = DEFAULT_MAX_ATTACHMENT_SIZE_BYTES,
        fail_on_error: bool = False,
        logger_: logging.Logger | None = None,
    ) -> None:
        """Configure download, parsing, limits and error policy.

        Args:
            download_fn: Downloads a URL; ``None`` means an empty body.
            base_url: Provider origin that download URLs must belong to.
            parser: Injected parser registry; never defaulted. Without one, every file
                type needs a ``custom_parsers`` entry or the attachment fails.
            custom_parsers: Parsers by file type, taking precedence over ``parser``.
            process_attachment_callback: Hook ``(media_type, size, title)`` returning
                ``(accepted, reason)``.
            max_attachment_size_bytes: Largest processed attachment, ``None`` for no limit.
            fail_on_error: Raise ``FetchError`` when processing raises, instead of recording
                ``failed``.
            logger_: Logger for per-attachment failures.
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
        """Download and parse each attachment, in order.

        Args:
            attachments: Provider attachment payloads.

        Returns:
            One entry per attachment, with status ``processed``, ``skipped``,
            ``unsupported`` or ``failed``.

        Raises:
            FetchError: If processing fails and ``fail_on_error`` is set.
        """
        return [self._process_one(attachment) for attachment in attachments]

    def _process_one(self, attachment: dict) -> AttachmentMetadata:
        """Return the outcome for one attachment; ``FetchError`` only with ``fail_on_error``."""

        attachment_id = str(attachment.get("id") or "")
        # TODO: read title, media type and upload time inside the attachment boundary.
        title = self._title(attachment)
        media_type = self._media_type(attachment)

        metadata = AttachmentMetadata(
            id=attachment_id,
            title=title,
            media_type=media_type,
            size_bytes=0,
            download_url="",
            status="skipped",
            # Set before the failure boundary below so a skipped, unsupported,
            # or failed attachment still reports when it was uploaded.
            created_at=self._created_at(attachment),
        )
        # One boundary per attachment: a malformed value or a raising callback fails only
        # this attachment.
        try:
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

            classified = self._classify(metadata, media_type, title)
            if classified is None:
                return metadata
            file_type, extension = classified

            content = self._download_within_limit(metadata)
            if content is None:
                return metadata

            text = self._parse_content(content, file_type, extension, title, media_type)

            metadata.status = "processed"
            metadata.text = text
            return metadata
        # TODO: re-raise ``AuthenticationError`` instead of recording it as a failed attachment.
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

    def _download_within_limit(self, metadata: AttachmentMetadata) -> bytes | None:
        """Download one attachment; ``None`` with a reason on ``metadata`` if it fails."""

        content = self._download(metadata.download_url)
        if not content:
            metadata.status = "failed"
            metadata.reason = "download failed or returned no content"
            return None
        if (
            self.max_attachment_size_bytes is not None
            and len(content) > self.max_attachment_size_bytes
        ):
            metadata.reason = (
                f"downloaded size {len(content)} exceeds "
                f"max_attachment_size_bytes {self.max_attachment_size_bytes}"
            )
            return None
        return content

    def _parse_content(
        self,
        content: bytes,
        file_type: FileType,
        extension: str,
        title: str,
        media_type: str,
    ) -> str:
        """Return the text from the custom parser or registry; ``ValueError`` if neither exists."""

        if file_type in self.custom_parsers:
            return self.custom_parsers[file_type](content, extension)
        if self.parser is not None:
            return self.parser.parse(
                ParseInput(
                    content=content,
                    filename=title or f"attachment.{extension}",
                    content_type=media_type or None,
                )
            ).content
        raise ValueError(
            f"No parser configured for attachment type {file_type!r}; "
            "pass an explicit `parser` or a matching custom_parsers entry"
        )

    @staticmethod
    def _classify(
        metadata: AttachmentMetadata,
        media_type: str,
        title: str,
    ) -> tuple[FileType, str] | None:
        """Return the file type and extension, or mark ``metadata`` unsupported (``None``)."""

        classified = classify_attachment(media_type, title)
        if classified is None:
            metadata.status = "unsupported"
            metadata.reason = f"no handler for media_type {media_type!r}"
        return classified

    @staticmethod
    def _title(attachment: dict) -> str:
        return str(
            attachment.get("title") or attachment.get("filename") or attachment.get("name") or ""
        )

    @staticmethod
    def _created_at(attachment: dict) -> str | None:
        """Return the upload time from the first known provider field, or ``None``."""
        version = attachment.get("version")
        history = attachment.get("history")
        value = (
            attachment.get("created")
            or attachment.get("createdDate")
            or (version.get("when") if isinstance(version, dict) else None)
            or (history.get("createdDate") if isinstance(history, dict) else None)
        )
        if value is None:
            return None
        return str(value).strip() or None

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
        """Return an absolute provider-origin download URL; ``ValueError`` if absent."""
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
