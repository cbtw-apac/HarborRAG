"""SharePoint skip reasons, skip recording, and item-level error mapping."""

from __future__ import annotations

import logging

from harborrag_adapters.connectors.exceptions import (
    AuthorizationError,
    DocumentProcessingError,
    FetchError,
    RateLimitError,
)
from harborrag_adapters.connectors.schemas import ConnectorSkip
from harborrag_core.ingestion import UnsupportedDocumentError

logger = logging.getLogger("harborrag.adapters.connectors.sharepoint")

NOT_IN_ALLOWLIST = "NOT_IN_ALLOWLIST"
UNSUPPORTED_EXTENSION = "UNSUPPORTED_EXTENSION"
EXCEEDS_SIZE_LIMIT = "EXCEEDS_SIZE_LIMIT"
DELETED = "DELETED"
ACCESS_DENIED = "ACCESS_DENIED"
EXTRACTION_FAILED = "EXTRACTION_FAILED"
TRANSIENT_ERROR_MAX_RETRIES = "TRANSIENT_ERROR_MAX_RETRIES"

_TRANSIENT_STATUS = {429, 500, 502, 503, 504}


class SharePointItemSkippedError(DocumentProcessingError, UnsupportedDocumentError):
    """Raised when a direct load declines one item for a standard skip reason.

    The run treats it as a per-item, non-retryable failure, never a fatal one:
    the connector has already retried transient errors up to ``max_retries``.
    The ``skip_reason`` attribute carries the machine-readable reason code.
    """

    def __init__(
        self,
        reason: str,
        detail: str,
        *,
        item_id: str | None = None,
        url: str | None = None,
    ) -> None:
        """Keep the reason and item identity next to the human message."""
        super().__init__(f"{reason}: {detail}")
        self.skip_reason = reason
        self.item_id = item_id
        self.url = url


def skip_reason_for_error(error: Exception) -> str | None:
    """Map an item-level Graph failure to a standard skip reason.

    Returns ``None`` for failures that are not about one item, so callers
    re-raise them instead of skipping.
    """
    if isinstance(error, AuthorizationError):
        return ACCESS_DENIED
    if isinstance(error, RateLimitError):
        return TRANSIENT_ERROR_MAX_RETRIES
    if isinstance(error, FetchError):
        if error.status_code == 404:
            return DELETED
        if error.status_code in _TRANSIENT_STATUS:
            return TRANSIENT_ERROR_MAX_RETRIES
    return None


class SharePointSkipRecorder:
    """Collect and log the items one discovery traversal declined."""

    def __init__(self) -> None:
        """Start with no recorded skips."""
        self._skips: dict[tuple[str, str], ConnectorSkip] = {}

    @property
    def skipped(self) -> tuple[ConnectorSkip, ...]:
        """Return skips in the order they were recorded."""
        return tuple(self._skips.values())

    def reset(self) -> None:
        """Forget skips from a previous traversal."""
        self._skips.clear()

    def record(
        self,
        reason: str,
        detail: str,
        *,
        item_id: str | None = None,
        url: str | None = None,
        path: str | None = None,
    ) -> None:
        """Record one skip once per item and reason, and log it."""
        identity = item_id or url or path or detail
        key = (identity, reason)
        if key in self._skips:
            return
        self._skips[key] = ConnectorSkip(
            path=path or url or item_id or "",
            reason=reason,
            detail=detail,
            item_id=item_id,
            url=url,
        )
        log_skip(reason, detail, item_id=item_id, url=url)


def log_skip(
    reason: str,
    detail: str,
    *,
    item_id: str | None = None,
    url: str | None = None,
) -> None:
    """Emit one skip log entry with its reason, item identity, and detail."""
    logger.warning(
        "SharePoint item skipped reason=%s item_id=%s url=%s detail=%s",
        reason,
        item_id,
        url,
        detail,
        extra={
            "skip_reason": reason,
            "item_id": item_id,
            "url": url,
            "detail": detail,
        },
    )
