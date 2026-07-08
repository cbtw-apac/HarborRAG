from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any


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


@dataclass
class ConnectorSyncState:
    """Small sync checkpoint carried by runtime or persistence layers."""

    connector_id: str
    source_system: str
    cursor: str | None = None
    last_success_at: datetime | None = None
    last_seen_updated_at: datetime | None = None
    high_watermark: str | None = None
    checksum_map: dict[str, str] = field(default_factory=dict)

    def update_checksum(self, source_id: str, checksum: str) -> None:
        """Record the latest checksum observed for a source item."""
        self.checksum_map[source_id] = checksum
