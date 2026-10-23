"""Helpers shared by the retired-version retention tests."""

from __future__ import annotations

from datetime import timedelta

from sqlalchemy import update

from harborrag_adapters.repositories.database.ingestion_control import (
    IngestionControlPlaneDatabase,
)
from harborrag_adapters.repositories.database.ingestion_control.retention import (
    PURGE_LEASE_SECONDS,
)
from harborrag_adapters.repositories.database.ingestion_control.schema import (
    DOCUMENT_VERSIONS,
)
from harborrag_core.base import utc_now
from harborrag_core.chunking import ConnectorType
from harborrag_core.ingestion import (
    AdmissionSnapshot,
    ChangeFingerprintBuilder,
    DocumentIdentityBuilder,
    DocumentVersionCandidate,
    DocumentVersionState,
    ProjectionManifest,
    SourceIdentity,
)

from ...fixtures.release import processing_profile


async def expire_purge_lease(control: IngestionControlPlaneDatabase, version_id: str) -> None:
    """Age a PURGING claim past its lease, as if its purger had died."""

    async with control._client.sessions.begin() as session:
        await session.execute(
            update(DOCUMENT_VERSIONS)
            .where(DOCUMENT_VERSIONS.c.document_version_id == version_id)
            .values(updated_at=utc_now() - timedelta(seconds=PURGE_LEASE_SECONDS + 1))
        )


def _candidate(item: str, content: str) -> DocumentVersionCandidate:
    source = SourceIdentity(
        tenant_id="default",
        connector_type=ConnectorType.LOCAL,
        connection_id="local-docs",
        source_item_id=item,
        source_scope_id="docs",
    )
    fingerprints = ChangeFingerprintBuilder().build(
        admission=AdmissionSnapshot(source_version=content),
        canonical_evidence={"content": content},
        retrieval_metadata={"title": item},
        processing=processing_profile(),
    )
    identity = DocumentIdentityBuilder()
    document_id = identity.document_id(
        tenant_id=source.tenant_id,
        connector_type=source.connector_type,
        connection_id=source.connection_id,
        source_item_id=source.source_item_id,
    )
    return DocumentVersionCandidate(
        document_id=document_id,
        document_version_id=identity.document_version_id(
            document_id=document_id,
            canonical_content_hash=fingerprints.canonical_content_hash,
            retrieval_metadata_hash=fingerprints.retrieval_metadata_hash,
            processing_fingerprint=fingerprints.processing_fingerprint,
        ),
        source_identity=source,
        fingerprints=fingerprints,
    )


async def cleaned_retired_version(
    control: IngestionControlPlaneDatabase,
    item: str,
) -> tuple[str, str]:
    """Publish two versions of ``item``; return (document, retired) once cleaned."""

    versions = control.document_versions
    first, second = _candidate(item, "v1"), _candidate(item, "v2")
    for value in (first, second):
        version_id = str(value.document_version_id)
        await versions.create_candidate(value)
        for state in (
            DocumentVersionState.RAW_CAPTURED,
            DocumentVersionState.CANONICAL_READY,
            DocumentVersionState.CHUNKS_READY,
            DocumentVersionState.REPRESENTATIONS_READY,
            DocumentVersionState.PROJECTIONS_STAGED,
        ):
            await versions.transition(version_id, state)
        await versions.save_projection_manifest(
            ProjectionManifest(
                document_id=value.document_id,
                document_version_id=value.document_version_id,
                route_point_ids=("route-1",),
                evidence_point_ids=("evidence-1",),
                graph_node_keys=("document-node",),
                chunk_ids=("chunk-1",),
            )
        )
        await versions.mark_verified(version_id)
        await control.publisher.publish(
            document_id=str(value.document_id),
            candidate_document_version_id=version_id,
        )
    retired = str(first.document_version_id)
    job = await control.reliability.cleanup_for_version(retired)
    assert job is not None
    assert await control.reliability.claim_cleanup(job.cleanup_job_id)
    await control.reliability.complete_cleanup(job.cleanup_job_id)
    return str(first.document_id), retired
