"""SharePoint skip reasons, skip recording, and item-level error mapping."""

from __future__ import annotations

import json
import logging
from collections import Counter

from harborrag_adapters.connectors.exceptions import (
    AuthorizationError,
    DocumentProcessingError,
    FetchError,
    ItemNotFoundError,
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
    re-running the load cannot change an allowlist, size, extension, or access
    decision. A deleted item and exhausted retries raise
    :class:`SharePointItemMissingError` and :class:`SharePointItemUnavailableError`
    instead. The ``skip_reason`` attribute carries the machine-readable reason code.
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


class SharePointItemMissingError(ItemNotFoundError):
    """Raised when a direct load finds the item gone (Graph answered HTTP 404).

    Classified as a missing source item, like Jira and Confluence, rather than
    as an unsupported document: it was deleted or moved after discovery listed it.
    """

    def __init__(
        self,
        detail: str,
        *,
        item_id: str | None = None,
        url: str | None = None,
    ) -> None:
        """Keep the DELETED reason and item identity next to the human message."""
        super().__init__(f"{DELETED}: {detail}", status_code=404)
        self.skip_reason = DELETED
        self.item_id = item_id
        self.url = url


class SharePointItemUnavailableError(FetchError):
    """Raised when Graph still throttles or fails one item after ``max_retries``.

    A 429 or 5xx outlasting a few seconds of backoff says nothing about the
    document, so this stays a retryable source-unavailable failure.
    """

    def __init__(
        self,
        detail: str,
        *,
        status_code: int | None = None,
        item_id: str | None = None,
        url: str | None = None,
    ) -> None:
        """Keep the TRANSIENT_ERROR_MAX_RETRIES reason and item identity."""
        super().__init__(f"{TRANSIENT_ERROR_MAX_RETRIES}: {detail}", status_code=status_code)
        self.skip_reason = TRANSIENT_ERROR_MAX_RETRIES
        self.item_id = item_id
        self.url = url


def item_load_error(
    reason: str,
    error: Exception,
    *,
    item_id: str | None = None,
    url: str | None = None,
) -> Exception:
    """Return the typed error a direct load raises for one item-level failure."""
    detail = str(error)
    if reason == DELETED:
        return SharePointItemMissingError(detail, item_id=item_id, url=url)
    if reason == TRANSIENT_ERROR_MAX_RETRIES:
        status_code = getattr(error, "status_code", None)
        return SharePointItemUnavailableError(
            detail, status_code=status_code, item_id=item_id, url=url
        )
    return SharePointItemSkippedError(reason, detail, item_id=item_id, url=url)


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

    def __init__(self, run_id: str | None = None) -> None:
        """Start with no recorded skips; ``run_id`` tags every skip log entry."""
        self.run_id = run_id
        self._skips: dict[tuple[str, str], ConnectorSkip] = {}

    @property
    def skipped(self) -> tuple[ConnectorSkip, ...]:
        """Return skips in the order they were recorded."""
        return tuple(self._skips.values())

    def counts_by_reason(self) -> dict[str, int]:
        """Return how many items were skipped for each standard reason."""
        return dict(Counter(skip.reason for skip in self._skips.values()))

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
        log_skip(reason, detail, item_id=item_id, url=url, run_id=self.run_id)


def log_skip(
    reason: str,
    detail: str,
    *,
    item_id: str | None = None,
    url: str | None = None,
    run_id: str | None = None,
) -> None:
    """Emit one skip log entry with its reason, item identity, and detail."""
    log_event(
        logging.WARNING,
        "sharepoint.item_skipped",
        run_id=run_id,
        skip_reason=reason,
        item_id=item_id,
        url=url,
        detail=detail,
    )


def log_event(level: int, event: str, *, run_id: str | None, **fields: object) -> None:
    """Emit one structured SharePoint log entry as a single JSON object.

    The message itself is JSON so the entry stays machine-readable whatever
    formatter the host process installs; the same fields are attached as
    ``extra`` for handlers that read record attributes.
    """
    payload = {"event": event, "run_id": run_id, **fields}
    # ``logging`` refuses ``extra`` keys that collide with LogRecord attributes,
    # so every attached field carries a provider prefix.
    extra = {f"sharepoint_{key}": value for key, value in payload.items()}
    logger.log(level, json.dumps(payload, default=str, sort_keys=True), extra=extra)
