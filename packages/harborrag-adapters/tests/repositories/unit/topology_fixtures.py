from datetime import timedelta

from harborrag_adapters.repositories.database.ingestion_control import IngestionControlPlaneDatabase
from harborrag_core.base import utc_now
from harborrag_core.ingestion import ArtifactReference
from harborrag_core.security.context import AccessContext
from harborrag_core.topology import (
    CanonicalAssertion,
    CanonicalMention,
    ChunkExtractionCheckpoint,
    DocumentTopologyBuild,
    EvidenceSpan,
    ExtractedAssertion,
    ExtractedEntity,
    ExtractionProfile,
    TopologyJob,
    TopologyPolicy,
)
from harborrag_core.topology.config import TenantIndexingConfig
from harborrag_core.topology.permissions import ResolvedPermissionSnapshot

from .ingestion_control_fixtures import advance_to_verified, candidate

ACCESS = AccessContext.system("DEFAULT")


async def permit(
    control: IngestionControlPlaneDatabase,
    document_id: str,
    source_scope_id: str = "scope-engineering",
) -> None:
    now = utc_now()
    for kind, resource in (("source", source_scope_id), ("document", document_id)):
        await control.topology.set_permissions(
            ResolvedPermissionSnapshot(
                tenant_id="DEFAULT",
                resource_kind=kind,
                resource_id=resource,
                revision="acl-1",
                resolved_at=now,
                expires_at=now + timedelta(hours=1),
                known=True,
                processing_allowed=True,
                public=True,
            )
        )


def policy(*, enabled: bool = True) -> TopologyPolicy:
    return TopologyPolicy(
        tenant_id="DEFAULT",
        source_scope_id="scope-engineering",
        enabled=enabled,
        profile=ExtractionProfile(model="test-model", deployment_revision="r1", prompt_digest="p1"),
    )


def artifact(key: str = "topology/output.json") -> ArtifactReference:
    return ArtifactReference(
        bucket="artifacts",
        key=key,
        sha256="a" * 64,
        byte_size=2,
        media_type="application/json",
    )


async def publish(control: IngestionControlPlaneDatabase, text: str = "one") -> None:
    version = candidate(text)
    await control.topology.configure_indexing(
        TenantIndexingConfig(tenant_id="DEFAULT", enabled=True)
    )
    await permit(control, str(version.document_id))
    await advance_to_verified(control, version)
    await control.publisher.publish(
        document_id=str(version.document_id),
        candidate_document_version_id=str(version.document_version_id),
    )


async def prepared_build(
    control: IngestionControlPlaneDatabase, job: TopologyJob
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
    build_id = f"build-{job.fence}-{job.policy_revision}"
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
            entity_id=f"opaque-{index}",
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
        subject_entity_id="opaque-1",
        object_entity_id="opaque-2",
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
