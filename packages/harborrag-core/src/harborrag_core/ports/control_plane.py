"""Repository ports for control-plane aggregates (plan §6 tables).

One Protocol per aggregate, async because every real implementation is
I/O-bound (SQLAlchemy async, ST5). App/runtime layers depend on these,
never on the adapter classes.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime
from typing import Protocol, TypeVar

from harborrag_core.contracts.events import HarborEvent
from harborrag_core.domain.activity import ActivityEntry
from harborrag_core.domain.graph_conflict import ConflictAction, ConflictStatus, GraphConflict
from harborrag_core.domain.job import Job, JobStatus
from harborrag_core.domain.mcp_usage import (
    McpClientUsage,
    McpConfigSnapshot,
    McpToolUsage,
    McpUsageEntry,
)
from harborrag_core.domain.member import Member
from harborrag_core.domain.pending_effect import PendingControlPlaneEffect
from harborrag_core.domain.project import Project
from harborrag_core.domain.provider import Provider
from harborrag_core.domain.routing_rule import RoutingRule
from harborrag_core.domain.settings import WorkspaceSettings
from harborrag_core.domain.source_config import SourceConfig
from harborrag_core.security.context import AccessContext


class ProjectRepositoryPort(Protocol):
    """CRUD for projects (plan §5.1).

    ``tenant_ids`` on every read/delete is the caller's allowed tenant scope:
    ``None`` means unrestricted and is reserved for trusted system/internal
    callers, never a convenience default -- every application-facing caller
    (e.g. an API route) must pass its principal's actual scope explicitly.
    """

    async def list(self, *, tenant_ids: frozenset[str] | None) -> list[Project]:
        """Projects visible to ``tenant_ids``, unpaginated."""

    async def get(self, project_id: str, *, tenant_ids: frozenset[str] | None) -> Project | None:
        """One project by id within ``tenant_ids``, or None."""

    async def create(self, project: Project) -> Project:
        """Persist a new project and return it."""

    async def update(self, project: Project) -> Project:
        """Persist changes to an existing project and return it."""

    async def delete(self, project_id: str, *, tenant_ids: frozenset[str] | None) -> None:
        """Remove a project within ``tenant_ids``; cascading tombstones are the engine's job."""


class SourceRepositoryPort(Protocol):
    """CRUD for configured sources (plan §5.2). See ``ProjectRepositoryPort`` for ``tenant_ids``."""

    async def list(
        self,
        project_id: str | None = None,
        *,
        tenant_ids: frozenset[str] | None,
    ) -> list[SourceConfig]:
        """Sources visible to ``tenant_ids``, optionally filtered to one project."""

    async def get(
        self, source_id: str, *, tenant_ids: frozenset[str] | None
    ) -> SourceConfig | None:
        """One source by id within ``tenant_ids``, or None."""

    async def create(self, source: SourceConfig) -> SourceConfig:
        """Persist a new source and return it."""

    async def update(self, source: SourceConfig) -> SourceConfig:
        """Persist changes to an existing source and return it."""

    async def delete(self, source_id: str, *, tenant_ids: frozenset[str] | None) -> None:
        """Remove a source configuration within ``tenant_ids``."""


class JobRepositoryPort(Protocol):
    """Read/persist jobs and their event streams (plan §5.3, §6 job_events).

    See ``ProjectRepositoryPort`` for ``tenant_ids``.
    """

    async def list(
        self,
        status: JobStatus | None = None,
        source_id: str | None = None,
        *,
        tenant_ids: frozenset[str] | None,
    ) -> list[Job]:
        """Jobs visible to ``tenant_ids``, filtered by status and/or source."""

    async def get(self, job_id: str, *, tenant_ids: frozenset[str] | None) -> Job | None:
        """One job by id within ``tenant_ids``, or None."""

    async def save(self, job: Job) -> Job:
        """Insert or update a job row."""

    async def append_event(self, job_id: str, event: HarborEvent) -> None:
        """Append to the job's ordered event log (WS reconnect replay source)."""

    async def count_by_status(self, *, tenant_ids: frozenset[str] | None) -> dict[str, int]:
        """Job counts within ``tenant_ids`` grouped by status (dashboard metrics)."""


class ActivityRepositoryPort(Protocol):
    """Append-only audit feed (plan §5.5). See ``ProjectRepositoryPort`` for ``tenant_ids``."""

    async def append(self, entry: ActivityEntry) -> None:
        """Write one audit row; entries are never mutated or deleted."""

    async def list(
        self, limit: int = 50, *, tenant_ids: frozenset[str] | None
    ) -> list[ActivityEntry]:
        """Most recent entries within ``tenant_ids``, newest first."""


