"""Retention TTL purge of RETIRED document versions."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import timedelta
from pathlib import Path

import pytest

from harborrag_adapters.repositories.database.ingestion_control import (
    IngestionControlPlaneDatabase,
)
from harborrag_adapters.repositories.errors import (
    HarborStorageConnectionError,
    StorageErrorContext,
)
from harborrag_adapters.repositories.object_store import (
    ARTIFACT_BUCKET,
    RAW_BUCKET,
    MemoryObjectStore,
)
from harborrag_core.base import utc_now
from harborrag_core.ingestion import (
    DiscoveredSourceItem,
    DocumentVersionState,
    SourceAdmissionDecision,
)
from harborrag_core.schemas.object_store import PutObjectRequest
from harborrag_core.storage import StorageFamily, StorageOperationContext
from harborrag_runtime.ingestion import (
    DocumentReleaseService,
    ProjectionCleanupService,
    RetiredVersionPurgeService,
)
from harborrag_runtime.ingestion.maintenance.retention import RetiredVersionPurgeBatch

from ...fixtures.connectors import SourceConnector
from ...fixtures.release import (
    ReleaseResources,
    build_control_plane,
    build_dependencies,
    build_release_resources,
    release_request,
)

_CONTEXT = StorageOperationContext.system("default")


@dataclass(slots=True)
class _Released:
    document_id: str
    retired: str
    active: str


async def _keys(store: MemoryObjectStore, bucket: str) -> set[str]:
    return {
        item.reference.key for item in await store.list(bucket, "", limit=100_000, context=_CONTEXT)
    }


async def _bind_scope(control: IngestionControlPlaneDatabase, document_id: str) -> None:
    request = release_request(source_version="1")
    await control.source_scans.register_scope(
        source_scope_id="docs",
        connector_type="local",
        connection_id="local-docs",
        configuration_fingerprint="local-config-v1",
    )
    scan_id = await control.source_scans.start("docs")
    await control.source_scans.record_seen(
        scan_id=scan_id,
        item=DiscoveredSourceItem(
            source_identity=request.source_identity,
            document_id=document_id,
            source_version="1",
            admission_change_key="admission-1",
        ),
    )


async def _release_twice_and_clean(
    resources: ReleaseResources,
    connector: SourceConnector,
) -> _Released:
    """Publish a metadata change so the first version retires, then clean it."""

    dependencies = build_dependencies(resources)
    service = DocumentReleaseService(dependencies)
    await service.provision(tenant_id="default")
    first = await service.release(release_request(source_version="1"), connector)
    connector.labels = ["production"]
    second = await service.release(
        release_request(
            source_version="1",
            discovery_decision=SourceAdmissionDecision.METADATA_CHANGED,
        ),
        connector,
    )
    await _bind_scope(resources.control, str(first.document_id))
    cleanup = await ProjectionCleanupService(
        control=resources.control,
        vector_store=dependencies.vector_store,
        graph_store=resources.graph,
    ).run_scope(tenant_id="default", source_scope_id="docs")
    assert cleanup.completed == 1
    return _Released(
        document_id=str(first.document_id),
        retired=str(first.document_version_id),
        active=str(second.document_version_id),
    )


def _service(
    control: IngestionControlPlaneDatabase,
    store: object,
    *,
    retention_days: int | None = 0,
) -> RetiredVersionPurgeService:
    return RetiredVersionPurgeService(
        control=control,
        object_store=store,  # type: ignore[arg-type]
        retention_days=retention_days,
    )


@pytest.mark.asyncio
async def test_purge_deletes_version_owned_objects_and_keeps_shared_ones(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    control = build_control_plane(tmp_path)
    resources = build_release_resources(control)
    async with control, resources.store:
        released = await _release_twice_and_clean(resources, SourceConnector())
        retired_snapshot = await control.document_versions.get_version(released.retired)
        active_snapshot = await control.document_versions.get_version(released.active)
        assert retired_snapshot is not None
        assert active_snapshot is not None
        assert retired_snapshot.raw_artifact is not None
        assert retired_snapshot.raw_metadata_artifact is not None
        assert active_snapshot.raw_artifact is not None
        # Same bytes, same content-addressed raw object.
        assert retired_snapshot.raw_artifact.key == active_snapshot.raw_artifact.key
        # Objects no version records: a shared parse cache entry and a task plan.
        for bucket, key in (
            (ARTIFACT_BUCKET, f"parsed/{released.document_id}/parser-v1/hash.json"),
            (ARTIFACT_BUCKET, "source-plans/task-1/scan-1.json"),
            # A sibling id that merely extends the retired id must survive.
            (ARTIFACT_BUCKET, f"comments/{released.document_id}/{released.retired}x.json"),
        ):
            await resources.store.put(
                PutObjectRequest(bucket=bucket, key=key, body=b"{}"),
                context=_CONTEXT,
            )
        artifacts_before = await _keys(resources.store, ARTIFACT_BUCKET)
        raw_before = await _keys(resources.store, RAW_BUCKET)
        retired_owned = {
            key
            for key in artifacts_before
            if f"/{released.document_id}/{released.retired}." in key
            or f"/{released.document_id}/{released.retired}/" in key
        }
        assert {
            f"canonical/{released.document_id}/{released.retired}.json",
            f"chunks/{released.document_id}/{released.retired}.jsonl",
            f"chunks/{released.document_id}/{released.retired}.idx",
        } <= retired_owned
        assert any(key.startswith("representations/") for key in retired_owned)

        with caplog.at_level(logging.INFO, logger="harborrag.runtime.ingestion.retention"):
            batch = await _service(control, resources.store).run_scope(
                tenant_id="default",
                source_scope_id="docs",
            )

        assert (batch.eligible, batch.purged, batch.failed) == (1, 1, 0)
        artifacts_after = await _keys(resources.store, ARTIFACT_BUCKET)
        raw_after = await _keys(resources.store, RAW_BUCKET)
        assert artifacts_after == artifacts_before - retired_owned
        assert raw_after == raw_before - {retired_snapshot.raw_metadata_artifact.key}
        assert retired_snapshot.raw_artifact.key in raw_after
        assert f"parsed/{released.document_id}/parser-v1/hash.json" in artifacts_after
        assert "source-plans/task-1/scan-1.json" in artifacts_after
        assert batch.deleted_objects == len(retired_owned) + 1

        purged = await control.document_versions.get_version(released.retired)
        assert purged is not None
        assert purged.state == DocumentVersionState.PURGED
        assert purged.canonical_artifact is None
        assert purged.raw_artifact is None
        assert await control.reliability.projection_manifest(released.retired) is None
        active = await control.document_versions.active_snapshot(released.document_id)
        assert active is not None
        assert str(active.document_version_id) == released.active
        assert await control.reliability.projection_manifest(released.active) is not None
        assert "Retired version purged" in caplog.text

        # Idempotent: nothing is eligible on the next run.
        again = await _service(control, resources.store).run_scope(
            tenant_id="default",
            source_scope_id="docs",
        )
        assert (again.eligible, again.purged) == (0, 0)


@pytest.mark.asyncio
async def test_purge_respects_the_ttl_and_none_disables_it(tmp_path: Path) -> None:
    control = build_control_plane(tmp_path)
    resources = build_release_resources(control)
    async with control, resources.store:
        released = await _release_twice_and_clean(resources, SourceConnector())
        before = await _keys(resources.store, ARTIFACT_BUCKET)

        disabled = await _service(control, resources.store, retention_days=None).run_scope(
            tenant_id="default",
            source_scope_id="docs",
        )
        not_yet = await _service(control, resources.store, retention_days=30).run_scope(
            tenant_id="default",
            source_scope_id="docs",
        )
        elapsed = await RetiredVersionPurgeService(
            control=control,
            object_store=resources.store,
            retention_days=30,
            clock=lambda: utc_now() + timedelta(days=31),
        ).run_scope(tenant_id="default", source_scope_id="docs")

        assert disabled.eligible == not_yet.eligible == 0
        assert elapsed.purged == 1
        assert await _keys(resources.store, ARTIFACT_BUCKET) < before
        snapshot = await control.document_versions.get_version(released.retired)
        assert snapshot is not None
        assert snapshot.state == DocumentVersionState.PURGED


def test_negative_retention_is_rejected(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="must not be negative"):
        _service(build_control_plane(tmp_path), MemoryObjectStore(), retention_days=-1)


class _FailingStore:
    """Delegate to a memory store but fail deletes until told otherwise."""

    def __init__(self, store: MemoryObjectStore) -> None:
        self._store = store
        self.fail = True

    async def list(self, bucket: str, prefix: str, *, limit: int, context: object) -> list:
        return await self._store.list(bucket, prefix, limit=limit, context=context)  # type: ignore[arg-type]

    async def delete(self, bucket: str, key: str, *, context: object) -> bool:
        if self.fail and key.startswith("chunks/"):
            raise HarborStorageConnectionError(
                "object store unavailable",
                context=StorageErrorContext(
                    family=StorageFamily.OBJECT_STORE,
                    backend="memory",
                    instance_name="test",
                    operation="delete",
                ),
            )
        return await self._store.delete(bucket, key, context=context)  # type: ignore[arg-type]


@pytest.mark.asyncio
async def test_a_storage_error_leaves_the_version_purging_for_the_next_run(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    control = build_control_plane(tmp_path)
    resources = build_release_resources(control)
    async with control, resources.store:
        released = await _release_twice_and_clean(resources, SourceConnector())
        store = _FailingStore(resources.store)

        with caplog.at_level(logging.ERROR, logger="harborrag.runtime.ingestion.retention"):
            failed = await _service(control, store).run_scope(
                tenant_id="default",
                source_scope_id="docs",
            )
        snapshot = await control.document_versions.get_version(released.retired)
        assert (failed.purged, failed.failed) == (0, 1)
        assert "error_type=HarborStorageConnectionError" in caplog.text
        assert snapshot is not None
        # Some objects may already be gone, so the version stays claimed: replay keeps
        # waiting on it and the next run resumes the purge rather than restarting it.
        assert snapshot.state == DocumentVersionState.PURGING
        assert snapshot.canonical_artifact is not None
        assert await control.reliability.projection_manifest(released.retired) is not None

        store.fail = False
        retried = await _service(control, store).run_scope(
            tenant_id="default",
            source_scope_id="docs",
        )
        snapshot = await control.document_versions.get_version(released.retired)
        assert (retried.purged, retried.failed) == (1, 0)
        assert snapshot is not None
        assert snapshot.state == DocumentVersionState.PURGED
        assert not any(
            released.retired in key for key in await _keys(resources.store, ARTIFACT_BUCKET)
        )


@pytest.mark.asyncio
async def test_reverting_to_purged_content_rebuilds_and_republishes_the_version(
    tmp_path: Path,
) -> None:
    control = build_control_plane(tmp_path)
    resources = build_release_resources(control)
    connector = SourceConnector()
    async with control, resources.store:
        released = await _release_twice_and_clean(resources, connector)
        await _service(control, resources.store).run_scope(
            tenant_id="default",
            source_scope_id="docs",
        )

        connector.labels = ["operations"]
        replay = await DocumentReleaseService(build_dependencies(resources)).release(
            release_request(
                source_version="1",
                discovery_decision=SourceAdmissionDecision.METADATA_CHANGED,
            ),
            connector,
        )

        assert str(replay.document_version_id) == released.retired
        assert replay.published is True
        snapshot = await control.document_versions.get_version(released.retired)
        assert snapshot is not None
        assert snapshot.state == DocumentVersionState.ACTIVE
        assert snapshot.canonical_artifact is not None
        assert await control.reliability.projection_manifest(released.retired) is not None


@pytest.mark.asyncio
async def test_the_periodic_sweep_purges_scopes_that_are_never_reingested(
    tmp_path: Path,
) -> None:
    """``run_all`` needs no source run: it finds expired versions in every scope."""

    control = build_control_plane(tmp_path)
    resources = build_release_resources(control)
    async with control, resources.store:
        released = await _release_twice_and_clean(resources, SourceConnector())

        batch = await _service(control, resources.store).run_all()

        snapshot = await control.document_versions.get_version(released.retired)
        assert (batch.eligible, batch.purged, batch.failed) == (1, 1, 0)
        assert snapshot is not None
        assert snapshot.state == DocumentVersionState.PURGED
        assert await _service(control, resources.store, retention_days=None).run_all() == (
            RetiredVersionPurgeBatch()
        )


@pytest.mark.asyncio
async def test_the_worker_sweeps_on_its_interval_and_stops_with_the_worker() -> None:
    import asyncio

    from harborrag_runtime.temporal.worker import _retention_runner

    calls = 0
    stop = asyncio.Event()

    class _Retention:
        async def run_all(self) -> RetiredVersionPurgeBatch:
            nonlocal calls
            calls += 1
            if calls == 2:
                stop.set()
            if calls == 1:
                raise RuntimeError("storage briefly down")  # a failed sweep waits for the next
            return RetiredVersionPurgeBatch()

    runtime = type("Runtime", (), {"retention": _Retention()})()
    settings = type("Settings", (), {"retired_version_purge_interval_hours": 0.01 / 3600})()

    runner = _retention_runner(settings, runtime, stop)  # type: ignore[arg-type]
    assert runner is not None
    await asyncio.wait_for(runner, timeout=5)

    assert calls == 2
    disabled = type("Settings", (), {"retired_version_purge_interval_hours": 0})()
    assert _retention_runner(disabled, runtime, stop) is None  # type: ignore[arg-type]
