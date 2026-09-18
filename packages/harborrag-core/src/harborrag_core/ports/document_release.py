"""Provider-independent operations consumed by document release services."""

from __future__ import annotations

from collections.abc import Sequence
from typing import Protocol

from harborrag_core.ingestion import (
    ActiveDocumentVersion,
    ActiveSourceDocument,
    ArtifactReference,
    DocumentFailure,
    DocumentRetirementResult,
    DocumentVersionCandidate,
    DocumentVersionSnapshot,
    DocumentVersionState,
    ProjectionCleanupJob,
    ProjectionManifest,
    PublicationResult,
    ReindexJob,
    ReindexProgress,
)


class DocumentVersionPort(Protocol):
    async def create_candidate(
        self, candidate: DocumentVersionCandidate
    ) -> DocumentVersionState: ...

    async def transition(
        self,
        document_version_id: str,
        target: DocumentVersionState,
        *,
        artifact_column: str | None = None,
        artifact: ArtifactReference | None = None,
    ) -> None: ...

    async def save_projection_manifest(self, manifest: ProjectionManifest) -> None: ...

    async def mark_verified(self, document_version_id: str) -> None: ...

    async def active_versions(
        self, document_ids: Sequence[str]
    ) -> dict[str, ActiveDocumentVersion]: ...

    async def get_version(self, document_version_id: str) -> DocumentVersionSnapshot | None: ...

    async def prepare_replay(self, document_version_id: str) -> DocumentVersionState: ...

    async def resume_failed(self, document_version_id: str) -> DocumentVersionState: ...

    async def active_snapshot(self, document_id: str) -> DocumentVersionSnapshot | None: ...

    async def resolve_active_sources(
        self,
        *,
        tenant_id: str,
        connector_type: str,
        connection_id: str,
        source_item_ids: Sequence[str],
    ) -> dict[str, ActiveSourceDocument]: ...

    async def resolve_unambiguous_active_sources(
        self, *, tenant_id: str, connector_type: str, source_item_ids: Sequence[str]
    ) -> dict[str, ActiveSourceDocument]: ...

    async def active_relation_document_ids(
        self,
        *,
        processing_fingerprint: str,
        anchor_document_id: str | None = None,
        limit: int = 100000,
    ) -> tuple[str, ...]: ...


class DocumentPublisherPort(Protocol):
    async def publish(
        self, *, document_id: str, candidate_document_version_id: str
    ) -> PublicationResult: ...

    async def retire_removed(self, *, document_id: str) -> DocumentRetirementResult: ...


class ReindexJobPort(Protocol):
    async def submit(
        self,
        *,
        reindex_job_id: str,
        target_processing_fingerprint: str,
        document_id: str | None = None,
    ) -> ReindexJob: ...

    async def start(self, reindex_job_id: str) -> ReindexJob: ...

    async def finish(
        self, reindex_job_id: str, *, progress: ReindexProgress, last_error_code: str | None = None
    ) -> ReindexJob: ...

    async def get(self, reindex_job_id: str) -> ReindexJob | None: ...

    async def stale_active_document_ids(
        self,
        *,
        tenant_id: str,
        target_processing_fingerprint: str,
        document_id: str | None = None,
        limit: int = 10000,
    ) -> tuple[str, ...]: ...


class IngestionReliabilityPort(Protocol):
    async def record_failure(self, failure: DocumentFailure) -> None: ...

    async def enqueue_cleanup(
        self, *, document_id: str, document_version_id: str
    ) -> ProjectionCleanupJob: ...

    async def pending_cleanup_jobs(
        self,
        *,
        limit: int = 100,
        document_ids: Sequence[str] | None = None,
        source_scope_id: str | None = None,
    ) -> tuple[ProjectionCleanupJob, ...]: ...

    async def cleanup_for_version(
        self, document_version_id: str
    ) -> ProjectionCleanupJob | None: ...

    async def start_cleanup(self, cleanup_job_id: str) -> None: ...

    async def claim_cleanup(self, cleanup_job_id: str) -> bool: ...

    async def complete_cleanup(self, cleanup_job_id: str) -> None: ...

    async def cancel_cleanup(self, cleanup_job_id: str, *, safe_reason_code: str) -> None: ...

    async def fail_cleanup(self, cleanup_job_id: str, *, safe_error_code: str) -> None: ...

    async def projection_manifest(self, document_version_id: str) -> ProjectionManifest | None: ...


class DocumentControlPort(Protocol):
    @property
    def document_versions(self) -> DocumentVersionPort: ...

    @property
    def publisher(self) -> DocumentPublisherPort: ...

    @property
    def reliability(self) -> IngestionReliabilityPort: ...

    @property
    def reindex(self) -> ReindexJobPort: ...