class SettingsRepositoryPort(Protocol):
    """Single-document workspace settings (plan §5.5).

    NOT tenant-scoped, despite ``SettingsRepositoryProvider`` below being a
    ``TenantScopedRepositoryProvider`` and ``WorkspaceSettings`` carrying a
    ``tenant_id``. Neither method takes a scope and the SQL adapter addresses
    one fixed row, so in a multi-tenant deployment every tenant reads and
    overwrites the same document.

    Closing this needs a unique constraint on ``workspace_settings.tenant_id``
    and a migration to split the existing row, so it is a schema decision
    rather than a code change. Until then, treat the document as workspace-wide
    and do not store anything tenant-specific in it.
    """

    async def get(self) -> WorkspaceSettings:
        """The settings document (empty document if never written)."""

    async def put(self, settings: WorkspaceSettings) -> WorkspaceSettings:
        """Replace the settings document and return it."""


class ProviderRepositoryPort(Protocol):
    """Model provider registry (plan §5.5). See ``ProjectRepositoryPort`` for ``tenant_ids``.

    Delete is a soft delete (see ``Provider.deleted_at``): a routing rule may
    reference a provider's id via a DB foreign key, so the row must keep
    existing. ``list``/``get`` hide deleted providers as if they were gone.
    """

    async def list(self, *, tenant_ids: frozenset[str] | None) -> list[Provider]:
        """Non-deleted providers visible to ``tenant_ids``."""

    async def get(self, provider_id: str, *, tenant_ids: frozenset[str] | None) -> Provider | None:
        """One non-deleted provider by id within ``tenant_ids``, or None."""

    async def save(self, provider: Provider) -> Provider:
        """Insert or update (upsert) a provider."""

    async def delete(self, provider_id: str, *, tenant_ids: frozenset[str] | None) -> None:
        """Tombstone a provider within ``tenant_ids`` (sets ``deleted_at``); row stays."""


class RoutingRuleRepositoryPort(Protocol):
    """Workspace-wide routing table (plan §5.5); not tenant-scoped, like ``SettingsRepositoryPort``.

    v1 has no "update just one rule": every write replaces the whole table
    atomically, so callers never observe a partially-applied routing change.
    """

    async def replace(self, rules: Sequence[RoutingRule]) -> list[RoutingRule]:
        """Atomically replace the entire routing table with ``rules``."""

    async def list(self) -> list[RoutingRule]:
        """Every routing rule, ordered by family then insertion."""


class MemberRepositoryPort(Protocol):
    """Workspace members and their RBAC roles (plan §8.1). See ``ProjectRepositoryPort`` for
    ``tenant_ids`` (``list``/``delete`` only -- ``get_by_subject`` is the identity-bootstrap
    lookup that establishes which tenants a subject belongs to in the first place, so it
    cannot itself be scoped by tenant)."""

    async def list(self, *, tenant_ids: frozenset[str] | None) -> list[Member]:
        """Members visible to ``tenant_ids``."""

    async def get_by_subject(self, subject: str) -> Member | None:
        """Look up a member by auth subject (JWT sub), or None."""

    async def save(self, member: Member) -> Member:
        """Insert or update a membership row."""

    async def delete(self, member_id: str, *, tenant_ids: frozenset[str] | None) -> None:
        """Remove a member within ``tenant_ids``."""


class PendingEffectRepositoryPort(Protocol):
    """Durable retry queue for control-plane side effects (ML2 recoverability hardening).

    Secret retirement and audit logging enqueue failed post-commit effects.
    Erasure enqueues intent before deleting canonical identifiers, retaining
    the keys needed to recover interrupted work across stores. The recovery drain
    retries each pending row and calls ``complete`` once the retry succeeds;
    a row that keeps failing simply stays pending for the next drain pass.
    """

    async def enqueue(self, effect: PendingControlPlaneEffect) -> None:
        """Durably record replayable work before its recovery identifiers are lost."""

    async def list_pending(self, *, limit: int = 100) -> list[PendingControlPlaneEffect]:
        """Oldest-first pending effects, for the recovery drain."""

    async def complete(self, effect_id: str) -> None:
        """Remove an effect once its retry has succeeded; a no-op if already gone."""


class LeaseRepositoryPort(Protocol):
    """Named, time-boxed singleton leases (ML2 multi-process hardening).

    Backs leader election for background loops that must run on at most
    one process at a time even when the API is scaled to multiple
    processes/replicas (e.g. the ingestion progress bridge) -- everything
    the ``InProcessEventBus`` docstrings assume but nothing enforces.
    """

    async def try_acquire(self, name: str, holder: str, *, ttl_seconds: float) -> bool:
        """Acquire or renew ``name`` for ``holder``.

        True if ``holder`` now owns the lease (either it already did, or the
        previous holder's lease had lapsed); False if another holder's lease
        is still live. A holder that stops calling this simply lets its
        lease expire after ``ttl_seconds``, letting another holder take over.
        """


