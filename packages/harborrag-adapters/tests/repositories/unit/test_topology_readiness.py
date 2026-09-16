from pathlib import Path

import pytest

from harborrag_core.contracts import HarborConflictError
from harborrag_core.topology import DocumentTopologyBuild, digest
from harborrag_core.topology.config import TenantIndexingConfig
from harborrag_core.topology.permissions import DerivedArtifactLineage

from .ingestion_control_fixtures import make_control_plane
from .test_topology_permissions_budget import acl
from .topology_fixtures import artifact, policy, prepared_build, publish


@pytest.mark.asyncio
async def test_readiness_tracks_missing_profile_stages_and_filters_before_cursor(
    tmp_path: Path,
) -> None:
    async with make_control_plane(tmp_path) as control:
        await control.topology.configure_policy(policy())
        await publish(control)
        job = await control.topology.claim("DEFAULT")
        assert job is not None
        legacy = await prepared_build(control, job)
        build = legacy.model_copy(
            update={
                "projection_revision": "semantic-v2",
                "document_id": job.document_id,
                "document_version_id": job.document_version_id,
                "source_scope_id": job.source_scope_id,
                "config_epoch": job.config_epoch,
                "permission_dependencies": job.permission_dependencies,
            }
        )
        await control.topology.stage(job, build)
        await control.topology.mark_verified(job, build.build_id)
        assert await control.topology.accept(job, build.build_id)
        inputs = await control.topology.get_build_lineage("DEFAULT", build.build_id)
        assert inputs is not None
        assert await control.topology.pending_derivation_build_ids("DEFAULT", "embedding") == (
            build.build_id,
        )
        assert await control.topology.pending_derivation_build_ids("other", "embedding") == ()
        assert (
            await control.topology.pending_derivation_build_ids(
                "DEFAULT", "embedding", after_build_id=build.build_id
            )
            == ()
        )
        for kind in ("contextual_chunk", "parent_description"):
            lineage = DerivedArtifactLineage(
                **inputs.model_dump(),
                artifact_id=digest([build.build_id, kind, "embedding"]),
                artifact_kind=kind,
                build_id=build.build_id,
                input_digest="digest",
                metadata={"embedding_profile": "embedding"},
            )
            with pytest.raises(HarborConflictError, match="profile-bound"):
                await control.topology.publish_derivation(
                    "DEFAULT", lineage.model_copy(update={"artifact_id": "arbitrary"}), artifact()
                )
            await control.topology.publish_derivation("DEFAULT", lineage, artifact())
            pending = await control.topology.pending_derivation_build_ids("DEFAULT", "embedding")
            assert pending == ((build.build_id,) if kind == "contextual_chunk" else ())
        assert await control.topology.pending_derivation_build_ids(
            "DEFAULT", "embedding", parent_profile="parent-v2"
        ) == (build.build_id,)
        parent = DerivedArtifactLineage(
            **inputs.model_dump(),
            artifact_id=digest([build.build_id, "parent_description", "parent-v2"]),
            artifact_kind="parent_description",
            build_id=build.build_id,
            input_digest="new-parent-prompt",
            metadata={"embedding_profile": "parent-v2"},
        )
        await control.topology.publish_derivation("DEFAULT", parent, artifact())
        # Publishing only the new parent artifact completes readiness; contextual stays reused.
        assert (
            await control.topology.pending_derivation_build_ids(
                "DEFAULT", "embedding", parent_profile="parent-v2"
            )
            == ()
        )
        assert await control.topology.pending_derivation_build_ids(
            "DEFAULT", "new-contextual", parent_profile="parent-v2"
        ) == (build.build_id,)
        assert await control.topology.pending_derivation_build_ids("DEFAULT", "new-embedding") == (
            build.build_id,
        )
        await control.topology.configure_indexing(TenantIndexingConfig(tenant_id="DEFAULT"))
        assert await control.topology.pending_derivation_build_ids("DEFAULT", "new-embedding") == ()


@pytest.mark.asyncio
async def test_revoked_dependencies_exclude_pending_derived_work(tmp_path: Path) -> None:
    async with make_control_plane(tmp_path) as control:
        await control.topology.configure_policy(policy())
        await publish(control)
        job = await control.topology.claim("DEFAULT")
        assert job is not None
        build = (await prepared_build(control, job)).model_copy(
            update={
                "projection_revision": "semantic-v2",
                "document_id": job.document_id,
                "document_version_id": job.document_version_id,
                "source_scope_id": job.source_scope_id,
                "config_epoch": job.config_epoch,
                "permission_dependencies": job.permission_dependencies,
            }
        )
        await control.topology.stage(job, build)
        await control.topology.mark_verified(job, build.build_id)
        assert await control.topology.accept(job, build.build_id)
        await control.topology.set_permissions(acl(job.document_id, processing_allowed=False))
        assert await control.topology.pending_derivation_build_ids("DEFAULT", "embedding") == ()


@pytest.mark.asyncio
async def test_zero_chunk_accepted_build_has_no_pending_derived_products(tmp_path: Path) -> None:
    async with make_control_plane(tmp_path) as control:
        await control.topology.configure_policy(policy())
        await publish(control)
        job = await control.topology.claim("DEFAULT")
        assert job is not None
        await control.topology.prepare(job, ())
        build = DocumentTopologyBuild(
            build_id="zero-chunk-build",
            job_id=job.job_id,
            artifact=artifact(),
            chunk_ids=(),
            projection_revision="semantic-v2",
            document_id=job.document_id,
            document_version_id=job.document_version_id,
            source_scope_id=job.source_scope_id,
            config_epoch=job.config_epoch,
            permission_dependencies=job.permission_dependencies,
        )
        await control.topology.stage(job, build)
        await control.topology.mark_verified(job, build.build_id)
        assert await control.topology.accept(job, build.build_id)
        assert await control.topology.get_build_lineage("DEFAULT", build.build_id) is not None
        assert (
            await control.topology.pending_derivation_build_ids(
                "DEFAULT", "embedding", parent_profile="parent"
            )
            == ()
        )
