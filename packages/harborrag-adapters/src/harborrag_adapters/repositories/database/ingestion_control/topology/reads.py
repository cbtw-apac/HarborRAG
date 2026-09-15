from __future__ import annotations

from sqlalchemy import Select, and_, or_, select

from harborrag_adapters.repositories.backends.sqlalchemy import SQLAlchemyDBClient
from harborrag_core.contracts import HarborConflictError
from harborrag_core.security.context import AccessContext
from harborrag_core.topology import (
    CanonicalAssertion,
    CanonicalMention,
    DocumentTopologyBuild,
    TopologyJob,
    TopologyPolicy,
)

from ..schema import DOCUMENTS
from .authorization import build_permissions_current
from .builds import build_record_digest
from .guards import job_from_row
from .jobs import runnable_predicate
from .policy_schema import INDEXING_CONFIGS
from .schema import (
    TOPOLOGY_ACCEPTED,
    TOPOLOGY_ASSERTIONS,
    TOPOLOGY_BUILDS,
    TOPOLOGY_JOBS,
    TOPOLOGY_MENTIONS,
    TOPOLOGY_POLICIES,
)


def eligible_builds(
    tenant_id: str, access: AccessContext | None = None, *, serving: bool = False
) -> Select[tuple[str]]:
    return (
        select(TOPOLOGY_ACCEPTED.c.build_id)
        .join(
            DOCUMENTS,
            and_(
                DOCUMENTS.c.tenant_id == TOPOLOGY_ACCEPTED.c.tenant_id,
                DOCUMENTS.c.document_id == TOPOLOGY_ACCEPTED.c.document_id,
                DOCUMENTS.c.source_scope_id == TOPOLOGY_ACCEPTED.c.source_scope_id,
                DOCUMENTS.c.active_document_version_id == TOPOLOGY_ACCEPTED.c.document_version_id,
            ),
        )
        .join(
            TOPOLOGY_POLICIES,
            and_(
                TOPOLOGY_POLICIES.c.tenant_id == TOPOLOGY_ACCEPTED.c.tenant_id,
                TOPOLOGY_POLICIES.c.source_scope_id == TOPOLOGY_ACCEPTED.c.source_scope_id,
                TOPOLOGY_POLICIES.c.revision == TOPOLOGY_ACCEPTED.c.policy_revision,
                TOPOLOGY_POLICIES.c.enabled.is_(True),
            ),
        )
        .join(
            INDEXING_CONFIGS,
            and_(
                INDEXING_CONFIGS.c.tenant_id == TOPOLOGY_ACCEPTED.c.tenant_id,
                INDEXING_CONFIGS.c.epoch == TOPOLOGY_ACCEPTED.c.config_epoch,
                INDEXING_CONFIGS.c.enabled.is_(True),
                INDEXING_CONFIGS.c.prohibited.is_(False),
            ),
        )
        .where(
            TOPOLOGY_ACCEPTED.c.tenant_id == tenant_id,
            build_permissions_current(
                TOPOLOGY_ACCEPTED.c.build_id, tenant_id, access, serving=serving
            ),
        )
    )


