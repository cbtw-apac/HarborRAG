"""Reindex jobs keep their identity, move through their states and find stale documents."""

from __future__ import annotations

from pathlib import Path

import pytest
from sqlalchemy import update

from harborrag_adapters.repositories.database.ingestion_control.schema import REINDEX_JOBS
from harborrag_core.contracts import HarborConflictError, HarborNotFoundError
from harborrag_core.ingestion import ReindexJobState, ReindexProgress

from .ingestion_control_fixtures import (
    advance_to_verified,
    candidate,
    make_control_plane,
    sqlite_client,
)

pytestmark = [pytest.mark.unit, pytest.mark.asyncio]


async def test_submit_rejects_a_blank_job_or_fingerprint(tmp_path: Path) -> None:
    async with make_control_plane(tmp_path) as control:
        with pytest.raises(ValueError, match="non-empty"):
            await control.reindex.submit(reindex_job_id=" ", target_processing_fingerprint="fp")
        with pytest.raises(ValueError, match="non-empty"):
            await control.reindex.submit(reindex_job_id="job-1", target_processing_fingerprint="")


async def test_submit_returns_the_stored_job_and_refuses_a_changed_identity(
    tmp_path: Path,
) -> None:
    async with make_control_plane(tmp_path) as control:
        created = await control.reindex.submit(
            reindex_job_id="job-1", target_processing_fingerprint="fp-2", document_id="document-1"
        )
        again = await control.reindex.submit(
            reindex_job_id="job-1", target_processing_fingerprint="fp-2", document_id="document-1"
        )
        with pytest.raises(HarborConflictError, match="immutable"):
            await control.reindex.submit(
                reindex_job_id="job-1", target_processing_fingerprint="fp-3"
            )

        assert created.status == ReindexJobState.PENDING
        assert again == created
        assert await control.reindex.get("job-1") == created
        assert await control.reindex.get("job-missing") is None


async def test_start_runs_a_pending_job_once(tmp_path: Path) -> None:
    async with make_control_plane(tmp_path) as control:
        await control.reindex.submit(reindex_job_id="job-1", target_processing_fingerprint="fp-2")

        started = await control.reindex.start("job-1")
        restarted = await control.reindex.start("job-1")

        assert started.status == ReindexJobState.RUNNING
        assert restarted == started
        with pytest.raises(HarborNotFoundError):
            await control.reindex.start("job-missing")


async def test_start_refuses_a_cancelled_job(tmp_path: Path) -> None:
    async with make_control_plane(tmp_path) as control:
        await control.reindex.submit(reindex_job_id="job-1", target_processing_fingerprint="fp-2")
        client = sqlite_client(tmp_path / "control-plane.db")
        await client.connect()
        async with client.sessions.begin() as session:
            await session.execute(
                update(REINDEX_JOBS).values(status=ReindexJobState.CANCELLED.value)
            )
        await client.close()

        with pytest.raises(HarborConflictError, match="cancelled"):
            await control.reindex.start("job-1")


@pytest.mark.parametrize(
    ("failures", "expected"),
    [(0, ReindexJobState.COMPLETED), (1, ReindexJobState.FAILED)],
)
async def test_finish_records_the_progress_and_its_outcome(
    tmp_path: Path, failures: int, expected: ReindexJobState
) -> None:
    async with make_control_plane(tmp_path) as control:
        await control.reindex.submit(reindex_job_id="job-1", target_processing_fingerprint="fp-2")
        await control.reindex.start("job-1")
        progress = ReindexProgress(
            scanned_count=3,
            processed_count=2,
            published_count=2 - failures,
            failure_count=failures,
        )

        finished = await control.reindex.finish(
            "job-1", progress=progress, last_error_code="E_DEMO" if failures else None
        )

        assert finished.status == expected
        assert finished.processed_count == 2
        assert finished.failure_count == failures
        assert await control.reindex.get("job-1") == finished
        with pytest.raises(HarborNotFoundError):
            await control.reindex.finish("job-missing", progress=progress)


@pytest.mark.parametrize(
    ("limit", "tenant_id"),
    [(0, "DEFAULT"), (100_001, "DEFAULT"), (10, " ")],
)
async def test_stale_scan_rejects_a_bad_limit_or_tenant(
    tmp_path: Path, limit: int, tenant_id: str
) -> None:
    async with make_control_plane(tmp_path) as control:
        with pytest.raises(ValueError, match="reindex"):
            await control.reindex.stale_active_document_ids(
                tenant_id=tenant_id, target_processing_fingerprint="fp-2", limit=limit
            )


async def test_stale_scan_lists_active_documents_built_with_another_fingerprint(
    tmp_path: Path,
) -> None:
    async with make_control_plane(tmp_path) as control:
        version = candidate("release notes")
        await advance_to_verified(control, version)
        await control.publisher.publish(
            document_id=str(version.document_id),
            candidate_document_version_id=str(version.document_version_id),
        )
        document_id = str(version.document_id)
        current = version.fingerprints.processing_fingerprint

        stale = await control.reindex.stale_active_document_ids(
            tenant_id="DEFAULT", target_processing_fingerprint="fp-new"
        )
        one = await control.reindex.stale_active_document_ids(
            tenant_id="DEFAULT", target_processing_fingerprint="fp-new", document_id=document_id
        )
        other = await control.reindex.stale_active_document_ids(
            tenant_id="DEFAULT", target_processing_fingerprint="fp-new", document_id="document-x"
        )
        fresh = await control.reindex.stale_active_document_ids(
            tenant_id="DEFAULT", target_processing_fingerprint=current
        )

        assert stale == one == (document_id,)
        assert other == fresh == ()
