"""Retired-version purge claims: lease, ownership, resume order and cleanup guard."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from sqlalchemy import select, update

from harborrag_adapters.repositories.database.ingestion_control import (
    IngestionControlPlaneDatabase,
)
from harborrag_adapters.repositories.database.ingestion_control.schema import (
    DOCUMENT_VERSIONS,
)
from harborrag_core.ingestion import CleanupJobState, DocumentVersionState

from .ingestion_control_fixtures import (
    advance_to_verified,
    candidate,
    make_control_plane,
    source_identity,
)
from .test_retired_version_retention import (
    _complete_cleanup,
    _expire_lease,
    _purgeable_ids,
    _retired_pair,
)


async def _status(control: IngestionControlPlaneDatabase, version_id: str) -> str:
    async with control._client.sessions() as session:
        return str(
            await session.scalar(
                select(DOCUMENT_VERSIONS.c.status).where(
                    DOCUMENT_VERSIONS.c.document_version_id == version_id
                )
            )
        )


async def _retired_version(control: IngestionControlPlaneDatabase, item: str) -> str:
    """Publish two versions of one document and complete the first one's cleanup."""

    source = source_identity(item)
    first = candidate(f"{item}-v1", source=source)
    second = candidate(f"{item}-v2", source=source)
    for value in (first, second):
        await advance_to_verified(control, value)
        await control.publisher.publish(
            document_id=str(value.document_id),
            candidate_document_version_id=str(value.document_version_id),
        )
    retired = str(first.document_version_id)
    await _complete_cleanup(control, retired)
    return retired


@pytest.mark.asyncio
async def test_only_the_purge_holding_the_claim_may_renew_or_finish_it(tmp_path: Path) -> None:
    """R1c-07: a worker whose lease lapsed cannot mark a version another worker took over."""

    control = make_control_plane(tmp_path)
    async with control:
        _, retired, _ = await _retired_pair(control)
        await _complete_cleanup(control, retired)
        first = await control.retention.claim_purge(retired)
        assert first is not None
        assert first.resumed is False
        # Held: a second worker neither selects nor claims it.
        assert await control.retention.claim_purge(retired) is None

        await _expire_lease(control, retired)
        second = await control.retention.claim_purge(retired)
        assert second is not None
        assert second.resumed is True

        # The first worker lost the purge: it can neither renew nor finish it.
        assert await control.retention.renew_purge(first) is None
        assert await control.retention.mark_version_purged(
            retired, claimed_at=first.claimed_at
        ) is (False)
        assert await _status(control, retired) == DocumentVersionState.PURGING.value

        renewed = await control.retention.renew_purge(second)
        assert renewed is not None
        assert await control.retention.mark_version_purged(
            retired, claimed_at=second.claimed_at
        ) is (False)
        assert await control.retention.mark_version_purged(retired, claimed_at=renewed.claimed_at)
        assert await _status(control, retired) == DocumentVersionState.PURGED.value


@pytest.mark.asyncio
async def test_retention_off_still_resumes_interrupted_purges_only(tmp_path: Path) -> None:
    """R1c-06: with no TTL, a PURGING version is resumed, a RETIRED one is left alone."""

    control = make_control_plane(tmp_path)
    async with control:
        purging = await _retired_version(control, "page-purging")
        retired = await _retired_version(control, "page-retired")
        assert await control.retention.claim_purge(purging) is not None

        # Lease held: nothing to resume yet.
        assert await _purgeable_ids(control, retired_before=None) == set()
        await _expire_lease(control, purging)
        assert await _purgeable_ids(control, retired_before=None) == {purging}
        assert await control.retention.count_purgeable(retired_before=None) == 1
        # A RETIRED version waits for the TTL; once it applies both are eligible.
        assert await _purgeable_ids(control) == {purging, retired}
        assert (
            await control.retention.count_purgeable(retired_before=datetime.now(UTC) + timedelta(1))
            == 2
        )


@pytest.mark.asyncio
async def test_a_failed_purge_queues_behind_versions_not_yet_attempted(tmp_path: Path) -> None:
    """R4-08: a version whose purge failed is no longer selected before fresh ones."""

    control = make_control_plane(tmp_path)
    async with control:
        bad = await _retired_version(control, "page-bad")
        good = await _retired_version(control, "page-good")
        future = datetime.now(UTC) + timedelta(days=1)
        # Retired days ago, ``bad`` first: what a real TTL leaves behind.
        for version_id, age in ((bad, 3), (good, 2)):
            async with control._client.sessions.begin() as session:
                await session.execute(
                    update(DOCUMENT_VERSIONS)
                    .where(DOCUMENT_VERSIONS.c.document_version_id == version_id)
                    .values(updated_at=datetime.now(UTC) - timedelta(days=age))
                )
        (first,) = await control.retention.purgeable_versions(retired_before=future, limit=1)
        assert str(first.snapshot.document_version_id) == bad

        # The purge of ``bad`` fails: while its lease is held it is not selected at
        # all, and once the lease lapses it queues behind ``good``.
        assert await control.retention.claim_purge(bad) is not None
        assert await _purgeable_ids(control) == {good}
        await _expire_lease(control, bad)
        (head,) = await control.retention.purgeable_versions(retired_before=future, limit=1)
        assert str(head.snapshot.document_version_id) == good
        assert await _purgeable_ids(control) == {bad, good}


@pytest.mark.asyncio
async def test_a_purging_sibling_does_not_keep_a_shared_raw_key(tmp_path: Path) -> None:
    """R1a-08: two versions purged together must not each keep the raw key for the other."""

    control = make_control_plane(tmp_path)
    async with control:
        document_id, retired, active = await _retired_pair(control, shared_raw=True)
        async with control._client.sessions.begin() as session:
            await session.execute(
                update(DOCUMENT_VERSIONS)
                .where(DOCUMENT_VERSIONS.c.document_version_id == active)
                .values(status=DocumentVersionState.PURGING.value)
            )

        assert (
            await control.retention.raw_keys_in_use(
                document_id=document_id,
                excluding_version_id=retired,
            )
            == frozenset()
        )


@pytest.mark.asyncio
async def test_a_failure_recorded_during_a_purge_keeps_its_cleanup_completed(
    tmp_path: Path,
) -> None:
    """R1a-01: re-enqueuing cleanup must not hide a PURGING version from the purge."""

    control = make_control_plane(tmp_path)
    async with control:
        document_id, retired, _ = await _retired_pair(control)
        await _complete_cleanup(control, retired)
        assert await control.retention.begin_purge(retired)

        job = await control.reliability.enqueue_cleanup(
            document_id=document_id,
            document_version_id=retired,
        )

        assert job.status == CleanupJobState.COMPLETED
        stored = await control.reliability.cleanup_for_version(retired)
        assert stored is not None
        assert stored.status == CleanupJobState.COMPLETED
        await _expire_lease(control, retired)
        assert await _purgeable_ids(control) == {retired}
