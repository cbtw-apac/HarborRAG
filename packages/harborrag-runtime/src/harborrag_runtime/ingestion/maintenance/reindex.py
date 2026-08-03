"""Connector-free document reindexing service."""

from __future__ import annotations

import logging
from dataclasses import dataclass

from harborrag_core.chunking import ConnectorType
from harborrag_core.domain.document import Document
from harborrag_core.domain.source import SourceRecord
from harborrag_core.ingestion import (
    AdmissionSnapshot,
    BindingKind,
    ChangeFingerprintBuilder,
    DocumentIdentityBuilder,
    DocumentVersionCandidate,
    ProcessingProfile,
    ReindexJob,
    ReindexJobState,
    ReindexProgress,
    SourceAdmissionDecision,
    SourceBinding,
    SourceIdentity,
    identity_for_source,
)
from harborrag_core.schemas.ids import DocumentId
from harborrag_core.storage import StorageOperationContext
from harborrag_engine.ingestion import (
    CanonicalVersionPlanner,
    ChunkVersionRebinder,
    source_version_from_document,
)

from ..document.dependencies import DocumentReleaseDependencies
from ..document.lifecycle import DocumentVersionLifecycle
from ..document.models import DocumentReleaseRequest
from ..document.pipeline import DocumentStagePipeline
from .reindex_plan import ReindexPlan, processing_profile_from_canonical
from .reindex_reuse import ChunkReuseCoordinator

logger = logging.getLogger("harborrag.runtime.ingestion.reindex")


@dataclass(frozen=True, slots=True)
class ReindexRequest:
    """Select stale active documents for a connector-free reprojection."""

    reindex_job_id: str
    tenant_id: str
    processing: ProcessingProfile
    document_id: str | None = None
    limit: int = 10_000

    def __post_init__(self) -> None:
        if not self.reindex_job_id.strip() or not self.tenant_id.strip():
            raise ValueError("reindex job and tenant IDs must be non-empty")
        if not 1 <= self.limit <= 100_000:
            raise ValueError("reindex limit must be between 1 and 100000")


