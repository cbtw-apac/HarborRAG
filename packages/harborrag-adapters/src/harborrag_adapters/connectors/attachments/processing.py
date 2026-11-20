"""Inline attachment download and parsing shared by the Confluence and Jira connectors.

Each attachment is checked for a trusted origin and its size before download, and a
failure stays on that one attachment. A failure log names the attachment by ID, not
by title.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass
from enum import StrEnum
from typing import TYPE_CHECKING, Literal

from harborrag_adapters.connectors.exceptions import FetchError
from harborrag_adapters.connectors.file_filters import ExtensionFilter, file_name_extension
from harborrag_adapters.connectors.limits import DEFAULT_MAX_DOCUMENT_BYTES
from harborrag_core.domain.parser import ParseInput
from harborrag_core.security.redaction import redact_secrets

if TYPE_CHECKING:
    from harborrag_adapters.parsers import HarborParserRegistry

from ..policies.http import require_same_origin_url

AttachmentStatus = Literal["processed", "skipped", "unsupported", "failed"]
CustomAttachmentParser = Callable[[bytes, str], str]
DEFAULT_MAX_ATTACHMENT_SIZE_BYTES = DEFAULT_MAX_DOCUMENT_BYTES


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
    """Result of attempting to download and parse one source attachment.

    Attributes:
        id: Provider ID of the attachment.
        title: File name with surrounding whitespace removed.
        media_type: Media type the provider reported, or ``""``.
        size_bytes: Size the provider reported before download.
        download_url: Trusted absolute download URL, or ``""`` when none was usable.
        status: Outcome of the attempt.
        text: Parsed text of a processed attachment.
        reason: Why the attachment was skipped, unsupported or failed.
        created_at: When the provider recorded the upload, if it says.
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
    """Resolve an attachment category from filename suffix and media type.

    Source APIs sometimes report generic or misleading MIME types for user
    uploads, so filename suffixes intentionally take precedence where they are
    more specific.
    """
    suffix = file_name_extension(title).lstrip(".")
    if suffix in _SUFFIX_TYPE_MAP:
        return _SUFFIX_TYPE_MAP[suffix]
    return MEDIA_TYPE_MAP.get(media_type)


def attachment_extension(media_type: str, title: str) -> str:
    """Return the dot-prefixed extension of an attachment, from its title or its media type."""
    suffix = file_name_extension(title)
    if suffix:
        return suffix
    classified = classify_attachment(media_type, title)
    return f".{classified[1]}" if classified else ""


class AttachmentProcessor:
    """Download source attachments and extract text through Harbor parsers.

    This class is shared by Confluence and Jira. It keeps attachment handling
    outside provider connectors and keeps their safety controls. Those are trusted
    origin checks, size limits before download, optional custom parsers, and
    failures that stay on one attachment.

    The class never builds a default parser, because a silent default would bypass
    the runtime's configured PDF backends and parser policies.

    Args:
        download_fn: Fetches the bytes at a trusted download URL, or returns None.
        base_url: Provider base URL that every download URL must share an origin with.
        parser: Injected parser registry. None is safe only when ``custom_parsers``
            covers every attachment type, because an uncovered one then fails.
        custom_parsers: Text extractors that replace the registry for a file type.
        process_attachment_callback: Optional caller check on media type, size and
            title that returns whether to process the attachment and why not.
        max_attachment_size_bytes: Largest attachment to download, or None for no limit.
        fail_on_error: Whether an attachment failure raises FetchError instead of
            being recorded on that attachment.
        logger_: Logger for attachment failures, the module logger by default.
        extension_filter: Allowed and excluded file extensions.
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
        extension_filter: ExtensionFilter | None = None,
    ) -> None:
        self._download = download_fn
        self.base_url = base_url.rstrip("/")
        self.parser = parser
        self.custom_parsers = custom_parsers or {}
        self.process_attachment_callback = process_attachment_callback
        self.max_attachment_size_bytes = max_attachment_size_bytes
        self.fail_on_error = fail_on_error
        self.extension_filter = extension_filter or ExtensionFilter()
        self.logger = logger_ or logger

    def process(self, attachments: list[dict]) -> list[AttachmentMetadata]:
        """Process a provider API attachment list into normalized metadata."""
        return [self._process_one(attachment) for attachment in attachments]

    def _process_one(self, attachment: dict) -> AttachmentMetadata:
        """Process one attachment so that any failure stays on that attachment.

        The upload time is read first, so a skipped, unsupported or failed attachment
        still reports it. Everything after that runs inside one failure boundary, so
        a malformed provider value or a raising callback fails this attachment
        instead of aborting the remaining ones.
        """
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
            created_at=self._created_at(attachment),
        )
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

            filtered = self.extension_filter.rejection(attachment_extension(media_type, title))
            if filtered is not None:
                metadata.reason = filtered
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
        except Exception as exc:  # noqa: BLE001 - attachment boundary
            safe_reason = redact_secrets(str(exc))
            self.logger.warning(
                "Failed to process attachment %s: %s",
                attachment_id,
                safe_reason,
            )
            if self.fail_on_error:
                raise FetchError(safe_reason) from exc
            metadata.status = "failed"
            metadata.reason = safe_reason
            return metadata

    def _download_within_limit(self, metadata: AttachmentMetadata) -> bytes | None:
        """Download one attachment and apply the post-download size boundary."""
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
        classified = classify_attachment(media_type, title)
        if classified is None:
            metadata.status = "unsupported"
            metadata.reason = f"no handler for media_type {media_type!r}"
        return classified

    @staticmethod
    def _title(attachment: dict) -> str:
        return str(
            attachment.get("title") or attachment.get("filename") or attachment.get("name") or ""
        ).strip()

    @staticmethod
    def _created_at(attachment: dict) -> str | None:
        """Read when the provider recorded this attachment being uploaded.

        JIRA returns ``created`` directly on the attachment. Confluence carries
        the equivalent on ``version.when`` for the current file version, and on
        ``history.createdDate`` for the original upload; prefer the former so
        a re-uploaded file reports its current timestamp.
        """
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
