"""A version's admission change key follows the source without changing its identity."""

from __future__ import annotations

from pathlib import Path

import pytest

from harborrag_core.ingestion import DocumentVersionCandidate, DocumentVersionState

from .ingestion_control_fixtures import advance_to_verified, candidate, make_control_plane

pytestmark = pytest.mark.unit


def _with_key(
    value: DocumentVersionCandidate, admission_change_key: str
) -> DocumentVersionCandidate:
    return value.model_copy(
        update={
            "fingerprints": value.fingerprints.model_copy(
                update={"admission_change_key": admission_change_key}
            )
        }
    )


@pytest.mark.asyncio
async def test_replanning_a_version_with_unchanged_hashes_and_a_new_key_is_no_collision(
    tmp_path: Path,
) -> None:
    control_plane = make_control_plane(tmp_path)
    async with control_plane:
        value = candidate("release guide body")
        await advance_to_verified(control_plane, value)
        await control_plane.publisher.publish(
            document_id=str(value.document_id),
            candidate_document_version_id=str(value.document_version_id),
        )

        state = await control_plane.document_versions.create_candidate(
            _with_key(value, "admission-2")
        )

    assert state == DocumentVersionState.ACTIVE


@pytest.mark.asyncio
async def test_refreshing_the_key_changes_only_the_active_version(tmp_path: Path) -> None:
    control_plane = make_control_plane(tmp_path)
    async with control_plane:
        versions = control_plane.document_versions
        active = candidate("release guide body")
        await advance_to_verified(control_plane, active)
        await control_plane.publisher.publish(
            document_id=str(active.document_id),
            candidate_document_version_id=str(active.document_version_id),
        )
        pending = candidate("release guide draft")
        await versions.create_candidate(pending)

        for value in (active, pending):
            await versions.refresh_admission_change_key(
                str(value.document_version_id), admission_change_key="admission-2"
            )
        refreshed = await versions.active_snapshot(str(active.document_id))
        untouched = await versions.get_version(str(pending.document_version_id))

    assert refreshed is not None
    assert refreshed.document_version_id == active.document_version_id
    assert refreshed.fingerprints.admission_change_key == "admission-2"
    assert untouched is not None
    assert untouched.fingerprints == pending.fingerprints