class DocumentReindexService:
    """Rebuild active versions exclusively from immutable canonical artifacts."""

    def __init__(
        self,
        dependencies: DocumentReleaseDependencies,
        *,
        pipeline: DocumentStagePipeline | None = None,
    ) -> None:
        self._dependencies = dependencies
        self._fingerprints = ChangeFingerprintBuilder()
        self._identities = DocumentIdentityBuilder()
        self._planner = CanonicalVersionPlanner()
        self._chunk_rebinder = ChunkVersionRebinder()
        self._pipeline = pipeline or DocumentStagePipeline(dependencies)
        self._lifecycle = DocumentVersionLifecycle(
            control=dependencies.control,
            canonical_artifacts=dependencies.canonical_artifacts,
            chunk_reader=dependencies.chunk_reader,
            projection_artifacts=dependencies.projection_artifacts,
        )
        self._reuse = ChunkReuseCoordinator(
            dependencies=dependencies,
            pipeline=self._pipeline,
            lifecycle=self._lifecycle,
            chunk_rebinder=self._chunk_rebinder,
        )

    async def run(self, request: ReindexRequest) -> ReindexJob:
        target_fingerprint = self._fingerprints.processing_fingerprint(profile=request.processing)
        submitted = await self._dependencies.control.reindex.submit(
            reindex_job_id=request.reindex_job_id,
            document_id=request.document_id,
            target_processing_fingerprint=target_fingerprint,
        )
        if submitted.status == ReindexJobState.COMPLETED:
            return submitted
        await self._dependencies.control.reindex.start(request.reindex_job_id)
        document_ids = await self._dependencies.control.reindex.stale_active_document_ids(
            tenant_id=request.tenant_id,
            target_processing_fingerprint=target_fingerprint,
            document_id=request.document_id,
            limit=request.limit,
        )
        published = 0
        skipped = 0
        failures = 0
        last_error_code: str | None = None
        for document_id in document_ids:
            try:
                was_published = await self._reindex_document(
                    document_id,
                    request=request,
                    target_processing_fingerprint=target_fingerprint,
                )
                if was_published:
                    published += 1
                else:
                    skipped += 1
            except Exception as error:
                logger.exception(
                    "Connector-free document reindex failed",
                    extra={
                        "reindex_job_id": request.reindex_job_id,
                        "document_id": document_id,
                    },
                )
                failures += 1
                last_error_code = f"reindex-{type(error).__name__.lower()}"
        return await self._dependencies.control.reindex.finish(
            request.reindex_job_id,
            progress=ReindexProgress(
                scanned_count=len(document_ids),
                processed_count=len(document_ids),
                published_count=published,
                skipped_count=skipped,
                failure_count=failures,
            ),
            last_error_code=last_error_code,
        )

    async def _reindex_document(
        self,
        document_id: str,
        *,
        request: ReindexRequest,
        target_processing_fingerprint: str,
    ) -> bool:
        active = await self._dependencies.control.document_versions.active_snapshot(document_id)
        if active is None:
            return False
        if active.fingerprints.processing_fingerprint == target_processing_fingerprint:
            return False
        if active.canonical_artifact is None:
            raise ValueError("active document version has no canonical artifact")
        context = StorageOperationContext.system(request.tenant_id)
        canonical = await self._dependencies.canonical_artifacts.get(
            active.canonical_artifact,
            context=context,
        )
        source_identity = source_identity_from_canonical(canonical)
        if str(identity_for_source(source_identity)) != document_id:
            raise ValueError("canonical source identity does not match document")
        current_processing = processing_profile_from_canonical(
            canonical.provenance.extra.get("processing_profile")
        )
        reindex_plan = ReindexPlan.between(current_processing, request.processing)
        fingerprints = active.fingerprints.model_copy(
            update={"processing_fingerprint": target_processing_fingerprint}
        )
        typed_document_id = DocumentId(document_id)
        document_version_id = self._identities.document_version_id(
            document_id=typed_document_id,
            canonical_content_hash=fingerprints.canonical_content_hash,
            retrieval_metadata_hash=fingerprints.retrieval_metadata_hash,
            processing_fingerprint=target_processing_fingerprint,
        )
        reidentified = self._planner.reidentify(
            canonical,
            document_id=typed_document_id,
            document_version_id=document_version_id,
            source_identity=source_identity,
            processing=request.processing,
        )
        candidate = DocumentVersionCandidate(
            document_id=typed_document_id,
            document_version_id=document_version_id,
            source_identity=source_identity,
            fingerprints=fingerprints,
        )
        prepared = await self._pipeline.preparation.prepare_canonical(
            tenant_id=request.tenant_id,
            candidate=candidate,
            document=reidentified,
            decision=SourceAdmissionDecision.FORCE_REPROCESS,
        )
        await self._reuse.preserve_raw_boundary(prepared, active)
        release_request = DocumentReleaseRequest(
            tenant_id=request.tenant_id,
            connector_name=source_identity.connection_id,
            source=SourceRecord(
                id=source_identity.source_item_id,
                source_type=canonical.content_type,
                locator=canonical.provenance.source,
            ),
            source_identity=source_identity,
            admission=AdmissionSnapshot(source_version=source_version_from_document(canonical)),
            processing=request.processing,
            force_reprocess=True,
        )
        if not reindex_plan.rebuild_chunks:
            await self._reuse.reuse_chunks_and_representations(
                request=release_request,
                prepared=prepared,
                active=active,
                plan=reindex_plan,
            )
        outcome = await self._pipeline.release_prepared(
            release_request,
            prepared,
        )
        return bool(outcome.published)


def source_identity_from_canonical(document: Document) -> SourceIdentity:
    """Recover stable source identity without consulting a source connector."""

    metadata = document.provenance.extra
    required = {
        name: str(metadata.get(name, "")).strip()
        for name in (
            "connector_type",
            "connection_id",
            "source_item_id",
            "source_scope_id",
        )
    }
    missing = tuple(name for name, value in required.items() if not value)
    if missing:
        raise ValueError("canonical source identity is incomplete: " + ", ".join(missing))
    binding_kind = BindingKind(str(metadata.get("binding_kind", BindingKind.ROOT.value)))
    parent = metadata.get("parent_source_item_id")
    parent_source_item_id = (
        str(parent).strip() if parent is not None and str(parent).strip() else None
    )
    return SourceIdentity(
        tenant_id=str(metadata.get("tenant_id") or "DEFAULT"),
        connector_type=ConnectorType(required["connector_type"]),
        connection_id=required["connection_id"],
        source_item_id=required["source_item_id"],
        source_scope_id=required["source_scope_id"],
        binding=SourceBinding(
            kind=binding_kind,
            parent_source_item_id=parent_source_item_id,
        ),
    )
