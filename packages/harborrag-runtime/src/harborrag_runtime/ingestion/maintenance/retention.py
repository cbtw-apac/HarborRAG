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
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta

from harborrag_adapters.repositories.database import IngestionControlPlaneDatabase
from harborrag_adapters.repositories.database.ingestion_control.retention import PurgeClaim
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
_SWEEP_BATCH = 1_000
# One periodic sweep keeps claiming batches while they come back full, for at
# most this long, so a backlog drains in one sweep rather than 1000 a day.
SWEEP_TIME_BUDGET_SECONDS = 600.0


@dataclass(frozen=True, slots=True)
class RetiredVersionPurgeBatch:
    eligible: int = 0
    purged: int = 0
    skipped: int = 0
    failed: int = 0
    deleted_objects: int = 0
    # Versions still eligible when a sweep stopped; only ``run_all`` counts them.
    backlog: int = 0

    def __add__(self, other: RetiredVersionPurgeBatch) -> RetiredVersionPurgeBatch:
        return RetiredVersionPurgeBatch(
            eligible=self.eligible + other.eligible,
            purged=self.purged + other.purged,
            skipped=self.skipped + other.skipped,
            failed=self.failed + other.failed,
            deleted_objects=self.deleted_objects + other.deleted_objects,
            backlog=other.backlog,
        )


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
        versions = await self._control.retention.purgeable_versions(
            tenant_id=tenant_id,
            source_scope_id=source_scope_id,
            retired_before=self._retired_before(),
            limit=limit,
        )
        return await self._purge_all(versions, label=source_scope_id)

    async def run_all(
        self,
        *,
        limit: int = _SWEEP_BATCH,
        time_budget_seconds: float = SWEEP_TIME_BUDGET_SECONDS,
    ) -> RetiredVersionPurgeBatch:
        """Purge eligible versions of every tenant and scope.

        The periodic sweep: a scope that is never re-ingested never runs the
        post-cleanup purge, so its retired versions would otherwise outlive the TTL.
        Batches repeat while they come back full and still purge something, within
        ``time_budget_seconds``. Safe beside other workers -- each version is
        claimed (RETIRED -> PURGING) under a row lock and a lease before anything
        is deleted. With retention off only interrupted purges are resumed.
        """

        deadline = time.monotonic() + time_budget_seconds
        total = RetiredVersionPurgeBatch()
        while True:
            versions = await self._control.retention.purgeable_versions(
                retired_before=self._retired_before(),
                limit=limit,
            )
            batch = await self._purge_all(versions, label="*")
            total += batch
            if batch.eligible < limit or batch.purged == 0 or time.monotonic() >= deadline:
                break
        backlog = 0
        if total.eligible:
            backlog = await self._control.retention.count_purgeable(
                retired_before=self._retired_before()
            )
        return total + RetiredVersionPurgeBatch(backlog=backlog)

    def _retired_before(self) -> datetime | None:
        """The TTL cutoff; None (retention off) still resumes interrupted purges."""

        if self._retention_days is None:
            return None
        return self._clock() - timedelta(days=self._retention_days)

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
                "Retired version purge completed source_scope_id=%s retention_days=%s "
                "eligible=%d purged=%d skipped=%d failed=%d deleted_objects=%d",
                label,
                self._retention_days if self._retention_days is not None else "off",
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
        run: _PurgeRun | None = None
        try:
            claim = await self._control.retention.claim_purge(version_id)
            if claim is None:
                _log_skip(document_id, version_id, "no_longer_retired_or_held")
                return "skipped", 0
            # The same tenant context the artifacts were written with: keys are
            # tenant-relative and the store adds the tenant prefix itself.
            run = _PurgeRun(claim, StorageOperationContext.system(version.tenant_id))
            await self._delete_owned(version, run)
            if not await self._control.retention.mark_version_purged(
                version_id, claimed_at=run.claim.claimed_at
            ):
                _log_skip(document_id, version_id, "finished_elsewhere")
                return "skipped", run.removed
        except _ClaimLost:
            _log_skip(document_id, version_id, "claim_lost")
            return "skipped", run.removed if run is not None else 0
        except Exception as error:
            removed = run.removed if run is not None else 0
            logger.error(
                "Retired version purge failed document_id=%s document_version_id=%s "
                "error_type=%s deleted_objects=%d",
                document_id,
                version_id,
                type(error).__name__,
                removed,
                exc_info=True,
            )
            # The version stays PURGING under its lease, which is the retry backoff:
            # a failed delete cannot prove the object survived, so replay must not
            # restore the version until a purge completes.
            return "failed", removed
        logger.info(
            "Retired version purged document_id=%s document_version_id=%s deleted_objects=%d",
            document_id,
            version_id,
            run.removed,
        )
        return "purged", run.removed

    async def _delete_owned(self, version: PurgeableDocumentVersion, run: _PurgeRun) -> None:
        snapshot = version.snapshot
        document_id = str(snapshot.document_id)
        version_id = str(snapshot.document_version_id)
        for bucket, key in sorted(self._recorded_objects(version)):
            await self._delete(bucket, key, run)
        for family in _VERSION_FAMILIES:
            await self._delete_stem(f"{family}/{document_id}/{version_id}", run)
        # Shared raw objects last, right after checking who still references them,
        # so a version recording the same raw key meanwhile keeps it.
        await self._renew(run)
        for bucket, key in sorted(await self._unshared_raw_objects(version)):
            await self._delete(bucket, key, run)

    async def _renew(self, run: _PurgeRun) -> None:
        renewed = await self._control.retention.renew_purge(run.claim)
        if renewed is None:
            raise _ClaimLost
        run.claim = renewed

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

    async def _delete_stem(self, stem: str, run: _PurgeRun) -> None:
        """Delete every ``{stem}.ext`` and ``{stem}/...`` object in the artifact bucket.

        ``comments/{doc}/{version}`` is a stem, not a directory, so only keys
        continuing with ``.`` or ``/`` match: a version id that happens to be a
        prefix of another can never select the other version's objects.
        Listing has no cursor, so full pages are deleted and listed again.
        """

        for _ in range(_MAX_LIST_PASSES):
            await self._renew(run)
            page = await self._store.list(
                ARTIFACT_BUCKET, stem, limit=_LIST_PAGE, context=run.context
            )
            matching = [
                item.reference.key
                for item in page
                if item.reference.key[len(stem) :].startswith((".", "/"))
            ]
            for key in matching:
                await self._delete(ARTIFACT_BUCKET, key, run)
            if len(page) < _LIST_PAGE or not matching:
                return

    async def _delete(self, bucket: str, key: str, run: _PurgeRun) -> None:
        try:
            existed = await self._store.delete(bucket, key, context=run.context)
        except HarborStorageNotFoundError:
            # Already deleted by an earlier, interrupted purge.
            return
        run.removed += int(existed)


class _ClaimLost(Exception):
    """Another worker took the purge over after this one's lease lapsed."""


@dataclass(slots=True)
class _PurgeRun:
    claim: PurgeClaim
    context: StorageOperationContext
    removed: int = 0


def _log_skip(document_id: str, version_id: str, reason: str) -> None:
    logger.info(
        "Retired version purge skipped document_id=%s document_version_id=%s reason=%s",
        document_id,
        version_id,
        reason,
    )
