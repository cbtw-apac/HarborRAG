from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import date, datetime
from enum import Enum
from typing import Any, ClassVar, cast

from harborrag_core.domain.source import SourceRecord


def _json_safe(value: Any) -> Any:
    """Recursively convert connector metadata into JSON-safe values."""
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
    """Common metadata emitted for every loaded connector document.

    Provider schemas inherit this contract and add their source-specific
    fields. ``source_system`` and ``metadata_schema_version`` are class-level
    constants so callers cannot accidentally construct metadata for the wrong
    provider. They are injected into the serialized payload by ``to_dict``.
    """

    source_system: ClassVar[str] = "base"
    metadata_schema_version: ClassVar[int] = 1

    record_id: str
    title: str
    checksum: str | None = None
    created_at: datetime | None = None
    updated_at: datetime | None = None

    def to_dict(self) -> dict[str, Any]:
        """Serialize common and provider metadata for ``RawDocument.metadata``."""
        payload = {
            "source_system": self.source_system,
            "metadata_schema_version": self.metadata_schema_version,
            **asdict(self),
        }
        return cast("dict[str, Any]", _json_safe(payload))


@dataclass(frozen=True)
class ConnectorCapabilities:
    """Feature flags advertised by a connector provider."""

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


@dataclass
class ConnectorQuery:
    """Shared query envelope for discovery calls.

    Provider-specific selectors belong in ``filters`` so the public connector
    contract can stay stable while sources expose different concepts such as
    page IDs, issue keys, repository paths, labels, or drive item IDs.
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
    """One bounded discovery page plus an opaque provider continuation cursor."""

    records: tuple[SourceRecord, ...]
    next_cursor: str | None
