"""Port for reading how fresh every ingestion source is, across tenants.

The read crosses tenants. Hand it only to an operator surface, such as the API's own
metrics, never to a reader tool.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Protocol

from harborrag_core.ingestion.states import IngestionTaskState

UNKNOWN_CONNECTOR_TYPE = "unknown"
"""Connector type reported for a source whose source scope record is gone."""


@dataclass(frozen=True, slots=True)
class SourceFreshness:
    """When one source last synced and how its latest run stands.

    Attributes:
        tenant_id: Tenant that owns the source.
        source_scope_id: The source's scope.
        connector_type: Connector type of the source, such as ``jira``.
        last_success_at: When its latest completed or partial run ended; ``None`` when
            no run ever succeeded.
        last_run_status: State of its most recently submitted run.
    """

    tenant_id: str
    source_scope_id: str
    connector_type: str
    last_success_at: datetime | None
    last_run_status: IngestionTaskState


@dataclass(frozen=True, slots=True)
class SourceFreshnessReport:
    """The freshness of the sources one read returned, out of every known source.

    Attributes:
        sources: The sources read, at most the requested limit.
        source_count: How many sources have at least one run, including any the
            limit left out.
    """

    sources: tuple[SourceFreshness, ...]
    source_count: int

    @property
    def truncated(self) -> bool:
        """Whether the limit left sources out."""
        return self.source_count > len(self.sources)


class SourceFreshnessReader(Protocol):
    """Reads the freshness of every source that has at least one ingestion run."""

    async def source_freshness(self, *, limit: int) -> SourceFreshnessReport:
        """Read each source's last success and latest run state, stalest first.

        Sources that never succeeded come first, then the longest since a success.

        Args:
            limit: The most sources to return.

        Returns:
            The sources read and how many exist in all.

        Raises:
            HarborUnavailableError: The store cannot be reached, read or answer in time.
        """
        ...
