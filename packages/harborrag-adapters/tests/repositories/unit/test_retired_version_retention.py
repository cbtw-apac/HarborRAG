"""Retired document-version retention: eligibility and the PURGED transition."""

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
    DOCUMENTS,
)
from harborrag_core.ingestion import (
    ArtifactReference,
    DiscoveredSourceItem,
    DocumentVersionCandidate,
    DocumentVersionState,
    ProjectionManifest,
)

from .ingestion_control_fixtures import candidate, make_control_plane

_FUTURE = datetime.now(UTC) + timedelta(days=1)


def _ref(bucket: str, key: str) -> ArtifactReference:
    return ArtifactReference(
        bucket=bucket,
        key=key,
        sha256="a" * 64,
        byte_size=2,
        media_type="application/octet-stream",
    )


async def _verified(
    control: IngestionControlPlaneDatabase,
    value: DocumentVersionCandidate,
    *,
    raw_key: str,
    raw_metadata_key: str,
) -> str:
    """Advance a candidate to VERIFIED with artifact references on its row."""

    versions = control.document_versions
    version_id = str(value.document_version_id)
    document_id = str(value.document_id)
    await versions.create_candidate(value)
    await versions.transition(
        version_id,
        DocumentVersionState.RAW_CAPTURED,
        artifact_column="raw_artifact",
        artifact=_ref("harborrag-raw", raw_key),
    )
    await versions.transition(
        version_id,
        DocumentVersionState.RAW_CAPTURED,
        artifact_column="raw_metadata_artifact",
        artifact=_ref("harborrag-raw", raw_metadata_key),
    )
    await versions.transition(
        version_id,
        DocumentVersionState.CANONICAL_READY,
        artifact_column="canonical_artifact",
        artifact=_ref("harborrag-artifacts", f"canonical/{document_id}/{version_id}.json"),
    )
    await versions.transition(
        version_id,
        DocumentVersionState.CHUNKS_READY,
        artifact_column="chunk_artifact",
        artifact=_ref("harborrag-artifacts", f"chunks/{document_id}/{version_id}.jsonl"),
    )
    await versions.transition(version_id, DocumentVersionState.REPRESENTATIONS_READY)
    await versions.transition(version_id, DocumentVersionState.PROJECTIONS_STAGED)
    await versions.save_projection_manifest(
        ProjectionManifest(
            document_id=value.document_id,
            document_version_id=value.document_version_id,
            route_point_ids=("route-1",),
            evidence_point_ids=("evidence-1",),
            graph_node_keys=("document-node",),
            chunk_ids=("route-chunk", "evidence-chunk"),
        )
    )
    await versions.mark_verified(version_id)
    return version_id


async def _retired_pair(
    control: IngestionControlPlaneDatabase,
    *,
    shared_raw: bool = True,
) -> tuple[str, str, str]:
    """Publish two versions of one document; return (document, retired, active)."""

    first = candidate("first")
    second = candidate("second")
    document_id = str(first.document_id)
    retired = await _verified(
        control,
        first,
        raw_key="raw/confluence/doc/hash-1/source",
        raw_metadata_key="raw/confluence/doc/hash-1/metadata/m1.json",
    )
    await control.publisher.publish(document_id=document_id, candidate_document_version_id=retired)
    active = await _verified(
        control,
        second,
        raw_key=(
            "raw/confluence/doc/hash-1/source" if shared_raw else "raw/confluence/doc/hash-2/source"
        ),
        raw_metadata_key="raw/confluence/doc/hash-2/metadata/m2.json",
    )
    await control.publisher.publish(document_id=document_id, candidate_document_version_id=active)
    return document_id, retired, active


async def _complete_cleanup(control: IngestionControlPlaneDatabase, version_id: str) -> None:
    job = await control.reliability.cleanup_for_version(version_id)
    assert job is not None
    assert await control.reliability.claim_cleanup(job.cleanup_job_id)
    await control.reliability.complete_cleanup(job.cleanup_job_id)


async def _purgeable_ids(
    control: IngestionControlPlaneDatabase,
    *,
    retired_before: datetime = _FUTURE,
    tenant_id: str | None = None,
    source_scope_id: str | None = None,
) -> set[str]:
    versions = await control.retention.purgeable_versions(
        retired_before=retired_before,
        tenant_id=tenant_id,
        source_scope_id=source_scope_id,
    )
    return {str(version.snapshot.document_version_id) for version in versions}