class TopologyReader:
    _client: SQLAlchemyDBClient

    async def runnable_jobs(self, tenant_id: str, *, limit: int = 100) -> tuple[TopologyJob, ...]:
        async with self._client.sessions() as session:
            values = (
                (
                    await session.execute(
                        select(TOPOLOGY_JOBS)
                        .where(
                            TOPOLOGY_JOBS.c.tenant_id == tenant_id,
                            runnable_predicate(),
                        )
                        .order_by(TOPOLOGY_JOBS.c.created_at, TOPOLOGY_JOBS.c.job_id)
                        .limit(max(1, min(limit, 1000)))
                    )
                )
                .mappings()
                .all()
            )
        return tuple(job_from_row(value) for value in values)

    async def eligible_build_ids(
        self, tenant_id: str, build_ids: tuple[str, ...], *, access: AccessContext | None = None
    ) -> set[str]:
        if not build_ids:
            return set()
        async with self._client.sessions() as session:
            values = (
                (
                    await session.execute(
                        eligible_builds(tenant_id, access, serving=True).where(
                            TOPOLOGY_ACCEPTED.c.build_id.in_(build_ids),
                        )
                    )
                )
                .scalars()
                .all()
            )
        return set(values)

    async def get_policy(self, tenant_id: str, source_scope_id: str) -> TopologyPolicy | None:
        async with self._client.sessions() as session:
            value = (
                await session.execute(
                    select(TOPOLOGY_POLICIES.c.policy).where(
                        TOPOLOGY_POLICIES.c.tenant_id == tenant_id,
                        TOPOLOGY_POLICIES.c.source_scope_id == source_scope_id,
                    )
                )
            ).scalar_one_or_none()
        return TopologyPolicy.model_validate(value) if value is not None else None

    async def get_job(self, tenant_id: str, job_id: str) -> TopologyJob | None:
        async with self._client.sessions() as session:
            value = (
                (
                    await session.execute(
                        select(TOPOLOGY_JOBS).where(
                            TOPOLOGY_JOBS.c.tenant_id == tenant_id,
                            TOPOLOGY_JOBS.c.job_id == job_id,
                        )
                    )
                )
                .mappings()
                .one_or_none()
            )
        return job_from_row(value) if value is not None else None

    async def list_jobs(self, tenant_id: str, *, limit: int = 100) -> tuple[TopologyJob, ...]:
        async with self._client.sessions() as session:
            values = (
                (
                    await session.execute(
                        select(TOPOLOGY_JOBS)
                        .where(
                            TOPOLOGY_JOBS.c.tenant_id == tenant_id,
                        )
                        .order_by(TOPOLOGY_JOBS.c.created_at.desc(), TOPOLOGY_JOBS.c.job_id)
                        .limit(max(1, min(limit, 1000)))
                    )
                )
                .mappings()
                .all()
            )
        return tuple(job_from_row(value) for value in values)

    async def get_build(self, tenant_id: str, build_id: str) -> DocumentTopologyBuild | None:
        async with self._client.sessions() as session:
            manifest = (
                await session.execute(
                    select(TOPOLOGY_BUILDS.c.manifest).where(
                        TOPOLOGY_BUILDS.c.tenant_id == tenant_id,
                        TOPOLOGY_BUILDS.c.build_id == build_id,
                    )
                )
            ).scalar_one_or_none()
            if manifest is None:
                return None
            mentions = (
                (
                    await session.execute(
                        select(TOPOLOGY_MENTIONS.c.record)
                        .where(
                            TOPOLOGY_MENTIONS.c.tenant_id == tenant_id,
                            TOPOLOGY_MENTIONS.c.build_id == build_id,
                        )
                        .order_by(TOPOLOGY_MENTIONS.c.mention_id)
                    )
                )
                .scalars()
                .all()
            )
            assertions = (
                (
                    await session.execute(
                        select(TOPOLOGY_ASSERTIONS.c.record)
                        .where(
                            TOPOLOGY_ASSERTIONS.c.tenant_id == tenant_id,
                            TOPOLOGY_ASSERTIONS.c.build_id == build_id,
                        )
                        .order_by(TOPOLOGY_ASSERTIONS.c.assertion_id)
                    )
                )
                .scalars()
                .all()
            )
        build = DocumentTopologyBuild.model_validate(
            {
                **{key: value for key, value in manifest.items() if key != "record_digest"},
                "mentions": mentions,
                "assertions": assertions,
            }
        )
        if build_record_digest(build) != manifest["record_digest"]:
            raise HarborConflictError("canonical topology build checksum mismatch")
        return build

    async def active_mentions(  # noqa: PLR0913 - explicit ACL context on shared search contract
        self,
        tenant_id: str,
        *,
        labels: tuple[str, ...] = (),
        chunk_ids: tuple[str, ...] = (),
        entity_ids: tuple[str, ...] = (),
        limit: int = 100,
        access: AccessContext | None = None,
    ) -> tuple[CanonicalMention, ...]:
        query = select(TOPOLOGY_MENTIONS.c.record).where(
            TOPOLOGY_MENTIONS.c.tenant_id == tenant_id,
            TOPOLOGY_MENTIONS.c.build_id.in_(eligible_builds(tenant_id, access, serving=True)),
        )
        selectors = []
        if labels:
            selectors.append(
                TOPOLOGY_MENTIONS.c.label_key.in_([label.casefold() for label in labels])
            )
        if chunk_ids:
            selectors.append(TOPOLOGY_MENTIONS.c.chunk_id.in_(chunk_ids))
        if entity_ids:
            selectors.append(TOPOLOGY_MENTIONS.c.entity_id.in_(entity_ids))
        if selectors:
            query = query.where(or_(*selectors))
        async with self._client.sessions() as session:
            values = (
                (
                    await session.execute(
                        query.order_by(TOPOLOGY_MENTIONS.c.mention_id).limit(
                            max(1, min(limit, 1000))
                        )
                    )
                )
                .scalars()
                .all()
            )
        return tuple(CanonicalMention.model_validate(value) for value in values)

    async def active_assertions(
        self,
        tenant_id: str,
        *,
        entity_ids: tuple[str, ...] = (),
        limit: int = 100,
        access: AccessContext | None = None,
    ) -> tuple[CanonicalAssertion, ...]:
        query = select(TOPOLOGY_ASSERTIONS.c.record).where(
            TOPOLOGY_ASSERTIONS.c.tenant_id == tenant_id,
            TOPOLOGY_ASSERTIONS.c.build_id.in_(eligible_builds(tenant_id, access, serving=True)),
        )
        if entity_ids:
            query = query.where(
                or_(
                    TOPOLOGY_ASSERTIONS.c.subject_entity_id.in_(entity_ids),
                    TOPOLOGY_ASSERTIONS.c.object_entity_id.in_(entity_ids),
                )
            )
        async with self._client.sessions() as session:
            values = (
                (
                    await session.execute(
                        query.order_by(TOPOLOGY_ASSERTIONS.c.assertion_id).limit(
                            max(1, min(limit, 1000))
                        )
                    )
                )
                .scalars()
                .all()
            )
        return tuple(CanonicalAssertion.model_validate(value) for value in values)
