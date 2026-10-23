"""Value types exchanged between connectors and the runtime.

Handles:
- Document metadata base class
- Capability flags
- Discovery query, page and skip records
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import date, datetime
from enum import Enum
from typing import Any, ClassVar, cast

from harborrag_core.domain.source import SourceRecord


def _json_safe(value: Any) -> Any:
    """Recursively convert dates, enums and collections into JSON-safe values."""
    if isinstance(value, (date, datetime)):
        return value.isoformat()
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, dict):
        return {key: _json_safe(item) for key, item in value.items()}
    if isinstance(value, (list, tuple, set, frozenset)):
        return [_json_safe(item) for item in value]
    return value


@dataclass(slots=True, kw_only=True)
class ConnectorMetadata:
    """Base metadata emitted for every loaded connector document.

    Provider schemas subclass it and add source-specific fields. ``source_system`` and
    ``metadata_schema_version`` are class constants added to the payload by ``to_dict``.
    """

    source_system: ClassVar[str] = "base"
    metadata_schema_version: ClassVar[int] = 1

    record_id: str
    title: str
    checksum: str | None = None
    created_at: datetime | None = None
    updated_at: datetime | None = None

    def to_dict(self) -> dict[str, Any]:
        """Return the JSON-safe metadata dict for ``RawDocument.metadata``."""
        payload = {
            "source_system": self.source_system,
            "metadata_schema_version": self.metadata_schema_version,
            **asdict(self),
        }
        return cast("dict[str, Any]", _json_safe(payload))


@dataclass(frozen=True)
class ConnectorCapabilities:
    """Feature flags a connector provider advertises."""

    sync: bool = True
    async_: bool = False
    incremental_sync: bool = False
    full_sync: bool = True
    delta_token: bool = False
    pagination: bool = False
    attachments: bool = False
    comments: bool = False
    permissions: bool = False
    changelog: bool = False
    labels: bool = False
    users: bool = False
    relationships: bool = True
    local_files: bool = False
    concurrent_describe: bool = False


@dataclass
class ConnectorQuery:
    """Query envelope for discovery calls.

    Provider-specific selectors, such as page IDs, issue keys or repository paths,
    go in ``filters``.
    """

    path: str | None = None
    pattern: str | None = None
    recursive: bool = True
    updated_after: datetime | None = None
    limit: int | None = None
    include_attachments: bool = True
    filters: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class ConnectorPage:
    """One discovery page plus the opaque cursor for the next, or ``None`` on the last."""

    records: tuple[SourceRecord, ...]
    next_cursor: str | None


SKIP_REASON_SIZE_LIMIT_EXCEEDED = "size_limit_exceeded"
SKIP_REASON_UNREADABLE = "unreadable"
SKIP_REASON_OUT_OF_SCOPE = "out_of_scope"
SKIP_REASON_CALLBACK_REJECTED = "callback_rejected"
SKIP_REASON_ACCESS_RESTRICTED = "access_restricted"
SKIP_REASON_ACCESS_UNRESOLVED = "access_unresolved"


@dataclass(frozen=True, slots=True)
class ConnectorSkip:
    """One source item a traversal dropped, with its reason.

    Configured filtering (extension, glob, pattern, ``updated_after``) is not a skip.

    Attributes:
        path: Item path or identifier.
        reason: One of the ``SKIP_REASON_*`` constants, for machine handling.
        detail: Human-readable explanation.
    """

    path: str
    reason: str
    detail: str

    def to_dict(self) -> dict[str, str]:
        """Return the skip as a plain dict for logs and reports."""
        return {"path": self.path, "reason": self.reason, "detail": self.detail}
