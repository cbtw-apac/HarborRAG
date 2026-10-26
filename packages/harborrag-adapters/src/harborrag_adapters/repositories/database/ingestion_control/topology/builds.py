from __future__ import annotations

from sqlalchemy import insert, select, update

from harborrag_adapters.repositories.backends.sqlalchemy import SQLAlchemyDBClient
from harborrag_core.contracts import HarborConflictError
from harborrag_core.topology import (
    CanonicalAssertion,
    CanonicalMention,
    DocumentTopologyBuild,
    TopologyJob,
    digest,
)

from .authorization import build_permissions_current
from .guards import require_lease
from .identity_proofs import build_input_proofs
from .policy_schema import BUILD_DOCUMENTS, BUILD_PERMISSIONS
from .schema import (
    TOPOLOGY_ACCEPTED,
    TOPOLOGY_ASSERTIONS,
    TOPOLOGY_BUILDS,
    TOPOLOGY_CHECKPOINTS,
    TOPOLOGY_JOBS,
    TOPOLOGY_MENTIONS,
)
from .transactions import topology_transaction


def validate_build(job: TopologyJob, build: DocumentTopologyBuild) -> None:
    if build.projection_revision == "semantic-v2" and (
        build.document_id != job.document_id
        or build.document_version_id != job.document_version_id
        or build.source_scope_id != job.source_scope_id
        or build.config_epoch != job.config_epoch
        or build.permission_dependencies != job.permission_dependencies
    ):
        raise ValueError("semantic build ownership or permission snapshot does not match job")
    if build.job_id != job.job_id or len(set(build.chunk_ids)) != len(build.chunk_ids):
        raise ValueError("invalid topology build identity or repeated chunk")
    if len({item.mention_id for item in build.mentions}) != len(build.mentions):
        raise ValueError("duplicate mention IDs")
    if len({item.assertion_id for item in build.assertions}) != len(build.assertions):
        raise ValueError("duplicate assertion IDs")
    items: tuple[CanonicalMention | CanonicalAssertion, ...] = (*build.mentions, *build.assertions)
    for item in items:
        if (
            item.tenant_id != job.tenant_id
            or item.build_id != build.build_id
            or item.document_id != job.document_id
            or item.document_version_id != job.document_version_id
            or item.chunk_id not in build.chunk_ids
        ):
            raise ValueError("topology record escapes build ownership")
    mentions = {(item.chunk_id, item.observation.local_id): item for item in build.mentions}
    if len(mentions) != len(build.mentions):
        raise ValueError("duplicate chunk-local mention IDs")
    for item in build.assertions:
        subject = mentions.get((item.chunk_id, item.observation.subject_id))
        target = mentions.get((item.chunk_id, item.observation.object_id))
        if (
            subject is None
            or target is None
            or subject.entity_id != item.subject_entity_id
            or target.entity_id != item.object_entity_id
        ):
            raise ValueError("assertion endpoint lacks an owning evidence mention")


def build_record_digest(build: DocumentTopologyBuild) -> str:
    serialized = build.model_dump(mode="json")
    serialized["mentions"] = sorted(serialized["mentions"], key=lambda item: item["mention_id"])
    serialized["assertions"] = sorted(
        serialized["assertions"], key=lambda item: item["assertion_id"]
    )
    return digest(serialized)


