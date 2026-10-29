"""Repository ports for control-plane aggregates (plan §6 tables).

One Protocol per aggregate, async because every real implementation is
I/O-bound (SQLAlchemy async, ST5). App/runtime layers depend on these,
never on the adapter classes.
"""

from __future__ import annotations

from typing import Any, Protocol, TypeVar

from harborrag_core.contracts.events import HarborEvent
from harborrag_core.domain.activity import ActivityEntry
from harborrag_core.domain.connection import Connection, ConnectionPutOutcome, ConnectionStatus
from harborrag_core.domain.connection_snapshot import ConnectionSnapshot
from harborrag_core.domain.graph_conflict import ConflictAction, ConflictStatus, GraphConflict
from harborrag_core.domain.job import Job, JobStatus
from harborrag_core.domain.member import Member
from harborrag_core.domain.pending_effect import PendingControlPlaneEffect
from harborrag_core.domain.project import Project
from harborrag_core.domain.provider import Provider
from harborrag_core.domain.settings import WorkspaceSettings
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

    async def update(self, project: Project, *, expected_version: int | None = None) -> Project:
        """Persist changes to an existing project, advance its version and return it.

        Raises:
            HarborPreconditionFailedError: ``expected_version`` is not the stored version.
        """

    async def delete(self, project_id: str, *, tenant_ids: frozenset[str] | None) -> None:
        """Remove a project within ``tenant_ids``; cascading tombstones are the engine's job."""


class ConnectionRepositoryPort(Protocol):
    """Persistence for editable connections (``sources`` table).

    A connection is addressed by ``(tenant_id, project_id, connection_id)``;
    ingestion addresses it by ``(tenant_id, source_key)``. ``put`` is the only
    write that changes configuration and advances ``version``. See
    ``ProjectRepositoryPort`` for ``tenant_ids``.
    """

    async def get(
        self, *, tenant_id: str, project_id: str, connection_id: str
    ) -> Connection | None:
        """One connection by its public identity, or None."""

    async def get_by_source_key(self, *, tenant_id: str, source_key: str) -> Connection | None:
        """One connection by the key ingestion stores on documents and scopes, or None."""

    async def get_by_id(
        self, connection_pk: str, *, tenant_ids: frozenset[str] | None
    ) -> Connection | None:
        """One connection by its internal id within ``tenant_ids``, or None."""

    async def list(  # noqa: PLR0913 - the spec's list filters plus paging
        self,
        *,
        tenant_ids: frozenset[str] | None,
        project_id: str | None = None,
        source_type: str | None = None,
        status: ConnectionStatus | None = None,
        limit: int | None = None,
        cursor: str | None = None,
    ) -> tuple[list[Connection], str | None]:
        """Connections needing attention first, then by project and ``connection_id``.

        Args:
            tenant_ids: Allowed tenant scope; ``None`` is unrestricted.
            project_id: Keep one project only.
            source_type: Keep one source type only.
            status: Keep one status only.
            limit: Page size; ``None`` returns every match on one page.
            cursor: ``next_cursor`` from the previous page.

        Returns:
            The page and the cursor of the next page, or ``None`` on the last one.

        Raises:
            HarborValidationError: ``cursor`` is malformed.
        """

    async def put(
        self,
        connection: Connection,
        *,
        expected_version: int | None = None,
        create_only: bool = False,
    ) -> tuple[Connection, ConnectionPutOutcome]:
        """Create or overwrite a connection, advancing ``version`` on overwrite.

        Args:
            connection: The connection to store. On overwrite its internal id,
                ``source_key`` and creation stamps are taken from the stored row.
            expected_version: Fail unless the stored version equals this value.
            create_only: Fail when the connection already exists.

        Returns:
            The stored connection and whether it was created or overwritten.

        Raises:
            HarborPreconditionFailedError: ``expected_version`` or ``create_only``
                does not hold, including a concurrent writer winning the race.
            HarborConflictError: ``source_key`` is already used in the tenant.
        """

    async def set_status(
        self,
        *,
        tenant_id: str,
        source_key: str,
        status: ConnectionStatus,
        detail: dict[str, Any] | None = None,
        only_from: frozenset[ConnectionStatus] | None = None,
    ) -> bool:
        """Record a status change without touching ``version``.

        Args:
            tenant_id: Owning tenant.
            source_key: Connection to update.
            status: New status.
            detail: Machine-readable explanation, e.g. field errors.
            only_from: Change the status only when the current one is in this set.

        Returns:
            True when a row changed.
        """

    async def delete(self, *, tenant_id: str, project_id: str, connection_id: str) -> None:
        """Remove the row; a missing row is a no-op."""


SourceRepositoryPort = ConnectionRepositoryPort


class ConnectionRevisionRepositoryPort(Protocol):
    """Immutable connection snapshots, one per content fingerprint.

    Submission saves the snapshot a run starts with; workers build connectors
    from it, so a later edit never changes a run that is already going.
    """

    async def save(self, snapshot: ConnectionSnapshot) -> None:
        """Store ``snapshot`` under its fingerprint; saving it again is a no-op."""

    async def get(self, *, tenant_id: str, fingerprint: str) -> ConnectionSnapshot | None:
        """The snapshot with this fingerprint in the tenant, or None."""

    async def list_for_source(self, *, tenant_id: str, source_key: str) -> list[ConnectionSnapshot]:
        """Every stored snapshot of one connection, oldest first."""


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
    """Model provider registry (plan §5.5). See ``ProjectRepositoryPort`` for ``tenant_ids``."""

    async def list(self, *, tenant_ids: frozenset[str] | None) -> list[Provider]:
        """Registered providers visible to ``tenant_ids``."""

    async def get(self, provider_id: str, *, tenant_ids: frozenset[str] | None) -> Provider | None:
        """One provider by id within ``tenant_ids``, or None."""

    async def save(self, provider: Provider) -> Provider:
        """Insert or update (upsert) a provider."""

    async def delete(self, provider_id: str, *, tenant_ids: frozenset[str] | None) -> None:
        """Remove a provider registration within ``tenant_ids``."""


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


TRepository_co = TypeVar("TRepository_co", covariant=True)


class TenantScopedRepositoryProvider(Protocol[TRepository_co]):
    """Bind a repository surface to one authenticated tenant context.

    Existing repositories remain source-compatible. Multi-tenant composition
    should obtain them through this capability; the returned view must expose
    only records matching ``access.tenant_id`` and stamp that tenant on writes.
    """

    def for_access(self, access: AccessContext) -> TRepository_co: ...


ProjectRepositoryProvider = TenantScopedRepositoryProvider[ProjectRepositoryPort]
ConnectionRepositoryProvider = TenantScopedRepositoryProvider[ConnectionRepositoryPort]
SourceRepositoryProvider = ConnectionRepositoryProvider
JobRepositoryProvider = TenantScopedRepositoryProvider[JobRepositoryPort]
ActivityRepositoryProvider = TenantScopedRepositoryProvider[ActivityRepositoryPort]
SettingsRepositoryProvider = TenantScopedRepositoryProvider[SettingsRepositoryPort]
ProviderRepositoryProvider = TenantScopedRepositoryProvider[ProviderRepositoryPort]
MemberRepositoryProvider = TenantScopedRepositoryProvider[MemberRepositoryPort]
GraphConflictRepositoryProvider = TenantScopedRepositoryProvider[GraphConflictRepositoryPort]
