"""A replayed failed version never publishes over a newer active version."""

from __future__ import annotations

from pathlib import Path

import pytest

from harborrag_adapters.repositories.database.ingestion_control import (
    IngestionControlPlaneDatabase,
)
from harborrag_core.ingestion import DocumentVersionState, SourceAdmissionDecision
from harborrag_runtime.ingestion import DocumentReleaseDependencies, DocumentReleaseService

from ...fixtures.connectors import SourceConnector
from ...fixtures.release import (
    build_control_plane,
    build_dependencies,
    build_release_resources,
    release_request,
)


async def _failed_version_id(control: IngestionControlPlaneDatabase, *, published: set[str]) -> str:
    jobs = await control.reliability.pending_cleanup_jobs()
    return next(
        str(job.document_version_id)
        for job in jobs
        if str(job.document_version_id) not in published
    )


def _fail_canonical_once(
    dependencies: DocumentReleaseDependencies, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Leave the version with raw recorded and no canonical, so replay re-parses."""

    put = dependencies.canonical_artifacts.put
    calls = []

    async def failing_once(*args, **kwargs):  # type: ignore[no-untyped-def]
        calls.append(1)
        if len(calls) == 1:
            raise ConnectionError("object store unavailable")
        return await put(*args, **kwargs)

    monkeypatch.setattr(dependencies.canonical_artifacts, "put", failing_once)


@pytest.mark.asyncio
async def test_canonical_replay_of_a_version_overtaken_by_a_newer_publish_is_skipped(
    tmp_path: Path,
) -> None:
    control = build_control_plane(tmp_path)
    resources = build_release_resources(control)
    connector = SourceConnector()
    async with control, resources.store:
        service = DocumentReleaseService(build_dependencies(resources))
        await service.provision(tenant_id="default")
        first = await service.release(release_request(source_version="1"), connector)
        connector.body = "The worker timeout is 45 seconds."
        resources.graph.fail_writes = True
        with pytest.raises(ConnectionError, match="graph unavailable"):
            await service.release(release_request(source_version="2"), connector)
        assert first.document_version_id is not None
        failed_id = await _failed_version_id(control, published={first.document_version_id})
        resources.graph.fail_writes = False
        connector.body = "The worker timeout is 60 seconds."
        newer = await service.release(release_request(source_version="3"), connector)
        assert newer.published is True

        replayed = await service.replay(release_request(source_version="2"), failed_id)

        active = await control.document_versions.active_snapshot(first.document_id)
        assert replayed.published is False
        assert replayed.decision == SourceAdmissionDecision.UNCHANGED
        assert active is not None
        assert str(active.document_version_id) == newer.document_version_id
        left = await control.document_versions.get_version(failed_id)
        assert left is not None and left.state == DocumentVersionState.FAILED


@pytest.mark.asyncio
async def test_raw_replay_of_a_version_overtaken_by_a_newer_publish_is_skipped(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    control = build_control_plane(tmp_path)
    resources = build_release_resources(control)
    connector = SourceConnector()
    async with control, resources.store:
        dependencies = build_dependencies(resources)
        service = DocumentReleaseService(dependencies)
        await service.provision(tenant_id="default")
        first = await service.release(release_request(source_version="1"), connector)
        connector.body = "The worker timeout is 45 seconds."
        _fail_canonical_once(dependencies, monkeypatch)
        with pytest.raises(ConnectionError, match="object store unavailable"):
            await service.release(release_request(source_version="2"), connector)
        assert first.document_version_id is not None
        failed_id = await _failed_version_id(control, published={first.document_version_id})
        failed = await control.document_versions.get_version(failed_id)
        assert failed is not None
        assert failed.raw_artifact is not None and failed.canonical_artifact is None
        connector.body = "The worker timeout is 60 seconds."
        newer = await service.release(release_request(source_version="3"), connector)
        assert newer.published is True

        replayed = await service.replay(release_request(source_version="2"), failed_id)

        active = await control.document_versions.active_snapshot(first.document_id)
        assert replayed.published is False
        assert active is not None
        assert str(active.document_version_id) == newer.document_version_id


@pytest.mark.asyncio
async def test_raw_replay_with_no_newer_publish_still_publishes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    control = build_control_plane(tmp_path)
    resources = build_release_resources(control)
    connector = SourceConnector()
    async with control, resources.store:
        dependencies = build_dependencies(resources)
        service = DocumentReleaseService(dependencies)
        await service.provision(tenant_id="default")
        first = await service.release(release_request(source_version="1"), connector)
        connector.body = "The worker timeout is 45 seconds."
        _fail_canonical_once(dependencies, monkeypatch)
        with pytest.raises(ConnectionError, match="object store unavailable"):
            await service.release(release_request(source_version="2"), connector)
        assert first.document_version_id is not None
        failed_id = await _failed_version_id(control, published={first.document_version_id})

        replayed = await service.replay(release_request(source_version="2"), failed_id)

        active = await control.document_versions.active_snapshot(first.document_id)
        assert replayed.published is True
        assert replayed.document_version_id == failed_id
        assert active is not None
        assert str(active.document_version_id) == failed_id
