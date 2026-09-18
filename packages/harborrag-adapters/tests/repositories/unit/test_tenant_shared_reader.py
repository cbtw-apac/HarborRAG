from __future__ import annotations

from datetime import timedelta
from pathlib import Path

import pytest

from harborrag_core.base import utc_now
from harborrag_core.contracts import HarborConflictError
from harborrag_core.ingestion import SourceCatalogQuery
from harborrag_core.security import AccessContext
from harborrag_core.topology.permissions import ResolvedPermissionSnapshot

from .ingestion_control_fixtures import advance_to_verified, candidate, make_control_plane
from .topology_fixtures import policy, prepared_build, publish


@pytest.mark.asyncio
async def test_shared_reader_sees_published_document_without_acl(tmp_path: Path) -> None:
    async with make_control_plane(tmp_path) as control:
        version = candidate("shared content")
        await advance_to_verified(control, version)
        await control.publisher.publish(
            document_id=str(version.document_id),
            candidate_document_version_id=str(version.document_version_id),
        )
        shared = AccessContext(
            principal_id="reader", tenant_id="DEFAULT", corpus_mode="tenant_shared"
        )
        strict = AccessContext(principal_id="reader", tenant_id="DEFAULT")
        document_id = str(version.document_id)
        assert (
            await control.topology.authorized_document_ids("DEFAULT", (document_id,), access=strict)
            == set()
        )
        assert await control.topology.authorized_document_ids(
            "DEFAULT", (document_id,), access=shared
        ) == {document_id}
        assert await control.topology.published_document_page(
            "DEFAULT", access=shared, after="", limit=2
        ) == (document_id,)
        assert await control.topology.allowed_source_scope_ids("DEFAULT", access=shared) == (
            "scope-engineering",
        )
        sources = await control.source_scans.list_readable_sources(
            SourceCatalogQuery(tenant_id="DEFAULT", access=shared, limit=10)
        )
        assert [source.source_scope_id for source in sources] == ["scope-engineering"]
        wrong_tenant = AccessContext(
            principal_id="reader", tenant_id="OTHER", corpus_mode="tenant_shared"
        )
        assert (
            await control.topology.authorized_document_ids(
                "DEFAULT", (document_id,), access=wrong_tenant
            )
            == set()
        )


@pytest.mark.asyncio
async def test_shared_summary_snapshot_requires_explicit_processing_approval(
    tmp_path: Path,
) -> None:
    async with make_control_plane(tmp_path) as control:
        version = candidate("summary input")
        await advance_to_verified(control, version)
        await control.publisher.publish(
            document_id=str(version.document_id),
            candidate_document_version_id=str(version.document_version_id),
        )
        async with control.summaries._client.sessions() as session:
            with pytest.raises(HarborConflictError, match="SUMMARY_PERMISSION_SNAPSHOT_MISSING"):
                await control.summaries._snapshot(session, "DEFAULT", "scope-engineering")
        control.summaries.allow_shared_processing("DEFAULT", "approved-v1")
        async with control.summaries._client.sessions() as session:
            approved = await control.summaries._snapshot(session, "DEFAULT", "scope-engineering")
        assert approved.document_versions[str(version.document_id)] == str(
            version.document_version_id
        )
        assert approved.permission_dependencies == ()


@pytest.mark.asyncio
async def test_shared_reader_keeps_active_graph_after_acl_revocation(tmp_path: Path) -> None:
    async with make_control_plane(tmp_path) as control:
        await control.topology.configure_policy(policy())
        await publish(control)
        job = await control.topology.claim("DEFAULT")
        assert job is not None
        build = await prepared_build(control, job)
        await control.topology.stage(job, build)
        await control.topology.mark_verified(job, build.build_id)
        assert await control.topology.accept(job, build.build_id)
        shared = AccessContext(
            principal_id="reader", tenant_id="DEFAULT", corpus_mode="tenant_shared"
        )
        now = utc_now()
        await control.topology.set_permissions(
            ResolvedPermissionSnapshot(
                tenant_id="DEFAULT",
                resource_kind="document",
                resource_id=job.document_id,
                revision="revoked",
                resolved_at=now,
                expires_at=now + timedelta(hours=1),
                known=True,
                processing_allowed=False,
                public=False,
            )
        )
        assert len(await control.topology.active_mentions("DEFAULT", access=shared)) == 2
        assert (
            await control.topology.active_mentions(
                "DEFAULT", access=AccessContext(principal_id="reader", tenant_id="DEFAULT")
            )
            == ()
        )
