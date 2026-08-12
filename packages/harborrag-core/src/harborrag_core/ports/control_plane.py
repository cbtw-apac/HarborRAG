"""Repository ports for control-plane aggregates (plan §6 tables).

One Protocol per aggregate, async because every real implementation is
I/O-bound (SQLAlchemy async, ST5). App/runtime layers depend on these,
never on the adapter classes.
"""

from __future__ import annotations

from typing import Protocol, TypeVar

from harborrag_core.contracts.events import HarborEvent
from harborrag_core.domain.activity import ActivityEntry
from harborrag_core.domain.job import Job, JobStatus
from harborrag_core.domain.member import Member
from harborrag_core.domain.pending_effect import PendingControlPlaneEffect
from harborrag_core.domain.project import Project
from harborrag_core.domain.provider import Provider
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
    """Single-document workspace settings (plan §5.5)."""

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

    A row is enqueued only when a secondary effect -- secret retirement or
    audit logging -- fails after the primary write it depends on has already
    committed. It is never a step on the happy path. The recovery drain
    retries each pending row and calls ``complete`` once the retry succeeds;
    a row that keeps failing simply stays pending for the next drain pass.
    """

    async def enqueue(self, effect: PendingControlPlaneEffect) -> None:
        """Durably record a failed side effect for later retry."""

    async def list_pending(self, *, limit: int = 100) -> list[PendingControlPlaneEffect]:
        """Oldest-first pending effects, for the recovery drain."""

    async def complete(self, effect_id: str) -> None:
        """Remove an effect once its retry has succeeded; a no-op if already gone."""


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
