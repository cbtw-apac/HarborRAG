"""Structured skip reporting for local filesystem discovery."""

from __future__ import annotations

import logging
from collections.abc import Iterator
from pathlib import Path

from harborrag_adapters.connectors.schemas import (
    SKIP_REASON_CALLBACK_REJECTED,
    SKIP_REASON_OUT_OF_SCOPE,
    SKIP_REASON_SIZE_LIMIT_EXCEEDED,
    SKIP_REASON_UNREADABLE,
    ConnectorSkip,
)

logger = logging.getLogger("harborrag.adapters.connectors.local")


class LocalSkipReport:
    """Accumulates the paths traversal dropped for a reportable reason."""

    def __init__(self) -> None:
        """Start an empty report."""
        self._skips: dict[tuple[str, str], ConnectorSkip] = {}

    def __len__(self) -> int:
        """Return how many distinct skips have been recorded."""
        return len(self._skips)

    def __iter__(self) -> Iterator[ConnectorSkip]:
        """Iterate recorded skips in first-seen order."""
        return iter(self._skips.values())

    @property
    def entries(self) -> tuple[ConnectorSkip, ...]:
        """Return the recorded skips in first-seen order."""
        return tuple(self._skips.values())

    def clear(self) -> None:
        """Discard every recorded skip, e.g. between independent sync runs."""
        self._skips.clear()

    def record(self, path: str | Path, *, reason: str, detail: str) -> None:
        """Log one skip at warning level and add it to the report.

        A path already reported for the same reason is ignored, so a re-walk
        neither duplicates the entry nor repeats the warning.
        """
        key = (str(path), reason)
        if key in self._skips:
            return
        self._skips[key] = ConnectorSkip(path=str(path), reason=reason, detail=detail)
        logger.warning("Skipping local path %s: %s", path, detail)

    def oversized(self, path: str | Path, *, size: int, limit: int) -> None:
        """Record a file rejected by ``max_file_size_bytes``."""
        self.record(
            path,
            reason=SKIP_REASON_SIZE_LIMIT_EXCEEDED,
            detail=f"exceeds size limit: {size} bytes > max_file_size_bytes {limit}",
        )

    def unreadable(self, path: str | Path, *, error: OSError) -> None:
        """Record a path that could not be listed or stat'ed."""
        self.record(path, reason=SKIP_REASON_UNREADABLE, detail=f"unreadable: {error}")

    def out_of_scope(self, path: str | Path) -> None:
        """Record a path resolving outside the configured source scope."""
        self.record(
            path,
            reason=SKIP_REASON_OUT_OF_SCOPE,
            detail="resolves outside the configured source scope",
        )

    def callback_rejected(self, path: str | Path, *, reason: str) -> None:
        """Record a file the configured ``process_file_callback`` declined."""
        self.record(
            path,
            reason=SKIP_REASON_CALLBACK_REJECTED,
            detail=f"rejected by process_file_callback: {reason}",
        )