@pytest.mark.asyncio
async def test_retired_version_is_purgeable_only_after_its_cleanup_completed(
    tmp_path: Path,
) -> None:
    control = make_control_plane(tmp_path)
    async with control:
        _, retired, _ = await _retired_pair(control)
        job = await control.reliability.cleanup_for_version(retired)
        assert job is not None

        # PENDING: cleanup still needs the manifest to find the vectors.
        assert await _purgeable_ids(control) == set()
        assert await control.reliability.claim_cleanup(job.cleanup_job_id)
        assert await _purgeable_ids(control) == set()  # RUNNING
        await control.reliability.fail_cleanup(job.cleanup_job_id, safe_error_code="boom")
        assert await _purgeable_ids(control) == set()  # FAILED
        await control.reliability.cancel_cleanup(job.cleanup_job_id, safe_reason_code="replayed")
        assert await _purgeable_ids(control) == set()  # CANCELLED

        await control.reliability.enqueue_cleanup(
            document_id=str(job.document_id),
            document_version_id=retired,
        )
        await _complete_cleanup(control, retired)

        assert await _purgeable_ids(control) == {retired}


@pytest.mark.asyncio
async def test_retention_boundary_is_inclusive_of_the_retirement_instant(
    tmp_path: Path,
) -> None:
    control = make_control_plane(tmp_path)
    async with control:
        _, retired, active = await _retired_pair(control)
        await _complete_cleanup(control, retired)
        (version,) = await control.retention.purgeable_versions(retired_before=_FUTURE)

        assert str(version.snapshot.document_version_id) == retired
        assert version.tenant_id == "DEFAULT"
        assert version.snapshot.state == DocumentVersionState.RETIRED
        assert await _purgeable_ids(control, retired_before=version.retired_at) == {retired}
        assert (
            await _purgeable_ids(
                control,
                retired_before=version.retired_at - timedelta(microseconds=1),
            )
            == set()
        )
        assert active not in await _purgeable_ids(control)


@pytest.mark.asyncio
async def test_purgeable_versions_are_filtered_by_tenant_and_source_scope(
    tmp_path: Path,
) -> None:
    control = make_control_plane(tmp_path)
    async with control:
        document_id, retired, _ = await _retired_pair(control)
        await _complete_cleanup(control, retired)

        assert await _purgeable_ids(control, tenant_id="DEFAULT") == {retired}
        assert await _purgeable_ids(control, tenant_id="OTHER") == set()
        # The scope filter follows source bindings, as projection cleanup does.
        assert await _purgeable_ids(control, source_scope_id="scope-engineering") == set()

        value = candidate("first")
        await control.source_scans.register_scope(
            source_scope_id="scope-engineering",
            connector_type="confluence",
            connection_id="wiki.example",
            configuration_fingerprint="config-v1",
        )
        scan_id = await control.source_scans.start("scope-engineering")
        await control.source_scans.record_seen(
            scan_id=scan_id,
            item=DiscoveredSourceItem(
                source_identity=value.source_identity,
                document_id=value.document_id,
                source_version="1",
                admission_change_key="admission-1",
            ),
        )

        assert await _purgeable_ids(control, source_scope_id="scope-engineering") == {retired}
        assert await _purgeable_ids(control, source_scope_id="scope-other") == set()
        assert document_id == str(value.document_id)


@pytest.mark.asyncio
async def test_the_active_version_is_never_purgeable(tmp_path: Path) -> None:
    control = make_control_plane(tmp_path)
    async with control:
        document_id, retired, _ = await _retired_pair(control)
        await _complete_cleanup(control, retired)
        # A corrupted pointer: the document still names the RETIRED row active.
        async with control._client.sessions.begin() as session:
            await session.execute(
                update(DOCUMENTS)
                .where(DOCUMENTS.c.document_id == document_id)
                .values(active_document_version_id=retired)
            )

        assert await _purgeable_ids(control) == set()
        assert await control.retention.begin_purge(retired) is False
        assert await control.retention.mark_version_purged(retired) is False
        snapshot = await control.document_versions.get_version(retired)
        assert snapshot is not None
        assert snapshot.state == DocumentVersionState.RETIRED


