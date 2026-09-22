"""Durable topology authority used by dispatch, enrichment and retrieval."""

from datetime import datetime
from typing import Protocol

from harborrag_core.ingestion import ArtifactReference
from harborrag_core.security.context import AccessContext
from harborrag_core.topology import (
    CanonicalAssertion,
    CanonicalMention,
    ChunkExtractionCheckpoint,
    DocumentTopologyBuild,
    ResolutionDecision,
    ResolutionRequest,
    TopologyJob,
    TopologyPolicy,
)
from harborrag_core.topology.budget import BudgetAdmission, BudgetRequest, UsageSettlement
from harborrag_core.topology.config import TenantIndexingConfig, TenantIndexingState
from harborrag_core.topology.permissions import (
    BuildInputLineage,
    DerivedArtifactLineage,
    DerivedArtifactRecord,
    PermissionCoverageReport,
    ResolvedPermissionSnapshot,
)


class TopologyDerivationRepositoryPort(Protocol):
    """Store and discover independently retryable derived products."""

    async def pending_derivation_build_ids(
        self,
        tenant_id: str,
        embedding_profile: str,
        *,
        parent_profile: str | None = None,
        limit: int = 100,
        after_build_id: str | None = None,
    ) -> tuple[str, ...]: ...
    async def get_build_lineage(
        self, tenant_id: str, build_id: str
    ) -> BuildInputLineage | None: ...
    async def publish_derivation(
        self, tenant_id: str, lineage: DerivedArtifactLineage, artifact: ArtifactReference
    ) -> None: ...

    async def active_derivations(
        self,
        tenant_id: str,
        *,
        access: AccessContext | None = None,
        artifact_kind: str | None = None,
        limit: int = 100,
    ) -> tuple[DerivedArtifactRecord, ...]: ...

    async def derivations_for_build(
        self,
        tenant_id: str,
        build_id: str,
        *,
        limit: int = 100,
    ) -> tuple[DerivedArtifactRecord, ...]: ...

    async def eligible_artifact_ids(
        self, tenant_id: str, artifact_ids: tuple[str, ...], *, access: AccessContext | None = None
    ) -> set[str]: ...


class TopologyIndexingRepositoryPort(Protocol):
    """Configure the tenant-level indexing gate."""

    async def configure_indexing(self, config: TenantIndexingConfig) -> TenantIndexingState: ...

    async def get_indexing(self, tenant_id: str) -> TenantIndexingState: ...


class TopologyPermissionRepositoryPort(Protocol):
    """Persist and query resolved authorization snapshots."""

    async def set_permissions(self, snapshot: ResolvedPermissionSnapshot) -> None: ...

    async def permission_coverage(self, tenant_id: str) -> PermissionCoverageReport: ...

    async def allowed_document_ids(
        self, tenant_id: str, *, access: AccessContext | None, limit: int = 10000
    ) -> tuple[str, ...]: ...

    async def allowed_source_scope_ids(
        self, tenant_id: str, *, access: AccessContext | None, limit: int = 10000
    ) -> tuple[str, ...]: ...

    async def authorized_document_ids(
        self, tenant_id: str, document_ids: tuple[str, ...], *, access: AccessContext | None
    ) -> set[str]: ...

    async def authorized_source_scope_ids(
        self,
        tenant_id: str,
        source_scope_ids: tuple[str, ...],
        *,
        access: AccessContext | None,
    ) -> set[str]: ...


class TopologyBudgetRepositoryPort(Protocol):
    """Reserve and settle durable model-spending allowances."""

    async def reserve_budget(self, job: TopologyJob, request: BudgetRequest) -> BudgetAdmission: ...

    async def reserve_for_build(
        self, tenant_id: str, build_id: str, request: BudgetRequest
    ) -> BudgetAdmission: ...

    async def settle_budget(
        self, tenant_id: str, reservation_id: str, usage: UsageSettlement
    ) -> None: ...


class TopologyAuditRepositoryPort(Protocol):
    """Enumerate generations for bounded audit and cleanup passes."""

    async def audit_build_ids(
        self,
        tenant_id: str,
        *,
        limit: int = 100,
        after_build_id: str | None = None,
    ) -> tuple[str, ...]: ...

    async def retired_build_ids(
        self,
        tenant_id: str,
        *,
        build_ids: tuple[str, ...] = (),
        limit: int = 100,
        after_build_id: str | None = None,
    ) -> tuple[str, ...]: ...


class TopologyResolutionRepositoryPort(Protocol):
    """Record manual resolution policy and resolve build-local entities."""

    async def record_resolution(self, request: ResolutionRequest) -> ResolutionDecision: ...

    async def list_resolutions(
        self,
        tenant_id: str,
        *,
        limit: int = 100,
    ) -> tuple[ResolutionDecision, ...]: ...

    async def resolve_entities(
        self, job: TopologyJob, entity_ids: tuple[str, ...]
    ) -> dict[str, str]: ...


