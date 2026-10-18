"""Accepted topology builds serve only the datasources a reader is entitled to.

``eligible_builds(serving=True)`` is the one gate every serving read of mentions, assertions,
build eligibility and derived artifacts goes through. Its permission check is the reader's
ACL (or, on a shared corpus, nothing at all), so a build of another datasource used to serve
for as long as the reader could read it.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from harborrag_adapters.repositories.database.ingestion_control import (
    IngestionControlPlaneDatabase,
)
from harborrag_core.security import AccessContext
from harborrag_core.topology import (
    CanonicalAssertion,
    CanonicalMention,
    ChunkExtractionCheckpoint,
    DocumentTopologyBuild,
    EvidenceSpan,
    ExtractedAssertion,
    ExtractedEntity,
    TopologyJob,
)
from harborrag_core.topology.config import TenantIndexingConfig

from .ingestion_control_fixtures import (
    advance_to_verified,
    candidate,
    make_control_plane,
    publish_over_active,
    source_identity,
)
from .test_datasource_entitlement_predicates import (
    ENGINEERING,
    HR,
    acl_access,
    hr_identity,
    shared_access,
)
from .topology_fixtures import artifact, permit, policy


async def accepted_build(control: IngestionControlPlaneDatabase, scope: str) -> tuple[str, str]:
    """Publish a document of ``scope``, build its topology and accept it.

    Returns ``(document_id, build_id)``. The build holds two mentions and the one assertion
    between them, all named after the scope so the two datasources' records tell apart.
    """

    source = source_identity("page-eng") if scope == ENGINEERING else hr_identity()
    version = candidate(f"content of {scope}", source=source)
    await permit(control, str(version.document_id), scope)
    await advance_to_verified(control, version)
    await publish_over_active(control, str(version.document_id), str(version.document_version_id))
    job = await control.topology.claim("DEFAULT")
    assert job is not None and job.source_scope_id == scope
    build = await prepared_build(control, job, f"build-{scope}")
    await control.topology.stage(job, build)
    await control.topology.mark_verified(job, build.build_id)
    assert await control.topology.accept(job, build.build_id)
    return str(version.document_id), build.build_id


async def prepared_build(
    control: IngestionControlPlaneDatabase, job: TopologyJob, build_id: str
) -> DocumentTopologyBuild:
    await control.topology.prepare(job, ("evidence-1",))
    await control.topology.checkpoint(
        job,
        ChunkExtractionCheckpoint(
            chunk_id="evidence-1",
            input_digest="input",
            artifact=artifact(),
            deployment_revision="r1",
        ),
    )
    owner = {
        "tenant_id": job.tenant_id,
        "build_id": build_id,
        "document_id": job.document_id,
        "document_version_id": job.document_version_id,
        "chunk_id": "evidence-1",
    }
    mentions = tuple(
        CanonicalMention(
            **owner,
            mention_id=f"{build_id}-{index}",
            entity_id=f"{build_id}-entity-{index}",
            observation=ExtractedEntity(
                local_id=str(index),
                name=name,
                entity_type="service",
                span=EvidenceSpan(start=start, end=start + len(name), quote=name),
            ),
        )
        for index, name, start in ((1, "A", 0), (2, "B", 13))
    )
    assertion = CanonicalAssertion(
        **owner,
        assertion_id=f"{build_id}-assertion",
        subject_entity_id=f"{build_id}-entity-1",
        object_entity_id=f"{build_id}-entity-2",
        observation=ExtractedAssertion(
            local_id="a1",
            subject_id="1",
            object_id="2",
            predicate="depends_on",
            span=EvidenceSpan(start=0, end=14, quote="A depends on B"),
        ),
    )
    return DocumentTopologyBuild(
        build_id=build_id,
        job_id=job.job_id,
        artifact=artifact(build_id),
        chunk_ids=("evidence-1",),
        mentions=mentions,
        assertions=(assertion,),
    )


async def two_datasources(control: IngestionControlPlaneDatabase) -> tuple[str, str]:
    """Accept one build in each datasource; return the engineering and HR build ids."""

    await control.topology.configure_indexing(
        TenantIndexingConfig(tenant_id="DEFAULT", enabled=True)
    )
    for scope in (ENGINEERING, HR):
        await control.topology.configure_policy(
            policy().model_copy(update={"source_scope_id": scope})
        )
    eng = await accepted_build(control, ENGINEERING)
    hr = await accepted_build(control, HR)
    return eng[1], hr[1]


async def served_builds(control: IngestionControlPlaneDatabase, access: AccessContext) -> dict:
    mentions = await control.topology.active_mentions("DEFAULT", access=access)
    assertions = await control.topology.active_assertions("DEFAULT", access=access)
    return {
        "mentions": {item.build_id for item in mentions},
        "assertions": {item.build_id for item in assertions},
        "eligible": await control.topology.eligible_build_ids(
            "DEFAULT", ("build-" + ENGINEERING, "build-" + HR), access=access
        ),
    }


@pytest.mark.asyncio
async def test_shared_serving_reads_keep_only_entitled_datasources(tmp_path: Path) -> None:
    async with make_control_plane(tmp_path) as control:
        eng, hr = await two_datasources(control)

        everything = {"mentions": {eng, hr}, "assertions": {eng, hr}, "eligible": {eng, hr}}
        assert await served_builds(control, shared_access(None)) == everything
        assert await served_builds(control, shared_access(frozenset({ENGINEERING, HR}))) == (
            everything
        )
        assert await served_builds(control, shared_access(frozenset({ENGINEERING}))) == {
            "mentions": {eng},
            "assertions": {eng},
            "eligible": {eng},
        }
        assert await served_builds(control, shared_access(frozenset({HR}))) == {
            "mentions": {hr},
            "assertions": {hr},
            "eligible": {hr},
        }
        nothing = {"mentions": set(), "assertions": set(), "eligible": set()}
        assert await served_builds(control, shared_access(frozenset())) == nothing
        assert await served_builds(control, shared_access(frozenset({"scope-finance"}))) == nothing


@pytest.mark.asyncio
async def test_source_acl_serving_reads_also_keep_only_entitled_datasources(
    tmp_path: Path,
) -> None:
    async with make_control_plane(tmp_path) as control:
        # ``permit`` made both datasources' sources and documents public, so ACL-wise the
        # reader may read every build; the entitlement is what narrows it.
        eng, hr = await two_datasources(control)

        everything = {"mentions": {eng, hr}, "assertions": {eng, hr}, "eligible": {eng, hr}}
        assert await served_builds(control, acl_access(None)) == everything
        assert await served_builds(control, acl_access(frozenset({HR}))) == {
            "mentions": {hr},
            "assertions": {hr},
            "eligible": {hr},
        }
        assert await served_builds(control, acl_access(frozenset())) == {
            "mentions": set(),
            "assertions": set(),
            "eligible": set(),
        }
