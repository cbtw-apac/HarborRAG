"""Admission and download of attachments ingested as documents of their own.

Admission reads only the provider's attachment metadata, so an attachment outside
the policy is never downloaded. Downloads stop at the configured size limit.
"""

from __future__ import annotations

import hashlib
import json
import logging
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, Literal, Protocol

from harborrag_adapters.connectors.exceptions import FetchError
from harborrag_adapters.connectors.file_filters import ExtensionFilter, trim_file_name
from harborrag_adapters.connectors.policies.http import (
    require_same_origin_url,
)
from harborrag_core.security.redaction import redact_secrets

from .classification import (
    DEFAULT_MAX_ATTACHMENT_SIZE_BYTES,
    attachment_extension,
    classify_attachment,
)

AttachmentSourceStatus = Literal[
    "admitted",
    "skipped",
    "unsupported",
    "failed",
]


class BoundedDownload(Protocol):
    """Downloader that streams a response and refuses to buffer past a byte limit."""

    def __call__(self, url: str, *, max_bytes: int | None) -> bytes | None:
        """Stream a response while refusing to buffer more than max_bytes."""
        ...


@dataclass(frozen=True, slots=True)
class AttachmentSourceDescriptor:
    """Fetch-safe metadata for one independently ingestible attachment.

    Attributes:
        attachment_id: Provider ID of the attachment.
        title: File name without surrounding whitespace or zero-width characters.
        media_type: Media type the provider reported, or a generic one.
        size_bytes: Size the provider reported before download.
        download_url: Trusted absolute download URL, or ``""`` when none was usable.
        source_version: Hash of the attachment's identity, title, size and version.
        status: Admission decision for the attachment.
        reason: Why the attachment was not admitted.
    """

    attachment_id: str
    title: str
    media_type: str
    size_bytes: int
    download_url: str
    source_version: str
    status: AttachmentSourceStatus
    reason: str | None = None


@dataclass(frozen=True, slots=True)
class AttachmentSourcePolicy:
    """Admission rules for the attachments of one connection.

    Attributes:
        base_url: Provider base URL that every download URL must share an origin with.
        process_callback: Optional caller check on media type, size and title that
            returns whether to admit the attachment and why not.
        max_size_bytes: Largest attachment to admit, or None for no limit.
        fail_on_error: Whether an unexpected admission error raises instead of
            being recorded on that attachment.
        extensions: Allowed and excluded file extensions.
    """

    base_url: str
    process_callback: Callable[[str, int, str], tuple[bool, str]] | None = None
    max_size_bytes: int | None = DEFAULT_MAX_ATTACHMENT_SIZE_BYTES
    fail_on_error: bool = False
    extensions: ExtensionFilter = field(default_factory=ExtensionFilter)


