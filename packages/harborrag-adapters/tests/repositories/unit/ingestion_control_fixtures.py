from __future__ import annotations

from pathlib import Path

from harborrag_adapters.repositories.database.ingestion_control import (
    IngestionControlPlaneDatabase,
)
from harborrag_adapters.repositories.database.sqlite.client import SQLiteDBClient
from harborrag_core.chunking import ConnectorType
from harborrag_core.ingestion import (
    AdmissionSnapshot,
    ChangeFingerprintBuilder,
    DocumentIdentityBuilder,
    DocumentVersionCandidate,
    DocumentVersionState,
    ProcessingProfile,
    ProjectionManifest,
    SourceIdentity,
)


def make_control_plane(tmp_path: Path) -> IngestionControlPlaneDatabase:
    return IngestionControlPlaneDatabase(
        SQLiteDBClient(database=str(tmp_path / "control-plane.db")),
        create_schema=True,
    )


def source_identity(source_item_id: str = "page-1") -> SourceIdentity:
    return SourceIdentity(
        tenant_id="DEFAULT",
        connector_type=ConnectorType.CONFLUENCE,
        connection_id="wiki.example",
        source_item_id=source_item_id,
        source_scope_id="scope-engineering",
    )


def candidate(
    content: str,
    *,
    source: SourceIdentity | None = None,
) -> DocumentVersionCandidate:
    selected_source = source or source_identity()
    fingerprints = ChangeFingerprintBuilder().build(
        admission=AdmissionSnapshot(source_version=content),
        canonical_evidence={"content": content},
        retrieval_metadata={"title": "Release guide"},
        processing=ProcessingProfile(
            parser_profile="confluence-v1",
            normalizer_version="canonical-v1",
            chunk_strategy="section-v1",
            dense_encoder_profile="dense-v1",
            sparse_encoder_profile="bm25-v1",
            graph_projection_version="graph-v1",
        ),
    )
    identity = DocumentIdentityBuilder()
    document_id = identity.document_id(
        tenant_id=selected_source.tenant_id,
        connector_type=selected_source.connector_type,
        connection_id=selected_source.connection_id,
        source_item_id=selected_source.source_item_id,
    )
    version_id = identity.document_version_id(
        document_id=document_id,
        canonical_content_hash=fingerprints.canonical_content_hash,
        retrieval_metadata_hash=fingerprints.retrieval_metadata_hash,
        processing_fingerprint=fingerprints.processing_fingerprint,
    )
    return DocumentVersionCandidate(
        document_id=document_id,
        document_version_id=version_id,
        source_identity=selected_source,
        fingerprints=fingerprints,
    )


async def advance_to_verified(
    control_plane: IngestionControlPlaneDatabase,
    value: DocumentVersionCandidate,
) -> None:
    repository = control_plane.document_versions
    await repository.create_candidate(value)
    for state in (
        DocumentVersionState.RAW_CAPTURED,
        DocumentVersionState.CANONICAL_READY,
        DocumentVersionState.CHUNKS_READY,
        DocumentVersionState.REPRESENTATIONS_READY,
        DocumentVersionState.PROJECTIONS_STAGED,
    ):
        await repository.transition(str(value.document_version_id), state)
    await repository.save_projection_manifest(
        ProjectionManifest(
            document_id=value.document_id,
            document_version_id=value.document_version_id,
            route_point_ids=("route-1",),
            evidence_point_ids=("evidence-1",),
            graph_node_keys=("document-node",),
            chunk_ids=("route-chunk", "evidence-chunk"),
        )
    )
    await repository.mark_verified(str(value.document_version_id))
