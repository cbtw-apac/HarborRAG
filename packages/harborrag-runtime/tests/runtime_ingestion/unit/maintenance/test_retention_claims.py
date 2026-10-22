"""Retired-version purge: replay during a purge, leases, sweep draining and metrics."""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path

import pytest

from harborrag_adapters.repositories.object_store import MemoryObjectStore
from harborrag_core.contracts.errors import HarborUnavailableError
from harborrag_core.ingestion import (
    CleanupJobState,
    DocumentVersionState,
    SourceAdmissionDecision,
)
from harborrag_runtime.ingestion import DocumentReleaseService
from harborrag_runtime.ingestion.failure_classification import ingestion_failure_classifier
from harborrag_runtime.ingestion.maintenance.retention import RetiredVersionPurgeBatch

from ...fixtures.connectors import SourceConnector
from ...fixtures.release import (
    build_control_plane,
    build_dependencies,
    build_release_resources,
    release_request,
)
from .retention_fixtures import cleaned_retired_version, expire_purge_lease
from .test_retention import _FailingStore, _release_twice_and_clean, _service


async def _state(control: object, version_id: str) -> DocumentVersionState:
    snapshot = await control.document_versions.get_version(version_id)  # type: ignore[attr-defined]
    assert snapshot is not None
    return snapshot.state


@pytest.mark.asyncio
async def test_reverting_to_content_being_purged_retries_instead_of_failing(
    tmp_path: Path,
) -> None:
    """R1a-01: a re-ingest of a PURGING version waits transiently and keeps its cleanup."""

    control = build_control_plane(tmp_path)
    resources = build_release_resources(control)
    connector = SourceConnector()
    async with control, resources.store:
        released = await _release_twice_and_clean(resources, connector)
        claim = await control.retention.claim_purge(released.retired)
        assert claim is not None

        connector.labels = ["operations"]
        service = DocumentReleaseService(build_dependencies(resources))
        request = release_request(
            source_version="1",
            discovery_decision=SourceAdmissionDecision.METADATA_CHANGED,
        )
        with pytest.raises(HarborUnavailableError, match="being purged") as raised:
            await service.release(request, connector)

        assert ingestion_failure_classifier().classify("ParseAndNormalize", raised.value).retryable
        assert await _state(control, released.retired) == DocumentVersionState.PURGING
        job = await control.reliability.cleanup_for_version(released.retired)
        assert job is not None
        assert job.status == CleanupJobState.COMPLETED

        # Once the purge finishes, the retried release rebuilds the version.
        assert await control.retention.mark_version_purged(
            released.retired, claimed_at=claim.claimed_at
        )
        replay = await service.release(request, connector)
        assert str(replay.document_version_id) == released.retired
        assert replay.published is True


@pytest.mark.asyncio
async def test_retention_off_still_finishes_an_interrupted_purge(tmp_path: Path) -> None:
    """R1c-06: turning retention off must not leave a version PURGING forever."""

    control = build_control_plane(tmp_path)
    resources = build_release_resources(control)
    async with control, resources.store:
        released = await _release_twice_and_clean(resources, SourceConnector())
        store = _FailingStore(resources.store)
        failed = await _service(control, store).run_all()
        assert (failed.purged, failed.failed) == (0, 1)
        assert await _state(control, released.retired) == DocumentVersionState.PURGING

        await expire_purge_lease(control, released.retired)
        store.fail = False
        resumed = await _service(control, store, retention_days=None).run_all()

        assert (resumed.eligible, resumed.purged) == (1, 1)
        assert await _state(control, released.retired) == DocumentVersionState.PURGED


class _PoisonedStore:
    """Every delete under one document's keys is denied; others succeed."""

    def __init__(self, poisoned_document_id: str) -> None:
        self.poisoned = poisoned_document_id
        self.store = MemoryObjectStore()

    async def list(self, bucket: str, prefix: str, *, limit: int, context: object) -> list:
        return []

    async def delete(self, bucket: str, key: str, *, context: object) -> bool:
        if self.poisoned in key:
            raise PermissionError("access denied on prefix")
        return True


@pytest.mark.asyncio
async def test_a_failing_purge_is_not_repicked_before_other_versions(tmp_path: Path) -> None:
    """R4-08: a poisoned version no longer fills every sweep's batch."""

    control = build_control_plane(tmp_path)
    async with control:
        bad_document, bad = await cleaned_retired_version(control, "docs/bad.txt")
        _, good = await cleaned_retired_version(control, "docs/good.txt")
        service = _service(control, _PoisonedStore(bad_document))

        first = await service.run_all(limit=1)
        second = await service.run_all(limit=1)

        assert (first.failed, first.purged) == (1, 0)
        assert (second.failed, second.purged) == (0, 1)
        assert await _state(control, bad) == DocumentVersionState.PURGING
        assert await _state(control, good) == DocumentVersionState.PURGED