class GraphConflictRepositoryPort(Protocol):
    """Durable queue of graph disagreements awaiting human resolution (plan §5.5).

    v1 is record-only: ``resolve`` persists the chosen action and closes the
    conflict; it does not itself mutate FalkorDB (see ``GraphConflict``'s
    docstring for why). ``report`` exists for future conflict-detection code
    or manual seeding -- nothing calls it yet.
    """

    async def report(self, conflict: GraphConflict) -> GraphConflict:
        """Durably record a newly detected conflict."""

    async def list(
        self,
        *,
        tenant_ids: frozenset[str] | None,
        status: ConflictStatus | None = None,
        cursor: str | None,
        limit: int,
    ) -> tuple[list[GraphConflict], str | None]:
        """Conflicts within ``tenant_ids``, newest-detected first.

        ``status`` narrows to only-open or only-resolved; omitted, both are
        returned. Returns ``(items, next_cursor)``; ``next_cursor`` is
        ``None`` once the caller has walked the whole set.
        """

    async def get(
        self, conflict_id: str, *, tenant_ids: frozenset[str] | None
    ) -> GraphConflict | None:
        """One conflict by id within ``tenant_ids``, or None."""

    async def resolve(
        self,
        conflict_id: str,
        *,
        action: ConflictAction,
        resolved_by: str,
        tenant_ids: frozenset[str] | None,
    ) -> GraphConflict:
        """Close a conflict with the chosen action.

        Raises ``HarborNotFoundError`` if missing within ``tenant_ids``,
        ``HarborConflictError`` if already resolved.
        """


class McpQueryLogRepositoryPort(Protocol):
    """Read-side MCP telemetry written by harborrag-mcp-server (plan §5.5/§6, ML4-P3).

    Not tenant-scoped: MCP tool calls are not reliably tenant-attributed at
    the transport boundary, and this is an operator-facing usage view, not a
    per-workspace resource. ``record`` is the only write path -- rows are
    never mutated or deleted.
    """

    async def record(self, entry: McpUsageEntry) -> None:
        """Durably record one completed MCP tool invocation."""

    async def usage_by_client(self) -> list[McpClientUsage]:
        """Every distinct client, with its total query count and last-seen time."""

    async def usage_by_tool(self) -> list[McpToolUsage]:
        """Every distinct tool, with its call count and average latency."""

    async def list_since(self, *, since: datetime, limit: int) -> list[McpUsageEntry]:
        """Entries at or after ``since``, newest first, capped at ``limit``."""

    async def ping(self) -> bool:
        """Best-effort reachability check for ``/mcp/status``: True if the store answers."""


class McpConfigSnapshotPort(Protocol):
    """Single-document read model for the MCP server's live effective configuration.

    Published by harborrag-mcp-server (the only writer) through the same
    control-plane DB bridge as ``McpQueryLogRepositoryPort``, since the
    layering rules forbid harborrag-app from importing harborrag-mcp-server
    directly to read it out of the running process.
    """

    async def get(self) -> McpConfigSnapshot | None:
        """The most recently published snapshot, or None if never published."""

    async def put(self, snapshot: McpConfigSnapshot) -> McpConfigSnapshot:
        """Replace the published snapshot and return it."""


TRepository_co = TypeVar("TRepository_co", covariant=True)


class TenantScopedRepositoryProvider(Protocol[TRepository_co]):
    """Bind a repository surface to one authenticated tenant context.

    Existing repositories remain source-compatible. Multi-tenant composition
    should obtain them through this capability; the returned view must expose
    only records matching ``access.tenant_id`` and stamp that tenant on writes.
    """

    def for_access(self, access: AccessContext) -> TRepository_co: ...


ProjectRepositoryProvider = TenantScopedRepositoryProvider[ProjectRepositoryPort]
SourceRepositoryProvider = TenantScopedRepositoryProvider[SourceRepositoryPort]
JobRepositoryProvider = TenantScopedRepositoryProvider[JobRepositoryPort]
ActivityRepositoryProvider = TenantScopedRepositoryProvider[ActivityRepositoryPort]
SettingsRepositoryProvider = TenantScopedRepositoryProvider[SettingsRepositoryPort]
ProviderRepositoryProvider = TenantScopedRepositoryProvider[ProviderRepositoryPort]
MemberRepositoryProvider = TenantScopedRepositoryProvider[MemberRepositoryPort]
GraphConflictRepositoryProvider = TenantScopedRepositoryProvider[GraphConflictRepositoryPort]
