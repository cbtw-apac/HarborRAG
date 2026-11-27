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
from harborrag_adapters.repositories.object_store import (
    ARTIFACT_BUCKET,
    IngestionArtifactLayout,
)
from harborrag_core.base import utc_now
from harborrag_core.contracts.errors import StorageNotFoundError
from harborrag_core.ingestion import PurgeableDocumentVersion
from harborrag_core.observability.log_events import log_event, log_failure
from harborrag_core.ports.storage import ObjectStorePort
from harborrag_core.storage import StorageOperationContext

logger = logging.getLogger("harborrag.runtime.ingestion.retention")

_VERSION_FAMILIES = (
    "canonical",
    "chunks",
    "comments",
    "relations",
    "projections",
    "tables",
    "representations",
)
"""Object families whose keys are addressed by (document_id, document_version_id). ``parsed/`` is a
content-addressed cache shared across versions and ``source-plans/`` belongs to tasks, so neither
is ever touched here.
"""
_SHARED_PREFIXES = ("parsed/", "source-plans/")
_LIST_PAGE = 1_000
_MAX_LIST_PASSES = 100
_SWEEP_BATCH = 1_000
_SCOPE_BATCH = 100
SWEEP_TIME_BUDGET_SECONDS = 600.0
"""One periodic sweep keeps claiming batches while they come back full, for at most this long, so a
backlog drains in one sweep rather than 1000 a day.
"""


@dataclass(frozen=True, slots=True)
class RetiredVersionPurgeBatch:
    """Counts from one purge batch of retired versions.

    Attributes:
        eligible: Purgeable versions the batch found.
        purged: Versions purged.
        skipped: Versions another worker or a newer state took over.
        failed: Versions whose purge failed; they stay PURGING for a later run.
        deleted_objects: Objects deleted from the object store.
        backlog: Versions still eligible when a sweep stopped; only ``run_all`` counts them.
    """

    eligible: int = 0
    purged: int = 0
    skipped: int = 0
    failed: int = 0
    deleted_objects: int = 0
    backlog: int = 0

    def __add__(self, other: RetiredVersionPurgeBatch) -> RetiredVersionPurgeBatch:
        """Sum two batches' counts, keeping the later batch's backlog."""
        return RetiredVersionPurgeBatch(
            eligible=self.eligible + other.eligible,
            purged=self.purged + other.purged,
            skipped=self.skipped + other.skipped,
            failed=self.failed + other.failed,
            deleted_objects=self.deleted_objects + other.deleted_objects,
            backlog=other.backlog,
        )


class RetiredVersionPurgeService:
    """Delete the artifacts of retired versions whose retention TTL elapsed.

    Args:
        control: Control-plane database that claims and marks the versions.
        object_store: Object store holding the versions' artifacts.
        retention_days: Days a version stays retired before its purge; ``None`` turns
            retention off, which only resumes interrupted purges. Must not be negative.
        clock: Current time, replaceable in tests.
    """

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
        limit: int = _SCOPE_BATCH,
    ) -> RetiredVersionPurgeBatch:
        """Purge eligible retired versions of one source scope, up to ``limit``."""
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

        Args:
            limit: Purgeable versions fetched per batch.
            time_budget_seconds: Time after which no further batch starts.

        Returns:
            The summed counts of every batch, with the backlog left when the sweep stopped.
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
        """Return the TTL cutoff; ``None`` (retention off) still resumes interrupted purges."""
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
            log_event(
                logger,
                "ingestion.version_purge_batch_completed",
                source_scope_id=label,
                retention_seconds=(
                    None
                    if self._retention_days is None
                    else int(timedelta(days=self._retention_days).total_seconds())
                ),
                eligible_count=batch.eligible,
                purged_count=batch.purged,
                skipped_count=batch.skipped,
                failed_count=batch.failed,
                deleted_objects_count=batch.deleted_objects,
            )
        return batch

    async def _purge(self, version: PurgeableDocumentVersion) -> tuple[str, int]:
        """Purge one retired version and return its outcome with the objects removed.

        Artifacts are deleted with the tenant context they were written with, because keys are
        tenant-relative and the store adds the tenant prefix itself. The version stays PURGING under
        its lease, which is the retry backoff: a failed delete cannot prove the object survived, so
        replay must not restore the version until a purge completes.
        """
        snapshot = version.snapshot
        document_id = str(snapshot.document_id)
        version_id = str(snapshot.document_version_id)
        run: _PurgeRun | None = None
        try:
            claim = await self._control.retention.claim_purge(version_id)
            if claim is None:
                _log_skip(document_id, version_id, "no_longer_retired_or_held")
                return "skipped", 0
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
        except Exception as error:  # noqa: BLE001 - one version must not stop the sweep
            removed = run.removed if run is not None else 0
            log_failure(
                logger,
                "ingestion.version_purge_failed",
                error,
                document_id=document_id,
                document_version_id=version_id,
                deleted_objects_count=removed,
            )
            return "failed", removed
        log_event(
            logger,
            "ingestion.version_purged",
            document_id=document_id,
            document_version_id=version_id,
            deleted_objects_count=run.removed,
        )
        return "purged", run.removed

    async def _delete_owned(self, version: PurgeableDocumentVersion, run: _PurgeRun) -> None:
        """Delete the objects a version owns, then its unshared raw objects.

        Shared raw objects go last, right after checking who still references them, so a version
        recording the same raw key meanwhile keeps it.
        """
        snapshot = version.snapshot
        document_id = str(snapshot.document_id)
        version_id = str(snapshot.document_version_id)
        for bucket, key in sorted(self._recorded_objects(version)):
            await self._delete(bucket, key, run)
        for family in _VERSION_FAMILIES:
            await self._delete_stem(f"{family}/{document_id}/{version_id}", run)
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
        """Return the artifacts the version row records plus its deterministic keys.

        Deterministic keys a crash may have written without recording them on the version row;
        deleting a missing key is a no-op.
        """
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
            )
            if reference is not None and not reference.key.startswith(_SHARED_PREFIXES)
        }
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
        """Delete one object and count it when it existed.

        A missing object was already deleted by an earlier, interrupted purge.
        """
        try:
            existed = await self._store.delete(bucket, key, context=run.context)
        except StorageNotFoundError:
            return
        run.removed += int(existed)


class _ClaimLost(Exception):
    """Another worker took the purge over after this one's lease lapsed."""


@dataclass(slots=True)
class _PurgeRun:
    """One claimed purge in progress.

    Attributes:
        claim: The purge claim, renewed as the purge goes on.
        context: Storage context of the version's tenant.
        removed: Objects deleted so far.
    """

    claim: PurgeClaim
    context: StorageOperationContext
    removed: int = 0


def _log_skip(document_id: str, version_id: str, reason: str) -> None:
    log_event(
        logger,
        "ingestion.version_purge_skipped",
        document_id=document_id,
        document_version_id=version_id,
        reason=reason,
    )
