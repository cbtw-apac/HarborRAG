from __future__ import annotations

from collections.abc import Sequence

from sqlalchemy import insert, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from harborrag_adapters.repositories.backends.sqlalchemy import SQLAlchemyDBClient
from harborrag_core.base import utc_now
from harborrag_core.contracts import (
    HarborConflictError,
    HarborNotFoundError,
    HarborValidationError,
)
from harborrag_core.ingestion import (
    ActiveDocumentVersion,
    ActiveSourceDocument,
    ArtifactReference,
    DocumentVersionCandidate,
    DocumentVersionSnapshot,
    DocumentVersionState,
    ProjectionManifest,
)
from harborrag_core.invariants import HarborInvariantError

from .document_version_access import DocumentVersionReader, DocumentVersionReplay
from .document_version_mapping import (
    validate_candidate_identity,
    validate_document_source,
    validate_immutable_version,
)
from .row_values import DatabaseRow
from .schema import (
    DOCUMENT_VERSIONS,
    DOCUMENTS,
    PROJECTION_MANIFESTS,
)

_ALLOWED_TRANSITIONS: dict[DocumentVersionState, frozenset[DocumentVersionState]] = {
    DocumentVersionState.PENDING: frozenset(
        {
            DocumentVersionState.RAW_CAPTURED,
            DocumentVersionState.CANONICAL_READY,
            DocumentVersionState.FAILED,
        }
    ),
    DocumentVersionState.RAW_CAPTURED: frozenset(
        {DocumentVersionState.CANONICAL_READY, DocumentVersionState.FAILED}
    ),
    DocumentVersionState.CANONICAL_READY: frozenset(
        {DocumentVersionState.CHUNKS_READY, DocumentVersionState.FAILED}
    ),
    DocumentVersionState.CHUNKS_READY: frozenset(
        {DocumentVersionState.REPRESENTATIONS_READY, DocumentVersionState.FAILED}
    ),
    DocumentVersionState.REPRESENTATIONS_READY: frozenset(
        {DocumentVersionState.PROJECTIONS_STAGED, DocumentVersionState.FAILED}
    ),
    DocumentVersionState.PROJECTIONS_STAGED: frozenset(
        {DocumentVersionState.VERIFIED, DocumentVersionState.FAILED}
    ),
    DocumentVersionState.VERIFIED: frozenset(
        {DocumentVersionState.ACTIVE, DocumentVersionState.FAILED}
    ),
    DocumentVersionState.ACTIVE: frozenset({DocumentVersionState.RETIRED}),
    DocumentVersionState.RETIRED: frozenset(),
    DocumentVersionState.FAILED: frozenset(),
}

_ARTIFACT_COLUMNS = frozenset(
    {
        "raw_artifact",
        "raw_metadata_artifact",
        "canonical_artifact",
        "chunk_artifact",
        "chunk_index_artifact",
        "relation_artifact",
        "representation_artifact",
    }
)