@pytest.mark.asyncio
async def test_one_sweep_drains_every_full_batch(tmp_path: Path) -> None:
    """R4-08: the sweep keeps claiming batches while they come back full."""

    control = build_control_plane(tmp_path)
    async with control:
        retired = [
            (await cleaned_retired_version(control, f"docs/{name}.txt"))[1]
            for name in ("a", "b", "c")
        ]

        batch = await _service(control, _PoisonedStore("nothing-matches")).run_all(limit=1)

        assert (batch.eligible, batch.purged, batch.failed, batch.backlog) == (3, 3, 0, 0)
        for version_id in retired:
            assert await _state(control, version_id) == DocumentVersionState.PURGED


class _TakenOverStore(_PoisonedStore):
    """Another worker takes the purge over while this one lists its objects."""

    def __init__(self, control: object, version_id: str) -> None:
        super().__init__("nothing-matches")
        self.control = control
        self.version_id = version_id
        self.taken = False

    async def list(self, bucket: str, prefix: str, *, limit: int, context: object) -> list:
        if not self.taken:
            self.taken = True
            await expire_purge_lease(self.control, self.version_id)  # type: ignore[arg-type]
            other = await self.control.retention.claim_purge(self.version_id)  # type: ignore[attr-defined]
            assert other is not None
        return []


@pytest.mark.asyncio
async def test_a_purge_that_lost_its_claim_stops_and_leaves_the_version_to_its_owner(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """R1c-07: only the worker holding the claim may finish a purge."""

    control = build_control_plane(tmp_path)
    async with control:
        _, retired = await cleaned_retired_version(control, "docs/shared.txt")
        store = _TakenOverStore(control, retired)

        with caplog.at_level(logging.INFO, logger="harborrag.runtime.ingestion.retention"):
            batch = await _service(control, store).run_all()

        assert (batch.purged, batch.skipped, batch.failed) == (0, 1, 0)
        assert "reason=claim_lost" in caplog.text
        assert await _state(control, retired) == DocumentVersionState.PURGING


@pytest.mark.asyncio
async def test_the_worker_sweeps_soon_after_start_and_records_metrics(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """R1d-07: the first sweep does not wait a full interval."""

    from harborrag_runtime.observability.model import IngestionTelemetry
    from harborrag_runtime.temporal import worker

    monkeypatch.setattr(worker, "FIRST_RETENTION_SWEEP_MAX_DELAY_SECONDS", 0.01)
    stop = asyncio.Event()

    class _Retention:
        async def run_all(self) -> RetiredVersionPurgeBatch:
            stop.set()
            return RetiredVersionPurgeBatch(
                eligible=3, purged=2, failed=1, deleted_objects=5, backlog=7
            )

    telemetry = IngestionTelemetry()
    runtime = type("Runtime", (), {"retention": _Retention(), "telemetry": telemetry})()
    settings = type("Settings", (), {"retired_version_purge_interval_hours": 24.0})()

    runner = worker._retention_runner(settings, runtime, stop)  # type: ignore[arg-type]
    assert runner is not None
    await asyncio.wait_for(runner, timeout=5)

    sample = telemetry.registry.get_sample_value
    assert sample("harborrag_retention_purge_versions_total", {"outcome": "purged"}) == 2
    assert sample("harborrag_retention_purge_versions_total", {"outcome": "failed"}) == 1
    assert sample("harborrag_retention_purge_deleted_objects_total") == 5
    assert sample("harborrag_retention_purge_backlog") == 7


@pytest.mark.asyncio
async def test_stopping_the_worker_cancels_a_running_sweep(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A sweep draining batches must not hold worker shutdown for its whole budget."""

    from harborrag_runtime.temporal import worker

    monkeypatch.setattr(worker, "FIRST_RETENTION_SWEEP_MAX_DELAY_SECONDS", 0.0)
    stop = asyncio.Event()
    cancelled = asyncio.Event()

    class _SlowRetention:
        async def run_all(self) -> RetiredVersionPurgeBatch:
            stop.set()
            try:
                await asyncio.sleep(60)
            except asyncio.CancelledError:
                cancelled.set()
                raise
            return RetiredVersionPurgeBatch()

    runtime = type("Runtime", (), {"retention": _SlowRetention()})()
    settings = type("Settings", (), {"retired_version_purge_interval_hours": 24.0})()

    runner = worker._retention_runner(settings, runtime, stop)  # type: ignore[arg-type]
    assert runner is not None
    await asyncio.wait_for(runner, timeout=5)

    assert cancelled.is_set()
