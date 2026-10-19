"""Durable side of the retired document-version retention TTL."""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import delete, null, or_, select, update

from harborrag_adapters.repositories.backends.sqlalchemy import SQLAlchemyDBClient
from harborrag_core.base import utc_now
from harborrag_core.ingestion import (
    ArtifactReference,
    CleanupJobState,
    DocumentVersionState,
    PurgeableDocumentVersion,
)

from .document_version_mapping import snapshot_from_row
from .row_values import required_datetime, required_text
from .schema import (
    DOCUMENT_VERSIONS,
    DOCUMENTS,
    PROJECTION_CLEANUP_JOBS,
    PROJECTION_MANIFESTS,
    SOURCE_ITEMS,
)

_ARTIFACT_COLUMNS = (
    "raw_artifact",
    "raw_metadata_artifact",
    "canonical_artifact",
    "chunk_artifact",
    "chunk_index_artifact",
    "relation_artifact",
    "representation_artifact",
)
_RAW_COLUMNS = ("raw_artifact", "raw_metadata_artifact")


class RetiredVersionRetentionRepository:
    """Select retired versions past their TTL and record their purge.

    The projection manifest is what projection cleanup deletes vectors through,
    so a version becomes purgeable only once its cleanup job has COMPLETED.
    """

    def __init__(self, client: SQLAlchemyDBClient) -> None:
        self._client = client

    async def purgeable_versions(
        self,
        *,
        retired_before: datetime,
        limit: int = 100,
        tenant_id: str | None = None,
        source_scope_id: str | None = None,
    ) -> tuple[PurgeableDocumentVersion, ...]:
        if not 1 <= limit <= 1_000:
            raise ValueError("retention purge limit must be between 1 and 1000")
        if tenant_id is not None and not tenant_id.strip():
            raise ValueError("retention tenant_id must be non-empty")
        if source_scope_id is not None and not source_scope_id.strip():
            raise ValueError("retention source_scope_id must be non-empty")
        statement = (
            select(DOCUMENT_VERSIONS, DOCUMENTS.c.tenant_id)
            .join(DOCUMENTS, DOCUMENTS.c.document_id == DOCUMENT_VERSIONS.c.document_id)
            .join(
                PROJECTION_CLEANUP_JOBS,
                PROJECTION_CLEANUP_JOBS.c.document_version_id
                == DOCUMENT_VERSIONS.c.document_version_id,
            )
            .where(
                # PURGING too: a purge interrupted mid-way resumes on the next run.
                DOCUMENT_VERSIONS.c.status.in_(
                    (DocumentVersionState.RETIRED.value, DocumentVersionState.PURGING.value)
                ),
                DOCUMENT_VERSIONS.c.retired_at.is_not(None),
                DOCUMENT_VERSIONS.c.retired_at <= retired_before,
                PROJECTION_CLEANUP_JOBS.c.status == CleanupJobState.COMPLETED.value,
                or_(
                    DOCUMENTS.c.active_document_version_id.is_(None),
                    DOCUMENTS.c.active_document_version_id
                    != DOCUMENT_VERSIONS.c.document_version_id,
                ),
            )
        )
        if tenant_id is not None:
            statement = statement.where(DOCUMENTS.c.tenant_id == tenant_id)
        if source_scope_id is not None:
            # Mirror projection cleanup's scope: every document the scope binds,
            # so the purge after a scope's cleanup sees the versions it cleaned.
            # A subquery rather than a join + DISTINCT, which PostgreSQL rejects
            # over the JSON artifact columns.
            statement = statement.where(
                DOCUMENT_VERSIONS.c.document_id.in_(
                    select(SOURCE_ITEMS.c.document_id).where(
                        SOURCE_ITEMS.c.source_scope_id == source_scope_id
                    )
                )
            )
        async with self._client.sessions() as session:
            result = await session.execute(
                statement.order_by(
                    DOCUMENT_VERSIONS.c.retired_at,
                    DOCUMENT_VERSIONS.c.document_version_id,
                ).limit(limit)
            )
            return tuple(
                PurgeableDocumentVersion(
                    tenant_id=required_text(row, "tenant_id"),
                    retired_at=required_datetime(row, "retired_at"),
                    snapshot=snapshot_from_row(row),
                )
                for row in result.mappings().all()
            )

    async def raw_keys_in_use(
        self,
        *,
        document_id: str,
        excluding_version_id: str,
    ) -> frozenset[tuple[str, str]]:
        """Return raw (bucket, key) pairs another unpurged version still references.

        Raw objects are content-addressed per document, so a later version with
        the same bytes points at the same key. Every state except PURGED counts:
        ACTIVE, RETIRED-not-yet-purged, FAILED and in-flight versions may all
        replay from their raw capture.
        """

        async with self._client.sessions() as session:
            result = await session.execute(
                select(
                    DOCUMENT_VERSIONS.c.raw_artifact,
                    DOCUMENT_VERSIONS.c.raw_metadata_artifact,
                ).where(
                    DOCUMENT_VERSIONS.c.document_id == document_id,
                    DOCUMENT_VERSIONS.c.document_version_id != excluding_version_id,
                    DOCUMENT_VERSIONS.c.status != DocumentVersionState.PURGED.value,
                )
            )
            keys: set[tuple[str, str]] = set()
            for row in result.mappings().all():
                for column in _RAW_COLUMNS:
                    value = row[column]
                    if value is not None:
                        reference = ArtifactReference.model_validate(value)
                        keys.add((reference.bucket, reference.key))
            return frozenset(keys)

    async def begin_purge(self, document_version_id: str) -> bool:
        """Claim a RETIRED version for purging (RETIRED -> PURGING) before any delete.

        While PURGING, replay refuses the version (transiently), so it can never be
        restored onto artifacts that are being deleted. Returns True for a version
        already PURGING (an interrupted purge resuming) and False, changing
        nothing, when it is no longer a retired, inactive version.
        """

        async with self._client.sessions.begin() as session:
            result = await session.execute(
                select(DOCUMENT_VERSIONS.c.status, DOCUMENT_VERSIONS.c.document_id)
                .where(DOCUMENT_VERSIONS.c.document_version_id == document_version_id)
                .with_for_update()
            )
            row = result.mappings().one_or_none()
            if row is None:
                return False
            if row["status"] == DocumentVersionState.PURGING.value:
                return True
            if row["status"] != DocumentVersionState.RETIRED.value:
                return False
            active = await session.execute(
                select(DOCUMENTS.c.active_document_version_id).where(
                    DOCUMENTS.c.document_id == row["document_id"]
                )
            )
            if active.scalar_one_or_none() == document_version_id:
                return False
            await session.execute(
                update(DOCUMENT_VERSIONS)
                .where(DOCUMENT_VERSIONS.c.document_version_id == document_version_id)
                .values(status=DocumentVersionState.PURGING.value, updated_at=utc_now())
            )
            return True

    async def mark_version_purged(self, document_version_id: str) -> bool:
        """Drop the manifest and mark a PURGING version PURGED in one transaction.

        Returns False, changing nothing, when the version is not PURGING -- it
        was never claimed, or another worker already finished it.
        """

        now = utc_now()
        async with self._client.sessions.begin() as session:
            result = await session.execute(
                select(
                    DOCUMENT_VERSIONS.c.status,
                    DOCUMENT_VERSIONS.c.document_id,
                )
                .where(DOCUMENT_VERSIONS.c.document_version_id == document_version_id)
                .with_for_update()
            )
            row = result.mappings().one_or_none()
            if row is None or row["status"] != DocumentVersionState.PURGING.value:
                return False
            active = await session.execute(
                select(DOCUMENTS.c.active_document_version_id).where(
                    DOCUMENTS.c.document_id == row["document_id"]
                )
            )
            if active.scalar_one_or_none() == document_version_id:
                return False
            await session.execute(
                delete(PROJECTION_MANIFESTS).where(
                    PROJECTION_MANIFESTS.c.document_version_id == document_version_id
                )
            )
            await session.execute(
                update(DOCUMENT_VERSIONS)
                .where(
                    DOCUMENT_VERSIONS.c.document_version_id == document_version_id,
                    DOCUMENT_VERSIONS.c.status == DocumentVersionState.PURGING.value,
                )
                .values(
                    status=DocumentVersionState.PURGED.value,
                    updated_at=now,
                    # SQL NULL, not the JSON ``null`` a bare None would store.
                    **{column: null() for column in _ARTIFACT_COLUMNS},
                )
            )
            return True