class DocumentVersionRepository:
    """Persist immutable version metadata and guarded business-state transitions."""

    def __init__(self, client: SQLAlchemyDBClient) -> None:
        self._client = client
        self._reader = DocumentVersionReader(client)
        self._replay = DocumentVersionReplay(client, self._reader)

    async def create_candidate(self, candidate: DocumentVersionCandidate) -> DocumentVersionState:
        validate_candidate_identity(candidate)
        if candidate.state != DocumentVersionState.PENDING:
            raise HarborValidationError("new document versions must begin in PENDING")
        now = utc_now()
        async with self._client.sessions.begin() as session:
            document_result = await session.execute(
                select(DOCUMENTS)
                .where(DOCUMENTS.c.document_id == str(candidate.document_id))
                .with_for_update()
            )
            document = document_result.mappings().one_or_none()
            if document is None:
                await session.execute(
                    insert(DOCUMENTS).values(
                        document_id=str(candidate.document_id),
                        tenant_id=candidate.source_identity.tenant_id,
                        source_scope_id=candidate.source_identity.source_scope_id,
                        connector_type=candidate.source_identity.connector_type.value,
                        connection_id=candidate.source_identity.connection_id,
                        source_item_id=candidate.source_identity.source_item_id,
                        active_document_version_id=None,
                        created_at=now,
                        updated_at=now,
                    )
                )
            else:
                validate_document_source(document, candidate)
            version_result = await session.execute(
                select(DOCUMENT_VERSIONS).where(
                    DOCUMENT_VERSIONS.c.document_version_id == str(candidate.document_version_id)
                )
            )
            existing = version_result.mappings().one_or_none()
            if existing is not None:
                validate_immutable_version(existing, candidate)
                return DocumentVersionState(existing["status"])
            await session.execute(
                insert(DOCUMENT_VERSIONS).values(
                    document_version_id=str(candidate.document_version_id),
                    document_id=str(candidate.document_id),
                    canonical_content_hash=candidate.fingerprints.canonical_content_hash,
                    retrieval_metadata_hash=candidate.fingerprints.retrieval_metadata_hash,
                    processing_fingerprint=candidate.fingerprints.processing_fingerprint,
                    admission_change_key=candidate.fingerprints.admission_change_key,
                    status=DocumentVersionState.PENDING.value,
                    created_at=candidate.created_at or now,
                    updated_at=now,
                )
            )
        return DocumentVersionState.PENDING

    async def transition(
        self,
        document_version_id: str,
        target: DocumentVersionState,
        *,
        artifact_column: str | None = None,
        artifact: ArtifactReference | None = None,
    ) -> None:
        if (artifact_column is None) != (artifact is None):
            raise ValueError("artifact column and reference must be supplied together")
        if artifact_column is not None and artifact_column not in _ARTIFACT_COLUMNS:
            raise ValueError(f"unsupported document-version artifact column: {artifact_column}")
        async with self._client.sessions.begin() as session:
            result = await session.execute(
                select(DOCUMENT_VERSIONS)
                .where(DOCUMENT_VERSIONS.c.document_version_id == document_version_id)
                .with_for_update()
            )
            row = result.mappings().one_or_none()
            if row is None:
                raise HarborNotFoundError(f"document version does not exist: {document_version_id}")
            current = DocumentVersionState(row["status"])
            if current == target:
                await self._attach_replayed_artifact(
                    session,
                    row=row,
                    document_version_id=document_version_id,
                    artifact_column=artifact_column,
                    artifact=artifact,
                )
                return
            if target not in _ALLOWED_TRANSITIONS[current]:
                raise HarborConflictError(
                    f"invalid document-version transition: {current.value} -> {target.value}"
                )
            values: dict[str, object] = {"status": target.value, "updated_at": utc_now()}
            if artifact_column is not None:
                if artifact is None:
                    raise HarborInvariantError("artifact must not be None here")
                existing_artifact = row[artifact_column]
                serialized = artifact.model_dump(mode="json")
                if existing_artifact is not None and existing_artifact != serialized:
                    raise HarborConflictError("document-version artifacts are immutable")
                values[artifact_column] = serialized
            await session.execute(
                update(DOCUMENT_VERSIONS)
                .where(DOCUMENT_VERSIONS.c.document_version_id == document_version_id)
                .values(**values)
            )

    @staticmethod
    async def _attach_replayed_artifact(
        session: AsyncSession,
        *,
        row: DatabaseRow,
        document_version_id: str,
        artifact_column: str | None,
        artifact: ArtifactReference | None,
    ) -> None:
        if artifact_column is None or artifact is None:
            return
        serialized = artifact.model_dump(mode="json")
        existing = row[artifact_column]
        if existing is not None and existing != serialized:
            raise HarborConflictError(
                "a replay attempted to replace an immutable artifact reference"
            )
        if existing is None:
            await session.execute(
                update(DOCUMENT_VERSIONS)
                .where(DOCUMENT_VERSIONS.c.document_version_id == document_version_id)
                .values(
                    **{
                        artifact_column: serialized,
                        "updated_at": utc_now(),
                    }
                )
            )

    async def save_projection_manifest(self, manifest: ProjectionManifest) -> None:
        serialized = manifest.model_dump(mode="json")
        now = utc_now()
        async with self._client.sessions.begin() as session:
            result = await session.execute(
                select(PROJECTION_MANIFESTS).where(
                    PROJECTION_MANIFESTS.c.document_version_id == str(manifest.document_version_id)
                )
            )
            row = result.mappings().one_or_none()
            if row is None:
                await session.execute(
                    insert(PROJECTION_MANIFESTS).values(
                        document_version_id=str(manifest.document_version_id),
                        document_id=str(manifest.document_id),
                        manifest=serialized,
                        created_at=now,
                        updated_at=now,
                    )
                )
                return
            if row["manifest"] != serialized:
                raise HarborConflictError("projection manifests are immutable")

    async def mark_verified(self, document_version_id: str) -> None:
        async with self._client.sessions.begin() as session:
            manifest = await session.execute(
                select(PROJECTION_MANIFESTS.c.document_version_id).where(
                    PROJECTION_MANIFESTS.c.document_version_id == document_version_id
                )
            )
            if manifest.scalar_one_or_none() is None:
                raise HarborConflictError(
                    "a document version cannot be verified without a projection manifest"
                )
            result = await session.execute(
                select(DOCUMENT_VERSIONS.c.status).where(
                    DOCUMENT_VERSIONS.c.document_version_id == document_version_id
                )
            )
            status = result.scalar_one_or_none()
            if status is None:
                raise HarborNotFoundError(f"document version does not exist: {document_version_id}")
            current = DocumentVersionState(status)
            if current in {
                DocumentVersionState.VERIFIED,
                DocumentVersionState.ACTIVE,
            }:
                return
            if DocumentVersionState.VERIFIED not in _ALLOWED_TRANSITIONS[current]:
                raise HarborConflictError(
                    f"document version is not ready for verification: {current.value}"
                )
            now = utc_now()
            await session.execute(
                update(DOCUMENT_VERSIONS)
                .where(DOCUMENT_VERSIONS.c.document_version_id == document_version_id)
                .values(status=DocumentVersionState.VERIFIED.value, updated_at=now)
            )
            await session.execute(
                update(PROJECTION_MANIFESTS)
                .where(PROJECTION_MANIFESTS.c.document_version_id == document_version_id)
                .values(verified_at=now, updated_at=now)
            )

    async def active_versions(
        self,
        document_ids: Sequence[str],
    ) -> dict[str, ActiveDocumentVersion]:
        return await self._reader.active_versions(document_ids)

    async def get_version(
        self,
        document_version_id: str,
    ) -> DocumentVersionSnapshot | None:
        return await self._reader.get_version(document_version_id)

    async def prepare_replay(
        self,
        document_version_id: str,
    ) -> DocumentVersionState:
        return await self._replay.prepare(document_version_id)

    async def resume_failed(
        self,
        document_version_id: str,
    ) -> DocumentVersionState:
        return await self._replay.resume_failed(document_version_id)

    async def active_snapshot(
        self,
        document_id: str,
    ) -> DocumentVersionSnapshot | None:
        return await self._reader.active_snapshot(document_id)

    async def resolve_active_sources(
        self,
        *,
        source_scope_id: str,
        source_item_ids: Sequence[str],
    ) -> dict[str, ActiveSourceDocument]:
        return await self._reader.resolve_active_sources(
            source_scope_id=source_scope_id,
            source_item_ids=source_item_ids,
        )

    async def active_relation_document_ids(
        self,
        *,
        processing_fingerprint: str,
        anchor_document_id: str | None = None,
        limit: int = 100_000,
    ) -> tuple[str, ...]:
        return await self._reader.active_relation_document_ids(
            processing_fingerprint=processing_fingerprint,
            anchor_document_id=anchor_document_id,
            limit=limit,
        )
