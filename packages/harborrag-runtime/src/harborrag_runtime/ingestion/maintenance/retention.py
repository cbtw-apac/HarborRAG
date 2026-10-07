"""Retention TTL for RETIRED document versions.

Projection cleanup removes a retired version's vectors and graph data but keeps
its object-store artifacts and projection manifest. Once the version has been
retired for longer than the configured TTL, this service deletes the objects
the version owns and marks it PURGED. The row itself stays: task results keep
referencing it, and re-ingesting identical content rebuilds the same
deterministic version id from PENDING.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta

from harborrag_adapters.repositories.database import IngestionControlPlaneDatabase
from harborrag_adapters.repositories.errors import HarborStorageNotFoundError
from harborrag_adapters.repositories.object_store import (
    ARTIFACT_BUCKET,
    IngestionArtifactLayout,
)
from harborrag_core.base import utc_now
from harborrag_core.ingestion import PurgeableDocumentVersion
from harborrag_core.ports.storage import ObjectStorePort
from harborrag_core.storage import StorageOperationContext

logger = logging.getLogger("harborrag.runtime.ingestion.retention")

# Object families whose keys are addressed by (document_id, document_version_id).
# ``parsed/`` is a content-addressed cache shared across versions and
# ``source-plans/`` belongs to tasks, so neither is ever touched here.
_VERSION_FAMILIES = (
    "canonical",
    "chunks",
    "comments",
    "relations",
    "projections",
    "tables",
    "representations",
)
_SHARED_PREFIXES = ("parsed/", "source-plans/")
_LIST_PAGE = 1_000
_MAX_LIST_PASSES = 100


@dataclass(frozen=True, slots=True)
class RetiredVersionPurgeBatch:
    eligible: int = 0
    purged: int = 0
    skipped: int = 0
    failed: int = 0
    deleted_objects: int = 0


class RetiredVersionPurgeService:
    """Delete the artifacts of retired versions whose retention TTL elapsed."""

    def __init__(
        self,
        *,
        control: IngestionControlPlaneDatabase,
        object_store: ObjectStorePort,
        retention_days: int | None,
        clock: Callable[[], datetime] = utc_now,
    ) -> None:
        if retention_days is not None and retention_days < 0:
            raise ValueError("retired version retention days must not be negative")
        self._control = control
        self._store = object_store
        self._retention_days = retention_days
        self._clock = clock

    async def run_scope(
        self,
        *,
        tenant_id: str,
        source_scope_id: str,
        limit: int = 100,
    ) -> RetiredVersionPurgeBatch:
        if self._retention_days is None:
            return RetiredVersionPurgeBatch()
        versions = await self._control.retention.purgeable_versions(
            tenant_id=tenant_id,
            source_scope_id=source_scope_id,
            retired_before=self._retired_before(self._retention_days),
            limit=limit,
        )
        return await self._purge_all(versions, label=source_scope_id)

    async def run_all(self, *, limit: int = 1_000) -> RetiredVersionPurgeBatch:
        """Purge eligible versions of every tenant and scope.

        The periodic sweep: a scope that is never re-ingested never runs the
        post-cleanup purge, so its retired versions would otherwise outlive the TTL.
        Safe beside other workers -- each version is claimed (RETIRED -> PURGING)
        under a row lock before anything is deleted.
        """

        if self._retention_days is None:
            return RetiredVersionPurgeBatch()
        versions = await self._control.retention.purgeable_versions(
            retired_before=self._retired_before(self._retention_days),
            limit=limit,
        )
        return await self._purge_all(versions, label="*")

    def _retired_before(self, retention_days: int) -> datetime:
        return self._clock() - timedelta(days=retention_days)

    async def _purge_all(
        self,
        versions: tuple[PurgeableDocumentVersion, ...],
        *,
        label: str,
    ) -> RetiredVersionPurgeBatch:
        purged = skipped = failed = deleted = 0
        for version in versions:
            outcome, removed = await self._purge(version)
            deleted += removed
            purged += outcome == "purged"
            skipped += outcome == "skipped"
            failed += outcome == "failed"
        batch = RetiredVersionPurgeBatch(
            eligible=len(versions),
            purged=purged,
            skipped=skipped,
            failed=failed,
            deleted_objects=deleted,
        )
        if versions:
            logger.info(
                "Retired version purge completed source_scope_id=%s retention_days=%d "
                "eligible=%d purged=%d skipped=%d failed=%d deleted_objects=%d",
                label,
                self._retention_days,
                batch.eligible,
                batch.purged,
                batch.skipped,
                batch.failed,
                batch.deleted_objects,
            )
        return batch

    async def _purge(self, version: PurgeableDocumentVersion) -> tuple[str, int]:
        snapshot = version.snapshot
        document_id = str(snapshot.document_id)
        version_id = str(snapshot.document_version_id)
        # The same tenant context the artifacts were written with: keys are
        # tenant-relative and the store adds the tenant prefix itself.
        context = StorageOperationContext.system(version.tenant_id)
        removed = _Counter()
        try:
            if not await self._control.retention.begin_purge(version_id):
                logger.info(
                    "Retired version purge skipped document_id=%s document_version_id=%s "
                    "reason=no_longer_retired",
                    document_id,
                    version_id,
                )
                return "skipped", removed.value
            for bucket, key in sorted(
                self._recorded_objects(version) | await self._unshared_raw_objects(version)
            ):
                await self._delete(bucket, key, context, removed)
            for family in _VERSION_FAMILIES:
                await self._delete_stem(
                    f"{family}/{document_id}/{version_id}",
                    context,
                    removed,
                )
            if not await self._control.retention.mark_version_purged(version_id):
                logger.info(
                    "Retired version purge skipped document_id=%s document_version_id=%s "
                    "reason=finished_elsewhere",
                    document_id,
                    version_id,
                )
                return "skipped", removed.value
        except Exception as error:
            logger.error(
                "Retired version purge failed document_id=%s document_version_id=%s "
                "error_type=%s deleted_objects=%d",
                document_id,
                version_id,
                type(error).__name__,
                removed.value,
            )
            return "failed", removed.value
        logger.info(
            "Retired version purged document_id=%s document_version_id=%s deleted_objects=%d",
            document_id,
            version_id,
            removed.value,
        )
        return "purged", removed.value

    @staticmethod
    def _recorded_objects(version: PurgeableDocumentVersion) -> set[tuple[str, str]]:
        """Artifacts the version row records plus its deterministic keys."""

        snapshot = version.snapshot
        document_id = str(snapshot.document_id)
        version_id = str(snapshot.document_version_id)
        targets = {
            (reference.bucket, reference.key)
            for reference in (
                snapshot.canonical_artifact,
                snapshot.chunk_artifact,
                snapshot.chunk_index_artifact,
                snapshot.representation_artifact,
                snapshot.relation_artifact,
            )
            if reference is not None and not reference.key.startswith(_SHARED_PREFIXES)
        }
        # Deterministic keys a crash may have written without recording them on
        # the version row; deleting a missing key is a no-op.
        layout = IngestionArtifactLayout
        targets |= {
            (ARTIFACT_BUCKET, layout.canonical(document_id, version_id)),
            (ARTIFACT_BUCKET, layout.chunks(document_id, version_id)),
            (ARTIFACT_BUCKET, layout.chunk_index(document_id, version_id)),
            (ARTIFACT_BUCKET, layout.comments(document_id, version_id)),
            (ARTIFACT_BUCKET, layout.relations(document_id, version_id)),
        }
        return targets

    async def _unshared_raw_objects(
        self,
        version: PurgeableDocumentVersion,
    ) -> set[tuple[str, str]]:
        snapshot = version.snapshot
        raw = {
            (reference.bucket, reference.key)
            for reference in (snapshot.raw_artifact, snapshot.raw_metadata_artifact)
            if reference is not None
        }
        if not raw:
            return set()
        in_use = await self._control.retention.raw_keys_in_use(
            document_id=str(snapshot.document_id),
            excluding_version_id=str(snapshot.document_version_id),
        )
        return raw - in_use

    async def _delete_stem(
        self,
        stem: str,
        context: StorageOperationContext,
        removed: _Counter,
    ) -> None:
        """Delete every ``{stem}.ext`` and ``{stem}/...`` object in the artifact bucket.

        ``comments/{doc}/{version}`` is a stem, not a directory, so only keys
        continuing with ``.`` or ``/`` match: a version id that happens to be a
        prefix of another can never select the other version's objects.
        Listing has no cursor, so full pages are deleted and listed again.
        """

        for _ in range(_MAX_LIST_PASSES):
            page = await self._store.list(ARTIFACT_BUCKET, stem, limit=_LIST_PAGE, context=context)
            matching = [
                item.reference.key
                for item in page
                if item.reference.key[len(stem) :].startswith((".", "/"))
            ]
            for key in matching:
                await self._delete(ARTIFACT_BUCKET, key, context, removed)
            if len(page) < _LIST_PAGE or not matching:
                return

    async def _delete(
        self,
        bucket: str,
        key: str,
        context: StorageOperationContext,
        removed: _Counter,
    ) -> None:
        try:
            existed = await self._store.delete(bucket, key, context=context)
        except HarborStorageNotFoundError:
            # Already deleted by an earlier, interrupted purge.
            return
        removed.value += int(existed)


class _Counter:
    __slots__ = ("value",)

    def __init__(self) -> None:
        self.value = 0
