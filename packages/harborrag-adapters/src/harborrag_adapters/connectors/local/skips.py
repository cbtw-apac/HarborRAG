"""Structured skip reporting for local filesystem discovery.

A file name can name a person, so warnings identify a skipped file by a digest
of its path and never by its path, name or skip detail.
"""

from __future__ import annotations

import hashlib
import logging
from collections.abc import Iterator
from pathlib import Path

from harborrag_adapters.connectors.schemas import (
    SKIP_REASON_CALLBACK_REJECTED,
    SKIP_REASON_OUT_OF_SCOPE,
    SKIP_REASON_SIZE_LIMIT_EXCEEDED,
    SKIP_REASON_UNREADABLE,
    SKIP_REASON_UNSUPPORTED_EXTENSION,
    ConnectorSkip,
)

logger = logging.getLogger("harborrag.adapters.connectors.local")

_PATH_DIGEST_CHARS = 12


def path_digest(path: str | Path) -> str:
    """Return a short digest that names a local path in logs without revealing it.

    It is the first 12 hex characters of the SHA-256 of the path as recorded on
    the skip, so an operator can match a suspected file with ``sha256sum``.
    """
    return hashlib.sha256(str(path).encode("utf-8")).hexdigest()[:_PATH_DIGEST_CHARS]


class LocalSkipReport:
    """Accumulates the paths traversal dropped for a reportable reason.

    Args:
        run_id: Run ID that tags every skip warning. The connector may rebind it.
    """

    def __init__(self, run_id: str | None = None) -> None:
        self.run_id = run_id
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
        """Add one skip to the report and log its reason, path digest and run ID.

        The path and detail stay on the ``ConnectorSkip`` only. A path already
        reported for the same reason is ignored, so a re-walk neither duplicates
        the entry nor repeats the warning.
        """
        key = (str(path), reason)
        if key in self._skips:
            return
        self._skips[key] = ConnectorSkip(path=str(path), reason=reason, detail=detail)
        logger.warning(
            "local.file_skipped reason=%s path_digest=%s run_id=%s",
            reason,
            path_digest(path),
            self.run_id,
        )

    def oversized(self, path: str | Path, *, size: int, limit: int) -> None:
        """Record a file rejected by ``max_file_size_bytes``."""
        self.record(
            path,
            reason=SKIP_REASON_SIZE_LIMIT_EXCEEDED,
            detail=f"exceeds size limit: {size} bytes > max_file_size_bytes {limit}",
        )

    def unsupported_extension(self, path: str | Path, *, extension: str, reason: str) -> None:
        """Record a file dropped by ``allowed_extensions`` or ``excluded_extensions``."""
        shown = extension or "<none>"
        self.record(
            path,
            reason=SKIP_REASON_UNSUPPORTED_EXTENSION,
            detail=f"extension {shown!r} {reason}",
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

    def callback_failed(self, path: str | Path, *, error: Exception) -> None:
        """Record a file whose ``process_file_callback`` raised.

        The warning names the error class only, because its message can carry
        the path. With ``fail_on_error`` set, the error and its traceback
        propagate instead.
        """
        logger.warning(
            "local.file_callback_failed error_type=%s path_digest=%s run_id=%s",
            type(error).__name__,
            path_digest(path),
            self.run_id,
        )
        self.callback_rejected(path, reason=f"raised {type(error).__name__}: {error}")
