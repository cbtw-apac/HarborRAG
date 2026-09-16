from __future__ import annotations

from datetime import timedelta
from pathlib import Path

import pytest

from harborrag_core.base import utc_now
from harborrag_core.topology.permissions import ResolvedPermissionSnapshot

from .ingestion_control_fixtures import (
    advance_to_verified,
    candidate,
    make_control_plane,
    source_identity,
)


def snapshot(  # noqa: PLR0913 - test factory exposes each independent ACL state
    resource_kind: str,
    resource_id: str,
    revision: str,
    *,
    resolved_offset: timedelta = timedelta(),
    expires_offset: timedelta = timedelta(hours=1),
    known: bool = True,
    processing_allowed: bool = True,
    public: bool = True,
) -> ResolvedPermissionSnapshot:
    now = utc_now()
    return ResolvedPermissionSnapshot(
        tenant_id="DEFAULT",
        resource_kind=resource_kind,
        resource_id=resource_id,
        revision=revision,
        resolved_at=now + resolved_offset,
        expires_at=now + expires_offset,
        known=known,
        processing_allowed=processing_allowed,
        public=public,
        allowed_principal_ids=() if public else ("reader",),
    )


@pytest.mark.asyncio
async def test_permission_coverage_reports_active_corpus_without_identifiers(
    tmp_path: Path,
) -> None:
    async with make_control_plane(tmp_path) as control:
        empty = await control.topology.permission_coverage("DEFAULT")
        assert not empty.corpus_present
        assert not empty.snapshot_coverage_complete

        version = candidate("permission coverage")
        await advance_to_verified(control, version)
        await control.publisher.publish(
            document_id=str(version.document_id),
            candidate_document_version_id=str(version.document_version_id),
        )

        missing = await control.topology.permission_coverage("DEFAULT")
        assert missing.sources.resources == missing.sources.missing_snapshots == 1
        assert missing.documents.resources == missing.documents.missing_snapshots == 1
        assert not missing.snapshot_coverage_complete

        await control.topology.set_permissions(
            snapshot(
                "source",
                "scope-engineering",
                "source-expired",
                resolved_offset=timedelta(hours=-2),
                expires_offset=timedelta(hours=-1),
            )
        )
        expired = await control.topology.permission_coverage("DEFAULT")
        assert expired.sources.expired_snapshots == 1
        assert expired.documents.missing_snapshots == 1

        await control.topology.set_permissions(
            snapshot("source", "scope-engineering", "source-unknown", known=False)
        )
        unknown = await control.topology.permission_coverage("DEFAULT")
        assert unknown.sources.unknown_snapshots == 1

        await control.topology.set_permissions(
            snapshot("source", "scope-engineering", "source-current")
        )
        await control.topology.set_permissions(
            snapshot(
                "document",
                str(version.document_id),
                "document-current",
                processing_allowed=False,
                public=False,
            )
        )
        covered = await control.topology.permission_coverage("DEFAULT")
        assert covered.snapshot_coverage_complete
        assert covered.sources.public_snapshots == 1
        assert covered.documents.restricted_snapshots == 1
        assert covered.documents.processing_disallowed_snapshots == 1
        assert not covered.processing_permission_complete
        serialized = covered.model_dump_json()
        assert str(version.document_id) not in serialized
        assert "scope-engineering" not in serialized
        assert "reader" not in serialized

        await control.topology.set_permissions(
            snapshot(
                "document",
                str(version.document_id),
                "document-processing-allowed",
                public=False,
            )
        )
        ready = await control.topology.permission_coverage("DEFAULT")
        assert ready.snapshot_coverage_complete
        assert ready.processing_permission_complete


@pytest.mark.asyncio
async def test_permission_coverage_classifies_future_and_multiple_resources_by_tenant(
    tmp_path: Path,
) -> None:
    async with make_control_plane(tmp_path) as control:
        versions = (
            candidate("one"),
            candidate("two", source=source_identity("page-2")),
        )
        for version in versions:
            await advance_to_verified(control, version)
            await control.publisher.publish(
                document_id=str(version.document_id),
                candidate_document_version_id=str(version.document_version_id),
            )
        await control.topology.set_permissions(
            snapshot(
                "source",
                "scope-engineering",
                "source-future",
                resolved_offset=timedelta(hours=1),
                expires_offset=timedelta(hours=2),
            )
        )
        await control.topology.set_permissions(
            snapshot(
                "document",
                str(versions[0].document_id),
                "document-future",
                resolved_offset=timedelta(hours=1),
                expires_offset=timedelta(hours=2),
            )
        )
        await control.topology.set_permissions(
            snapshot(
                "document",
                str(versions[1].document_id),
                "document-unknown",
                known=False,
            )
        )

        report = await control.topology.permission_coverage("DEFAULT")
        other = await control.topology.permission_coverage("OTHER")

    assert report.sources.resources == 1
    assert report.sources.not_yet_valid_snapshots == 1
    assert report.documents.resources == 2
    assert report.documents.not_yet_valid_snapshots == 1
    assert report.documents.unknown_snapshots == 1
    assert not report.snapshot_coverage_complete
    assert not other.corpus_present
    assert other.sources.resources == other.documents.resources == 0
