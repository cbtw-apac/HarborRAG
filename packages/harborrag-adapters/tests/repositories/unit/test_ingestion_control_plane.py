from __future__ import annotations

from pathlib import Path

import pytest

from harborrag_core.contracts import HarborConflictError
from harborrag_core.ingestion import DocumentVersionState, PublicationSupersededError

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