class TopologyBuildOperations:
    _client: SQLAlchemyDBClient

    async def mark_verified(self, job: TopologyJob, build_id: str) -> None:
        async with topology_transaction(self._client) as session:
            await require_lease(session, job)
            result = await session.execute(
                update(TOPOLOGY_BUILDS)
                .where(
                    TOPOLOGY_BUILDS.c.tenant_id == job.tenant_id,
                    TOPOLOGY_BUILDS.c.build_id == build_id,
                    TOPOLOGY_BUILDS.c.job_id == job.job_id,
                    TOPOLOGY_BUILDS.c.fence == job.fence,
                )
                .values(verified=True)
                .returning(TOPOLOGY_BUILDS.c.build_id)
            )
            if result.scalar_one_or_none() is None:
                raise HarborConflictError("candidate topology build has not been staged")

    async def stage(self, job: TopologyJob, build: DocumentTopologyBuild) -> None:
        validate_build(job, build)
        async with topology_transaction(self._client) as session:
            row = await require_lease(session, job)
            completed = (
                (
                    await session.execute(
                        select(TOPOLOGY_CHECKPOINTS.c.chunk_id).where(
                            TOPOLOGY_CHECKPOINTS.c.tenant_id == job.tenant_id,
                            TOPOLOGY_CHECKPOINTS.c.job_id == job.job_id,
                        )
                    )
                )
                .scalars()
                .all()
            )
            if row["expected_chunks"] != sorted(build.chunk_ids) or set(completed) != set(
                build.chunk_ids
            ):
                raise HarborConflictError("topology build has incomplete chunk coverage")
            existing = (
                (
                    await session.execute(
                        select(TOPOLOGY_BUILDS).where(
                            TOPOLOGY_BUILDS.c.build_id == build.build_id,
                        )
                    )
                )
                .mappings()
                .one_or_none()
            )
            # The artifact checksum commits all facts, even though the manifest is compact.
            manifest = build.model_dump(mode="json", exclude={"mentions", "assertions"})
            manifest["record_digest"] = build_record_digest(build)
            if existing is not None:
                if (
                    existing["tenant_id"] != job.tenant_id
                    or existing["job_id"] != job.job_id
                    or existing["fence"] != job.fence
                    or existing["manifest"] != manifest
                ):
                    raise HarborConflictError("topology candidate build is immutable")
                return
            await session.execute(
                insert(TOPOLOGY_BUILDS).values(
                    build_id=build.build_id,
                    tenant_id=job.tenant_id,
                    job_id=job.job_id,
                    fence=job.fence,
                    verified=False,
                    manifest=manifest,
                )
            )
            input_versions, dependencies = await build_input_proofs(session, job, build)
            await session.execute(
                insert(BUILD_DOCUMENTS),
                [
                    {
                        "tenant_id": job.tenant_id,
                        "build_id": build.build_id,
                        "document_id": document_id,
                        "document_version_id": version,
                    }
                    for document_id, version in input_versions.items()
                ],
            )
            await session.execute(
                insert(BUILD_PERMISSIONS),
                [
                    {
                        "tenant_id": job.tenant_id,
                        "build_id": build.build_id,
                        **value.model_dump(mode="json"),
                    }
                    for value in dependencies
                ],
            )
            if build.mentions:
                await session.execute(
                    insert(TOPOLOGY_MENTIONS),
                    [
                        {
                            "mention_id": item.mention_id,
                            "tenant_id": item.tenant_id,
                            "build_id": item.build_id,
                            "entity_id": item.entity_id,
                            "chunk_id": item.chunk_id,
                            "label_key": item.observation.name.casefold(),
                            "record": item.model_dump(mode="json"),
                        }
                        for item in build.mentions
                    ],
                )
            if build.assertions:
                await session.execute(
                    insert(TOPOLOGY_ASSERTIONS),
                    [
                        {
                            "assertion_id": item.assertion_id,
                            "tenant_id": item.tenant_id,
                            "build_id": item.build_id,
                            "subject_entity_id": item.subject_entity_id,
                            "object_entity_id": item.object_entity_id,
                            "record": item.model_dump(mode="json"),
                        }
                        for item in build.assertions
                    ],
                )

    async def accept(self, job: TopologyJob, build_id: str) -> bool:
        async with topology_transaction(self._client) as session:
            try:
                await require_lease(session, job)
            except HarborConflictError:
                return False
            build = (
                (
                    await session.execute(
                        select(TOPOLOGY_BUILDS).where(
                            TOPOLOGY_BUILDS.c.tenant_id == job.tenant_id,
                            TOPOLOGY_BUILDS.c.build_id == build_id,
                            TOPOLOGY_BUILDS.c.job_id == job.job_id,
                            TOPOLOGY_BUILDS.c.fence == job.fence,
                        )
                    )
                )
                .mappings()
                .one_or_none()
            )
            if build is None:
                raise HarborConflictError("candidate topology build has not been staged")
            if not build["verified"]:
                raise HarborConflictError("candidate topology build has not been verified")
            if not (
                await session.execute(
                    select(TOPOLOGY_BUILDS.c.build_id).where(
                        TOPOLOGY_BUILDS.c.build_id == build_id,
                        build_permissions_current(TOPOLOGY_BUILDS.c.build_id, job.tenant_id),
                    )
                )
            ).scalar_one_or_none():
                return False
            existing = (
                (
                    await session.execute(
                        select(TOPOLOGY_ACCEPTED).where(
                            TOPOLOGY_ACCEPTED.c.tenant_id == job.tenant_id,
                            TOPOLOGY_ACCEPTED.c.document_id == job.document_id,
                        )
                    )
                )
                .mappings()
                .one_or_none()
            )
            values = {
                "tenant_id": job.tenant_id,
                "document_id": job.document_id,
                "document_version_id": job.document_version_id,
                "source_scope_id": job.source_scope_id,
                "policy_revision": job.policy_revision,
                "build_id": build_id,
                "config_epoch": job.config_epoch,
            }
            if existing is None:
                await session.execute(insert(TOPOLOGY_ACCEPTED).values(**values))
            else:
                await session.execute(
                    update(TOPOLOGY_ACCEPTED)
                    .where(
                        TOPOLOGY_ACCEPTED.c.tenant_id == job.tenant_id,
                        TOPOLOGY_ACCEPTED.c.document_id == job.document_id,
                    )
                    .values(**values)
                )
            await session.execute(
                update(TOPOLOGY_JOBS)
                .where(
                    TOPOLOGY_JOBS.c.job_id == job.job_id,
                    TOPOLOGY_JOBS.c.fence == job.fence,
                )
                .values(state="accepted", lease_until=None)
            )
        return True
