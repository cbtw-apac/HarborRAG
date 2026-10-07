"""A topology claim must never wait on a document lock while holding the config lock.

Publication locks a document, then the tenant indexing config. ``claim`` holds the
config after its first candidate; waiting on a later candidate's document then closes
a deadlock cycle with a publisher. SQLite renders no row locks, so the probe's SQL is
checked against the Postgres dialect and the skip path is driven through a fake probe.
"""

from pathlib import Path

import pytest
from sqlalchemy.dialects import postgresql

from harborrag_adapters.repositories.database.ingestion_control import (
    IngestionControlPlaneDatabase,
)
from harborrag_adapters.repositories.database.ingestion_control.topology import guards, jobs
from harborrag_core.topology import TopologyJob
from harborrag_core.topology.config import TenantIndexingConfig

from .ingestion_control_fixtures import (
    advance_to_verified,
    candidate,
    make_control_plane,
    publish_over_active,
    source_identity,
)
from .topology_fixtures import permit, policy


async def _published(control: IngestionControlPlaneDatabase, source_item_id: str) -> str:
    version = candidate(source_item_id, source=source_identity(source_item_id))
    await permit(control, str(version.document_id))
    await advance_to_verified(control, version)
    await publish_over_active(control, str(version.document_id), str(version.document_version_id))
    return str(version.document_id)


async def _enable(control: IngestionControlPlaneDatabase) -> None:
    await control.topology.configure_policy(policy())
    await control.topology.configure_indexing(
        TenantIndexingConfig(tenant_id="DEFAULT", enabled=True)
    )


@pytest.mark.asyncio
async def test_claim_probes_documents_with_skip_locked(tmp_path: Path) -> None:
    async with make_control_plane(tmp_path) as control:
        await _enable(control)
        await _published(control, "page-1")
        (job,) = await control.topology.list_jobs("DEFAULT")

    sql = str(guards.document_lock_probe(job).compile(dialect=postgresql.dialect()))

    assert sql.rstrip().endswith("FOR UPDATE SKIP LOCKED")


@pytest.mark.asyncio
async def test_claim_skips_a_busy_document_without_superseding_its_job(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async with make_control_plane(tmp_path) as control:
        await _enable(control)
        busy = await _published(control, "page-1")
        free = await _published(control, "page-2")
        real = guards.try_lock_document

        async def contended(session, job: TopologyJob) -> bool:  # type: ignore[no-untyped-def]
            # A publisher holds this document and is waiting for the indexing config.
            return False if job.document_id == busy else await real(session, job)

        monkeypatch.setattr(jobs, "try_lock_document", contended)

        claimed = await control.topology.claim("DEFAULT")

        assert claimed is not None
        assert claimed.document_id == free
        states = {job.document_id: job.state for job in await control.topology.list_jobs("DEFAULT")}
        assert states[busy] == "pending"


@pytest.mark.asyncio
async def test_targeted_claim_waits_for_its_document_instead_of_skipping(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async with make_control_plane(tmp_path) as control:
        await _enable(control)
        await _published(control, "page-1")
        (job,) = await control.topology.list_jobs("DEFAULT")

        async def busy(session, job: TopologyJob) -> bool:  # type: ignore[no-untyped-def]
            return False

        monkeypatch.setattr(jobs, "try_lock_document", busy)

        claimed = await control.topology.claim("DEFAULT", job_id=job.job_id)

        assert claimed is not None
        assert claimed.job_id == job.job_id
        assert claimed.state == "running"