@pytest.mark.asyncio
async def test_mark_version_purged_drops_manifest_and_artifacts_but_keeps_the_row(
    tmp_path: Path,
) -> None:
    control = make_control_plane(tmp_path)
    async with control:
        _, retired, active = await _retired_pair(control)
        await _complete_cleanup(control, retired)

        # Never marked without first being claimed.
        assert await control.retention.mark_version_purged(retired) is False
        assert await control.retention.begin_purge(retired) is True
        assert await control.retention.mark_version_purged(retired) is True

        snapshot = await control.document_versions.get_version(retired)
        assert snapshot is not None
        assert snapshot.state == DocumentVersionState.PURGED
        assert (
            snapshot.raw_artifact,
            snapshot.raw_metadata_artifact,
            snapshot.canonical_artifact,
            snapshot.chunk_artifact,
        ) == (None, None, None, None)
        async with control._client.sessions() as session:
            nulls = (
                await session.execute(
                    select(DOCUMENT_VERSIONS.c.document_version_id).where(
                        DOCUMENT_VERSIONS.c.document_version_id == retired,
                        DOCUMENT_VERSIONS.c.raw_artifact.is_(None),
                        DOCUMENT_VERSIONS.c.canonical_artifact.is_(None),
                    )
                )
            ).scalar_one_or_none()
        assert nulls == retired  # SQL NULL, not a JSON null literal
        assert await control.reliability.projection_manifest(retired) is None
        assert await control.reliability.projection_manifest(active) is not None
        active_snapshot = await control.document_versions.get_version(active)
        assert active_snapshot is not None
        assert active_snapshot.state == DocumentVersionState.ACTIVE
        assert active_snapshot.canonical_artifact is not None

        # Idempotent: a re-run finds nothing and changes nothing.
        assert await _purgeable_ids(control) == set()
        assert await control.retention.mark_version_purged(retired) is False
        assert await control.retention.mark_version_purged("missing-version") is False


@pytest.mark.asyncio
async def test_raw_keys_in_use_counts_every_unpurged_sibling_version(
    tmp_path: Path,
) -> None:
    control = make_control_plane(tmp_path)
    async with control:
        document_id, retired, active = await _retired_pair(control, shared_raw=True)

        in_use = await control.retention.raw_keys_in_use(
            document_id=document_id,
            excluding_version_id=retired,
        )

        assert ("harborrag-raw", "raw/confluence/doc/hash-1/source") in in_use
        assert ("harborrag-raw", "raw/confluence/doc/hash-1/metadata/m1.json") not in in_use

        # A FAILED sibling may still replay from its raw capture, so it counts.
        async with control._client.sessions.begin() as session:
            await session.execute(
                update(DOCUMENT_VERSIONS)
                .where(DOCUMENT_VERSIONS.c.document_version_id == active)
                .values(status=DocumentVersionState.FAILED.value)
            )
        assert ("harborrag-raw", "raw/confluence/doc/hash-1/source") in (
            await control.retention.raw_keys_in_use(
                document_id=document_id,
                excluding_version_id=retired,
            )
        )

        # A PURGED sibling no longer needs anything.
        async with control._client.sessions.begin() as session:
            await session.execute(
                update(DOCUMENT_VERSIONS)
                .where(DOCUMENT_VERSIONS.c.document_version_id == active)
                .values(status=DocumentVersionState.PURGED.value)
            )
        assert (
            await control.retention.raw_keys_in_use(
                document_id=document_id,
                excluding_version_id=retired,
            )
            == frozenset()
        )


@pytest.mark.asyncio
async def test_a_purged_version_replays_from_pending_when_its_content_returns(
    tmp_path: Path,
) -> None:
    control = make_control_plane(tmp_path)
    async with control:
        _, retired, _ = await _retired_pair(control)
        await _complete_cleanup(control, retired)
        assert await control.retention.begin_purge(retired)
        assert await control.retention.mark_version_purged(retired)

        # Reverting to the old content derives the same deterministic version id.
        assert (
            await control.document_versions.create_candidate(candidate("first"))
            == DocumentVersionState.PURGED
        )
        assert (
            await control.document_versions.prepare_replay(retired) == DocumentVersionState.PENDING
        )


@pytest.mark.asyncio
async def test_a_version_being_purged_refuses_replay_transiently_and_resumes(
    tmp_path: Path,
) -> None:
    """The replay/purge race: content returning mid-purge must wait, not restore.

    Restoring a version whose objects are being deleted left it pointing at missing
    artifacts. A PURGING version raises a transient error, so the ingestion retries
    once the purge finishes and rebuilds the version from PENDING.
    """

    from harborrag_core.contracts.errors import HarborUnavailableError

    control = make_control_plane(tmp_path)
    async with control:
        _, retired, _ = await _retired_pair(control)
        await _complete_cleanup(control, retired)
        assert await control.retention.begin_purge(retired) is True

        with pytest.raises(HarborUnavailableError, match="being purged"):
            await control.document_versions.prepare_replay(retired)
        snapshot = await control.document_versions.get_version(retired)
        assert snapshot is not None
        assert snapshot.state == DocumentVersionState.PURGING

        # An interrupted purge is still eligible, and claiming it again resumes it.
        assert await _purgeable_ids(control) == {retired}
        assert await control.retention.begin_purge(retired) is True
        assert await control.retention.mark_version_purged(retired) is True
        assert await control.document_versions.prepare_replay(retired) == (
            DocumentVersionState.PENDING
        )
