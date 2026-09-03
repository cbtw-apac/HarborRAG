from __future__ import annotations

from pathlib import Path

import pytest

from harborrag_core.chunking import ConnectorType
from harborrag_core.contracts import HarborConflictError
from harborrag_core.ingestion import SourceIdentity

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
        )
        replayed = await control_plane.publisher.publish(
            document_id=str(first.document_id),
            candidate_document_version_id=str(first.document_version_id),
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
        )
        unverified = candidate("not verified")
        await control_plane.document_versions.create_candidate(unverified)

        with pytest.raises(HarborConflictError, match="VERIFIED"):
            await control_plane.publisher.publish(
                document_id=str(unverified.document_id),
                candidate_document_version_id=str(unverified.document_version_id),
            )

        current = await control_plane.document_versions.active_versions([str(active.document_id)])
        assert current[str(active.document_id)].document_version_id == active.document_version_id


@pytest.mark.asyncio
async def test_active_snapshot_for_tenant_hides_other_tenants_documents(
    tmp_path: Path,
) -> None:
    control_plane = make_control_plane(tmp_path)
    async with control_plane:
        owned = candidate(
            "tenant a content",
            source=SourceIdentity(
                tenant_id="tenant-a",
                connector_type=ConnectorType.CONFLUENCE,
                connection_id="wiki.example",
                source_item_id="page-1",
                source_scope_id="scope-engineering",
            ),
        )
        await advance_to_verified(control_plane, owned)
        await control_plane.publisher.publish(
            document_id=str(owned.document_id),
            candidate_document_version_id=str(owned.document_version_id),
        )

        snapshot = await control_plane.document_versions.active_snapshot_for_tenant(
            tenant_id="tenant-a",
            document_id=str(owned.document_id),
        )
        assert snapshot is not None
        assert snapshot.document_version_id == owned.document_version_id

        leaked = await control_plane.document_versions.active_snapshot_for_tenant(
            tenant_id="tenant-b",
            document_id=str(owned.document_id),
        )
        assert leaked is None
