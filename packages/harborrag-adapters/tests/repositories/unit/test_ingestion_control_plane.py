from __future__ import annotations

from pathlib import Path

import pytest
from sqlalchemy import event
from sqlalchemy.dialects import postgresql

from harborrag_adapters.repositories.database.ingestion_control import document_versions
from harborrag_core.contracts import HarborConflictError
from harborrag_core.ingestion import (
    DocumentVersionState,
    ProjectionManifest,
    PublicationSupersededError,
)

from .ingestion_control_fixtures import (
    advance_to_verified,
    candidate,
    make_control_plane,
)


@pytest.mark.asyncio
async def test_publication_is_atomic_idempotent_and_retires_previous_version(
    tmp_path: Path,
) -> None:
    control_plane = make_control_plane(tmp_path)
    async with control_plane:
        first = candidate("version one")
        await advance_to_verified(control_plane, first)
        published = await control_plane.publisher.publish(
            document_id=str(first.document_id),
            candidate_document_version_id=str(first.document_version_id),
            expected_active_document_version_id=None,
        )
        replayed = await control_plane.publisher.publish(
            document_id=str(first.document_id),
            candidate_document_version_id=str(first.document_version_id),
            expected_active_document_version_id=None,
        )

        assert published.active_document_version_id == first.document_version_id
        assert replayed.replayed is True
        active = await control_plane.document_versions.active_versions([str(first.document_id)])
        assert active[str(first.document_id)].document_version_id == first.document_version_id

        second = candidate("version two")
        await advance_to_verified(control_plane, second)
        replaced = await control_plane.publisher.publish(
            document_id=str(second.document_id),
            candidate_document_version_id=str(second.document_version_id),
            expected_active_document_version_id=str(first.document_version_id),
        )

        assert replaced.retired_document_version_id == first.document_version_id
        assert replaced.cleanup_job_created is True
        active = await control_plane.document_versions.active_versions([str(second.document_id)])
        assert active[str(second.document_id)].document_version_id == second.document_version_id


@pytest.mark.asyncio
async def test_unverified_candidate_never_replaces_active_version(
    tmp_path: Path,
) -> None:
    control_plane = make_control_plane(tmp_path)
    async with control_plane:
        active = candidate("published")
        await advance_to_verified(control_plane, active)
        await control_plane.publisher.publish(
            document_id=str(active.document_id),
            candidate_document_version_id=str(active.document_version_id),
            expected_active_document_version_id=None,
        )
        unverified = candidate("not verified")
        await control_plane.document_versions.create_candidate(unverified)

        with pytest.raises(HarborConflictError, match="VERIFIED"):
            await control_plane.publisher.publish(
                document_id=str(unverified.document_id),
                candidate_document_version_id=str(unverified.document_version_id),
                expected_active_document_version_id=str(active.document_version_id),
            )

        current = await control_plane.document_versions.active_versions([str(active.document_id)])
        assert current[str(active.document_id)].document_version_id == active.document_version_id


@pytest.mark.asyncio
async def test_publication_refuses_a_candidate_whose_base_was_superseded(
    tmp_path: Path,
) -> None:
    # A reindex reads v1, rebuilds it, and publishes after a source release
    # activated v2: last-writer-wins used to put the stale rebuild back.
    control_plane = make_control_plane(tmp_path)
    async with control_plane:
        first = candidate("version one")
        await advance_to_verified(control_plane, first)
        await control_plane.publisher.publish(
            document_id=str(first.document_id),
            candidate_document_version_id=str(first.document_version_id),
            expected_active_document_version_id=None,
        )
        second = candidate("version two")
        await advance_to_verified(control_plane, second)
        await control_plane.publisher.publish(
            document_id=str(second.document_id),
            candidate_document_version_id=str(second.document_version_id),
            expected_active_document_version_id=str(first.document_version_id),
        )
        rebuilt = candidate("version one, rebuilt by a reindex")
        await advance_to_verified(control_plane, rebuilt)

        with pytest.raises(PublicationSupersededError):
            await control_plane.publisher.publish(
                document_id=str(rebuilt.document_id),
                candidate_document_version_id=str(rebuilt.document_version_id),
                expected_active_document_version_id=str(first.document_version_id),
            )

        document_id = str(first.document_id)
        active = await control_plane.document_versions.active_versions([document_id])
        assert active[document_id].document_version_id == second.document_version_id
        stale = await control_plane.document_versions.get_version(str(rebuilt.document_version_id))
        assert stale is not None and stale.state == DocumentVersionState.VERIFIED


