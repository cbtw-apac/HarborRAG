"""Durable side of the retired document-version retention TTL."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta

from sqlalchemy import and_, delete, func, null, or_, select, update
from sqlalchemy.sql import Select

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

# How long a purge owns a PURGING version without renewing its claim. The purger
# renews between object families and listing pages, so only a purger that died
# or hung lets the lease lapse, and only then may another worker resume it.
# A failed purge keeps its lease, which doubles as the retry backoff.
PURGE_LEASE_SECONDS = 900.0


@dataclass(frozen=True, slots=True)
class PurgeClaim:
    """One worker's ownership of a version's purge.

    ``claimed_at`` is the ``updated_at`` the claim wrote on the version row. Only
    the purge holding it may renew or finish the purge, so a worker that
    lost its lease can no longer mark a version PURGED under another's purge.
    """

    document_version_id: str
    claimed_at: datetime
    resumed: bool


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
        retired_before: datetime | None,
        limit: int = 100,
        tenant_id: str | None = None,
        source_scope_id: str | None = None,
    ) -> tuple[PurgeableDocumentVersion, ...]:
        """Versions a purge may claim now, least recently attempted first.

        RETIRED versions are eligible once retired before ``retired_before``;
        ``None`` (retention off) selects none of them. A PURGING version is
        eligible only once its lease lapsed -- an interrupted purge resumes even
        when retention is off, since its artifacts are already partly gone.
        Ordering by ``updated_at`` puts a version whose purge just failed behind
        every version not yet attempted.
        """

        if not 1 <= limit <= 1_000:
            raise ValueError("retention purge limit must be between 1 and 1000")
        statement = self._eligible(
            select(DOCUMENT_VERSIONS, DOCUMENTS.c.tenant_id),
            retired_before=retired_before,
            tenant_id=tenant_id,
            source_scope_id=source_scope_id,
        )
        async with self._client.sessions() as session:
            result = await session.execute(
                statement.order_by(
                    DOCUMENT_VERSIONS.c.updated_at,
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

    async def count_purgeable(self, *, retired_before: datetime | None) -> int:
        """How many versions ``purgeable_versions`` could still return: the backlog."""

        statement = self._eligible(
            select(func.count()).select_from(DOCUMENT_VERSIONS),
            retired_before=retired_before,
            tenant_id=None,
            source_scope_id=None,
        )
        async with self._client.sessions() as session:
            return int(await session.scalar(statement) or 0)

    @staticmethod
    def _eligible(
        statement: Select,
        *,
        retired_before: datetime | None,
        tenant_id: str | None,
        source_scope_id: str | None,
    ) -> Select:
        if tenant_id is not None and not tenant_id.strip():
            raise ValueError("retention tenant_id must be non-empty")
        if source_scope_id is not None and not source_scope_id.strip():
            raise ValueError("retention source_scope_id must be non-empty")
        lease_expired = DOCUMENT_VERSIONS.c.updated_at < _lease_cutoff()
        states = [
            and_(
                DOCUMENT_VERSIONS.c.status == DocumentVersionState.PURGING.value,
                lease_expired,
            )
        ]
        if retired_before is not None:
            states.append(
                and_(
                    DOCUMENT_VERSIONS.c.status == DocumentVersionState.RETIRED.value,
                    DOCUMENT_VERSIONS.c.retired_at <= retired_before,
                )
            )
        statement = (
            statement.join(DOCUMENTS, DOCUMENTS.c.document_id == DOCUMENT_VERSIONS.c.document_id)
            .join(
                PROJECTION_CLEANUP_JOBS,
                PROJECTION_CLEANUP_JOBS.c.document_version_id
                == DOCUMENT_VERSIONS.c.document_version_id,
            )
            .where(
                or_(*states),
                DOCUMENT_VERSIONS.c.retired_at.is_not(None),
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
        return statement

    async def raw_keys_in_use(
        self,
        *,
        document_id: str,
        excluding_version_id: str,
    ) -> frozenset[tuple[str, str]]:
        """Return raw (bucket, key) pairs another unpurged version still references.

        Raw objects are content-addressed per document, so a later version with
        the same bytes points at the same key. ACTIVE, RETIRED-not-yet-purged,
        FAILED and in-flight versions may all replay from their raw capture, so
        they count. PURGED and PURGING versions do not: two versions sharing a key
        and purged at the same time would otherwise each keep it for the other,
        and the key would outlive both.
        """

        async with self._client.sessions() as session:
            result = await session.execute(
                select(
                    DOCUMENT_VERSIONS.c.raw_artifact,
                    DOCUMENT_VERSIONS.c.raw_metadata_artifact,
                ).where(
                    DOCUMENT_VERSIONS.c.document_id == document_id,
                    DOCUMENT_VERSIONS.c.document_version_id != excluding_version_id,
                    DOCUMENT_VERSIONS.c.status.not_in(
                        (DocumentVersionState.PURGED.value, DocumentVersionState.PURGING.value)
                    ),
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
        """Boolean form of ``claim_purge``."""

        return await self.claim_purge(document_version_id) is not None

    async def claim_purge(self, document_version_id: str) -> PurgeClaim | None:
        """Claim a version for purging (RETIRED -> PURGING) before any delete.

        While PURGING, replay refuses the version (transiently), so it can never be
        restored onto artifacts that are being deleted. A version already PURGING
        is taken over (``resumed``) only once its lease lapsed: its purger died or
        hung. Returns None, changing nothing, while another purge holds the lease
        or when the version is no longer a retired, inactive version.
        """

        now = utc_now()
        async with self._client.sessions.begin() as session:
            result = await session.execute(
                select(
                    DOCUMENT_VERSIONS.c.status,
                    DOCUMENT_VERSIONS.c.document_id,
                    DOCUMENT_VERSIONS.c.updated_at,
                )
                .where(DOCUMENT_VERSIONS.c.document_version_id == document_version_id)
                .with_for_update()
            )
            row = result.mappings().one_or_none()
            if row is None:
                return None
            resumed = row["status"] == DocumentVersionState.PURGING.value
            if resumed and row["updated_at"] >= _lease_cutoff():
                return None
            if not resumed and row["status"] != DocumentVersionState.RETIRED.value:
                return None
            active = await session.execute(
                select(DOCUMENTS.c.active_document_version_id).where(
                    DOCUMENTS.c.document_id == row["document_id"]
                )
            )
            if active.scalar_one_or_none() == document_version_id:
                return None
            await session.execute(
                update(DOCUMENT_VERSIONS)
                .where(DOCUMENT_VERSIONS.c.document_version_id == document_version_id)
                .values(status=DocumentVersionState.PURGING.value, updated_at=now)
            )
            return PurgeClaim(document_version_id, now, resumed)

    async def renew_purge(self, claim: PurgeClaim) -> PurgeClaim | None:
        """Extend the lease; None means another worker took the purge over."""

        now = utc_now()
        async with self._client.sessions.begin() as session:
            result = await session.execute(
                update(DOCUMENT_VERSIONS)
                .where(
                    DOCUMENT_VERSIONS.c.document_version_id == claim.document_version_id,
                    DOCUMENT_VERSIONS.c.status == DocumentVersionState.PURGING.value,
                    DOCUMENT_VERSIONS.c.updated_at == claim.claimed_at,
                )
                .values(updated_at=now)
            )
        if getattr(result, "rowcount", 0) != 1:
            return None
        return PurgeClaim(claim.document_version_id, now, claim.resumed)

    async def mark_version_purged(
        self,
        document_version_id: str,
        *,
        claimed_at: datetime | None = None,
    ) -> bool:
        """Drop the manifest and mark a PURGING version PURGED in one transaction.

        Returns False, changing nothing, when the version is not PURGING -- it
        was never claimed, or another worker already finished it -- or, given the
        claim's ``claimed_at``, when another worker has since taken the purge over.
        """

        now = utc_now()
        async with self._client.sessions.begin() as session:
            result = await session.execute(
                select(
                    DOCUMENT_VERSIONS.c.status,
                    DOCUMENT_VERSIONS.c.document_id,
                    DOCUMENT_VERSIONS.c.updated_at,
                )
                .where(DOCUMENT_VERSIONS.c.document_version_id == document_version_id)
                .with_for_update()
            )
            row = result.mappings().one_or_none()
            if row is None or row["status"] != DocumentVersionState.PURGING.value:
                return False
            if claimed_at is not None and row["updated_at"] != claimed_at:
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


def _lease_cutoff() -> datetime:
    return utc_now() - timedelta(seconds=PURGE_LEASE_SECONDS)