class TopologyPolicyRepositoryPort(Protocol):
    """Configure source policies and reconcile active publications."""

    async def configure_policy(self, policy: TopologyPolicy) -> int: ...

    async def get_policy(self, tenant_id: str, source_scope_id: str) -> TopologyPolicy | None: ...

    async def reconcile(self, tenant_id: str, *, limit: int = 1000) -> int: ...


class TopologyJobRepositoryPort(Protocol):
    """Own enrichment job leases and durable chunk checkpoints."""

    async def claim(
        self,
        tenant_id: str,
        *,
        lease_seconds: int = 300,
        job_id: str | None = None,
    ) -> TopologyJob | None: ...

    async def defer_job(
        self, job: TopologyJob, reason: str, retry_after: datetime | None = None
    ) -> None: ...

    async def renew(self, job: TopologyJob, *, lease_seconds: int = 300) -> None: ...

    async def prepare(self, job: TopologyJob, chunk_ids: tuple[str, ...]) -> None: ...

    async def checkpoints(self, job: TopologyJob) -> tuple[ChunkExtractionCheckpoint, ...]: ...

    async def reusable_checkpoint(
        self,
        tenant_id: str,
        extraction_fingerprint: str,
        input_digest: str,
    ) -> ChunkExtractionCheckpoint | None: ...

    async def checkpoint(
        self,
        job: TopologyJob,
        value: ChunkExtractionCheckpoint,
    ) -> ChunkExtractionCheckpoint: ...

    async def fail(self, job: TopologyJob, error_code: str) -> None: ...

    async def get_job(self, tenant_id: str, job_id: str) -> TopologyJob | None: ...

    async def list_jobs(self, tenant_id: str, *, limit: int = 100) -> tuple[TopologyJob, ...]: ...

    async def runnable_jobs(
        self, tenant_id: str, *, limit: int = 100
    ) -> tuple[TopologyJob, ...]: ...


class TopologyBuildRepositoryPort(Protocol):
    """Stage, verify, publish, and read canonical topology builds."""

    async def stage(self, job: TopologyJob, build: DocumentTopologyBuild) -> None: ...

    async def mark_verified(self, job: TopologyJob, build_id: str) -> None: ...

    async def accept(self, job: TopologyJob, build_id: str) -> bool: ...

    async def get_build(self, tenant_id: str, build_id: str) -> DocumentTopologyBuild | None: ...

    async def eligible_build_ids(
        self, tenant_id: str, build_ids: tuple[str, ...], *, access: AccessContext | None = None
    ) -> set[str]: ...


class TopologyObservationRepositoryPort(Protocol):
    """Read accepted semantic observations under an explicit access context."""

    async def active_mentions(  # noqa: PLR0913 - explicit permission context on shared search contract
        self,
        tenant_id: str,
        *,
        labels: tuple[str, ...] = (),
        chunk_ids: tuple[str, ...] = (),
        entity_ids: tuple[str, ...] = (),
        limit: int = 100,
        access: AccessContext | None = None,
    ) -> tuple[CanonicalMention, ...]: ...

    async def active_assertions(
        self,
        tenant_id: str,
        *,
        entity_ids: tuple[str, ...] = (),
        limit: int = 100,
        access: AccessContext | None = None,
    ) -> tuple[CanonicalAssertion, ...]: ...


class TopologyEnrichmentRepositoryPort(
    TopologyJobRepositoryPort,
    TopologyBuildRepositoryPort,
    TopologyResolutionRepositoryPort,
    TopologyBudgetRepositoryPort,
    Protocol,
):
    """The focused authority required by one enrichment attempt."""


class TopologyConfigurationRepositoryPort(
    TopologyIndexingRepositoryPort,
    TopologyPolicyRepositoryPort,
    Protocol,
):
    """The focused authority required to synchronize operator configuration."""


class TopologyDispatchRepositoryPort(
    TopologyPolicyRepositoryPort,
    TopologyJobRepositoryPort,
    Protocol,
):
    """The focused authority required to discover dispatchable work."""


class TopologyRepositoryPort(
    TopologyDerivationRepositoryPort,
    TopologyIndexingRepositoryPort,
    TopologyPermissionRepositoryPort,
    TopologyBudgetRepositoryPort,
    TopologyAuditRepositoryPort,
    TopologyResolutionRepositoryPort,
    TopologyPolicyRepositoryPort,
    TopologyJobRepositoryPort,
    TopologyBuildRepositoryPort,
    TopologyObservationRepositoryPort,
    Protocol,
):
    """Compatibility aggregate for composition roots requiring the full authority."""