@pytest.mark.asyncio
async def test_publication_cannot_resurrect_a_retired_removed_document(
    tmp_path: Path,
) -> None:
    control_plane = make_control_plane(tmp_path)
    async with control_plane:
        first = candidate("version one")
        await advance_to_verified(control_plane, first)
        await control_plane.publisher.publish(
            document_id=str(first.document_id),
            candidate_document_version_id=str(first.document_version_id),
            expected_active_document_version_id=None,
        )
        rebuilt = candidate("version one, rebuilt by a reindex")
        await advance_to_verified(control_plane, rebuilt)
        await control_plane.publisher.retire_removed(document_id=str(first.document_id))

        with pytest.raises(PublicationSupersededError):
            await control_plane.publisher.publish(
                document_id=str(rebuilt.document_id),
                candidate_document_version_id=str(rebuilt.document_version_id),
                expected_active_document_version_id=str(first.document_version_id),
            )

        assert await control_plane.document_versions.active_versions([str(first.document_id)]) == {}


@pytest.mark.asyncio
async def test_republishing_the_active_version_is_a_replay_whatever_base_is_stated(
    tmp_path: Path,
) -> None:
    # A publish that committed and then timed out is retried with the base it was
    # built from; the pointer now equals the candidate, which is a replay.
    control_plane = make_control_plane(tmp_path)
    async with control_plane:
        first = candidate("version one")
        await advance_to_verified(control_plane, first)
        await control_plane.publisher.publish(
            document_id=str(first.document_id),
            candidate_document_version_id=str(first.document_version_id),
            expected_active_document_version_id=None,
        )

        retried = await control_plane.publisher.publish(
            document_id=str(first.document_id),
            candidate_document_version_id=str(first.document_version_id),
            expected_active_document_version_id=None,
        )

        assert retried.replayed is True


async def _staged(control_plane, value) -> None:  # type: ignore[no-untyped-def]
    repository = control_plane.document_versions
    await repository.create_candidate(value)
    for state in (
        DocumentVersionState.RAW_CAPTURED,
        DocumentVersionState.CANONICAL_READY,
        DocumentVersionState.CHUNKS_READY,
        DocumentVersionState.REPRESENTATIONS_READY,
        DocumentVersionState.PROJECTIONS_STAGED,
    ):
        await repository.transition(str(value.document_version_id), state)
    await repository.save_projection_manifest(
        ProjectionManifest(
            document_id=value.document_id,
            document_version_id=value.document_version_id,
            route_point_ids=("route-1",),
            evidence_point_ids=("evidence-1",),
            graph_node_keys=("document-node",),
            chunk_ids=("route-chunk", "evidence-chunk"),
        )
    )


@pytest.mark.asyncio
async def test_mark_verified_never_regresses_a_version_that_moved_on(
    tmp_path: Path,
) -> None:
    # Another attempt verifies and publishes between this attempt's status read and
    # its update. Without the status predicate the update put ACTIVE back to VERIFIED.
    control_plane = make_control_plane(tmp_path)
    async with control_plane:
        value = candidate("verify race")
        await _staged(control_plane, value)
        engine = control_plane.document_versions._client.raw.sync_engine
        fired: list[str] = []

        def overtake(conn, cursor, statement, parameters, context, executemany):  # type: ignore[no-untyped-def]  # noqa: PLR0913
            text = statement.upper()
            if not fired and text.startswith("SELECT") and "FROM DOCUMENT_VERSIONS" in text:
                fired.append(statement)
                side = conn.connection.cursor()
                side.execute(
                    "UPDATE document_versions SET status = 'ACTIVE' WHERE document_version_id = ?",
                    (str(value.document_version_id),),
                )
                side.close()

        event.listen(engine, "after_cursor_execute", overtake)
        try:
            await control_plane.document_versions.mark_verified(str(value.document_version_id))
        finally:
            event.remove(engine, "after_cursor_execute", overtake)

        assert fired
        snapshot = await control_plane.document_versions.get_version(str(value.document_version_id))
        assert snapshot is not None and snapshot.state == DocumentVersionState.ACTIVE


def test_mark_verified_reads_the_status_under_a_row_lock() -> None:
    sql = str(
        document_versions.document_version_lock("version-1").compile(dialect=postgresql.dialect())
    )

    assert sql.rstrip().endswith("FOR UPDATE")