class AttachmentSourceGateway:
    """Apply attachment admission policy without downloading source bytes.

    Args:
        download_fn: Bounded downloader used to fetch admitted attachments.
        policy: Admission rules for the connection.
        logger_: Logger for admission failures, the module logger by default.
    """

    def __init__(
        self,
        *,
        download_fn: BoundedDownload,
        policy: AttachmentSourcePolicy,
        logger_: logging.Logger | None = None,
    ) -> None:
        self._download = download_fn
        self._policy = policy
        self._logger = logger_ or logging.getLogger(
            "harborrag.adapters.connectors.attachments.sources"
        )

    def describe(
        self,
        attachments: Sequence[Mapping[str, Any]],
    ) -> tuple[AttachmentSourceDescriptor, ...]:
        """Return one admission decision per provider attachment, in order.

        Args:
            attachments: Attachment payloads as the provider returned them.

        Returns:
            One descriptor per attachment, admitted or not.
        """
        return tuple(self._safely_describe(attachment) for attachment in attachments)

    def fetch(self, descriptor: AttachmentSourceDescriptor) -> bytes:
        """Download an admitted attachment and re-enforce the byte limit."""
        if descriptor.status != "admitted":
            raise FetchError("attachment was not admitted for content capture")
        trusted_url = self._trusted_url(descriptor.download_url)
        content = self._download(
            trusted_url,
            max_bytes=self._policy.max_size_bytes,
        )
        if not content:
            raise FetchError("attachment download returned no content")
        if self._policy.max_size_bytes is not None and len(content) > self._policy.max_size_bytes:
            raise FetchError("downloaded attachment exceeds max_attachment_size_bytes")
        return content

    def _safely_describe(
        self,
        attachment: Mapping[str, Any],
    ) -> AttachmentSourceDescriptor:
        try:
            return self._describe_one(attachment)
        except Exception as error:
            safe_reason = redact_secrets(str(error))
            if self._policy.fail_on_error:
                raise FetchError(safe_reason) from error
            self._logger.warning(
                "Attachment descriptor rejected (%s)",
                type(error).__name__,
            )
            return AttachmentSourceDescriptor(
                attachment_id=str(attachment.get("id") or ""),
                title=self._title(attachment),
                media_type=self._media_type(attachment),
                size_bytes=0,
                download_url="",
                source_version=self._failed_version(attachment),
                status="failed",
                reason=safe_reason,
            )

    def _describe_one(
        self,
        attachment: Mapping[str, Any],
    ) -> AttachmentSourceDescriptor:
        attachment_id = str(attachment.get("id") or "").strip()
        title = self._title(attachment)
        media_type = self._media_type(attachment)
        size_bytes = self._size_bytes(attachment)
        download_url = self._download_url(attachment)
        reason: str | None = None
        status: AttachmentSourceStatus = "admitted"
        if not attachment_id or not title:
            raise ValueError("attachment descriptor is missing identity or title")
        if self._policy.process_callback is not None:
            accepted, reason = self._policy.process_callback(
                media_type,
                size_bytes,
                title,
            )
            if not accepted:
                status = "skipped"
        if status == "admitted":
            filtered = self._policy.extensions.rejection(attachment_extension(media_type, title))
            if filtered is not None:
                status, reason = "skipped", filtered
        if (
            status == "admitted"
            and self._policy.max_size_bytes is not None
            and size_bytes > self._policy.max_size_bytes
        ):
            status = "skipped"
            reason = (
                f"size {size_bytes} exceeds max_attachment_size_bytes {self._policy.max_size_bytes}"
            )
        if status == "admitted" and classify_attachment(media_type, title) is None:
            status = "unsupported"
            reason = f"no handler for media_type {media_type!r}"
        return AttachmentSourceDescriptor(
            attachment_id=attachment_id,
            title=title,
            media_type=media_type,
            size_bytes=size_bytes,
            download_url=download_url,
            source_version=self._version(attachment),
            status=status,
            reason=reason,
        )

    def _download_url(self, attachment: Mapping[str, Any]) -> str:
        value = str(
            attachment.get("content")
            or attachment.get("downloadUrl")
            or self._nested(attachment, "_links", "download")
            or ""
        )
        if not value:
            raise ValueError("attachment is missing a download URL")
        return self._trusted_url(value)

    def _trusted_url(self, value: str) -> str:
        if value.startswith(("http://", "https://")):
            return require_same_origin_url(
                value,
                self._policy.base_url.rstrip("/"),
                label="attachment download",
            )
        if value.startswith("/"):
            return f"{self._policy.base_url.rstrip('/')}{value}"
        return f"{self._policy.base_url.rstrip('/')}/{value}"

    @staticmethod
    def _title(attachment: Mapping[str, Any]) -> str:
        return trim_file_name(
            str(
                attachment.get("title")
                or attachment.get("filename")
                or attachment.get("name")
                or ""
            )
        )

    @classmethod
    def _media_type(cls, attachment: Mapping[str, Any]) -> str:
        return str(
            attachment.get("mimeType")
            or attachment.get("mediaType")
            or cls._nested(attachment, "metadata", "mediaType")
            or "application/octet-stream"
        )

    @classmethod
    def _size_bytes(cls, attachment: Mapping[str, Any]) -> int:
        size = (
            attachment.get("size")
            or attachment.get("sizeBytes")
            or cls._nested(attachment, "extensions", "fileSize")
            or 0
        )
        if not isinstance(size, (int, float, str)):
            raise TypeError("attachment size is not a number")
        return int(size)

    @classmethod
    def _version(cls, attachment: Mapping[str, Any]) -> str:
        identity = {
            "id": attachment.get("id"),
            "title": cls._title(attachment),
            "size": cls._size_bytes(attachment),
            "version": cls._nested(attachment, "version", "number"),
            "updated": (
                attachment.get("updated")
                or cls._nested(attachment, "version", "when")
                or attachment.get("created")
            ),
        }
        return hashlib.sha256(
            json.dumps(
                identity,
                default=str,
                separators=(",", ":"),
                sort_keys=True,
            ).encode("utf-8")
        ).hexdigest()

    @classmethod
    def _failed_version(cls, attachment: Mapping[str, Any]) -> str:
        """``_version``, or a hash of id and title when a field it reads is malformed.

        A rejected attachment must stay a ``failed`` descriptor of its own, not
        fail every attachment of its issue or page.
        """
        try:
            return cls._version(attachment)
        except (TypeError, ValueError):
            identity = f"{attachment.get('id')}:{cls._title(attachment)}"
            return hashlib.sha256(identity.encode("utf-8")).hexdigest()

    @staticmethod
    def _nested(
        value: Mapping[str, Any],
        parent: str,
        child: str,
    ) -> object:
        nested = value.get(parent)
        return nested.get(child) if isinstance(nested, Mapping) else None
