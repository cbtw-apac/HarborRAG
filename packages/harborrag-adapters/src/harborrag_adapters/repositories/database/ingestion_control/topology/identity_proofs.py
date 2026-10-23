"""A resolved identifier may bridge documents only through its recorded input proofs."""

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from harborrag_core.contracts import HarborConflictError
from harborrag_core.topology import CanonicalMention, DocumentTopologyBuild, TopologyJob
from harborrag_core.topology.permissions import PermissionDependency

from ..schema import DOCUMENTS
from .permissions import permission_dependencies
from .schema import TOPOLOGY_MENTIONS, TOPOLOGY_RESOLUTION_SNAPSHOTS


async def resolution_proofs(
    session: AsyncSession, tenant_id: str, mapping: dict[str, str]
) -> dict[str, dict[str, str]]:
    if not mapping:
        return {}
    records = (
        (
            await session.execute(
                select(TOPOLOGY_MENTIONS.c.record).where(
                    TOPOLOGY_MENTIONS.c.tenant_id == tenant_id,
                    TOPOLOGY_MENTIONS.c.entity_id.in_(tuple(mapping)),
                )
            )
        )
        .scalars()
        .all()
    )
    mentions = [CanonicalMention.model_validate(value) for value in records]
    documents = (
        (
            await session.execute(
                select(DOCUMENTS).where(
                    DOCUMENTS.c.tenant_id == tenant_id,
                    DOCUMENTS.c.document_id.in_({value.document_id for value in mentions}),
                )
            )
        )
        .mappings()
        .all()
    )
    active = {
        str(value["document_id"]): str(value["active_document_version_id"]) for value in documents
    }
    proofs: dict[str, dict[str, str]] = {}
    seen = set()
    for mention in mentions:
        if active.get(mention.document_id) != mention.document_version_id:
            continue
        seen.add(mention.entity_id)
        proofs.setdefault(mapping[mention.entity_id], {})[mention.document_id] = (
            mention.document_version_id
        )
    if seen != set(mapping):
        raise HarborConflictError("identity merges require current source support for every member")
    return proofs


async def build_input_proofs(
    session: AsyncSession, job: TopologyJob, build: DocumentTopologyBuild
) -> tuple[dict[str, str], tuple[PermissionDependency, ...]]:
    versions = {job.document_id: job.document_version_id}
    if job.policy.resolution_revision != "conservative-v1":
        proofs = (
            await session.execute(
                select(TOPOLOGY_RESOLUTION_SNAPSHOTS.c.proofs).where(
                    TOPOLOGY_RESOLUTION_SNAPSHOTS.c.tenant_id == job.tenant_id,
                    TOPOLOGY_RESOLUTION_SNAPSHOTS.c.resolution_revision
                    == job.policy.resolution_revision,
                )
            )
        ).scalar_one_or_none() or {}
        for entity_id in {value.entity_id for value in build.mentions}:
            versions.update(proofs.get(entity_id, {}))
    dependencies: dict[tuple[str, str], PermissionDependency] = {}
    for document_id, version in versions.items():
        row = (
            (
                await session.execute(
                    select(DOCUMENTS).where(
                        DOCUMENTS.c.tenant_id == job.tenant_id,
                        DOCUMENTS.c.document_id == document_id,
                    )
                )
            )
            .mappings()
            .one_or_none()
        )
        if row is None or row["active_document_version_id"] != version:
            raise HarborConflictError("identity proof source is no longer current")
        values = await permission_dependencies(
            session, job.tenant_id, row["source_scope_id"], document_id
        )
        if not values:
            raise HarborConflictError(
                "identity proof permissions are unknown or processing is denied"
            )
        for value in values:
            dependencies[(value.resource_kind, value.resource_id)] = value
    return versions, tuple(dependencies.values())
