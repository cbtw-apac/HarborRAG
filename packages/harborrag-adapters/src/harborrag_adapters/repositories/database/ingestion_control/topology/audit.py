"""Bounded discovery for projection audits and provably obsolete build cleanup."""

from sqlalchemy import and_, exists, or_, select

from harborrag_adapters.repositories.backends.sqlalchemy import SQLAlchemyDBClient

from ..schema import DOCUMENTS
from .policy_schema import BUILD_PERMISSIONS, INDEXING_CONFIGS, PERMISSION_SNAPSHOTS
from .reads import eligible_builds
from .schema import TOPOLOGY_ACCEPTED, TOPOLOGY_BUILDS, TOPOLOGY_JOBS, TOPOLOGY_POLICIES


class TopologyAuditOperations:
    _client: SQLAlchemyDBClient

    async def audit_build_ids(
        self,
        tenant_id: str,
        *,
        limit: int = 100,
        after_build_id: str | None = None,
    ) -> tuple[str, ...]:
        query = eligible_builds(tenant_id)
        if after_build_id is not None:
            query = query.where(TOPOLOGY_ACCEPTED.c.build_id > after_build_id)
        async with self._client.sessions() as session:
            values = (
                (
                    await session.execute(
                        query.order_by(TOPOLOGY_ACCEPTED.c.build_id).limit(max(1, min(limit, 1000)))
                    )
                )
                .scalars()
                .all()
            )
        return tuple(values)

    async def retired_build_ids(
        self,
        tenant_id: str,
        *,
        build_ids: tuple[str, ...] = (),
        limit: int = 100,
        after_build_id: str | None = None,
    ) -> tuple[str, ...]:
        # A lease may be retried. Only monotonic obsolescence permits deletion.
        query = (
            select(TOPOLOGY_BUILDS.c.build_id)
            .join(
                TOPOLOGY_JOBS,
                and_(
                    TOPOLOGY_JOBS.c.tenant_id == TOPOLOGY_BUILDS.c.tenant_id,
                    TOPOLOGY_JOBS.c.job_id == TOPOLOGY_BUILDS.c.job_id,
                ),
            )
            .join(
                DOCUMENTS,
                and_(
                    DOCUMENTS.c.tenant_id == TOPOLOGY_JOBS.c.tenant_id,
                    DOCUMENTS.c.document_id == TOPOLOGY_JOBS.c.document_id,
                ),
            )
            .join(
                TOPOLOGY_POLICIES,
                and_(
                    TOPOLOGY_POLICIES.c.tenant_id == TOPOLOGY_JOBS.c.tenant_id,
                    TOPOLOGY_POLICIES.c.source_scope_id == TOPOLOGY_JOBS.c.source_scope_id,
                ),
            )
            .join(INDEXING_CONFIGS, INDEXING_CONFIGS.c.tenant_id == TOPOLOGY_JOBS.c.tenant_id)
            .where(
                TOPOLOGY_BUILDS.c.tenant_id == tenant_id,
                ~TOPOLOGY_BUILDS.c.build_id.in_(eligible_builds(tenant_id)),
                or_(
                    INDEXING_CONFIGS.c.epoch > TOPOLOGY_JOBS.c.config_epoch,
                    exists(
                        select(BUILD_PERMISSIONS.c.resource_id)
                        .join(
                            PERMISSION_SNAPSHOTS,
                            and_(
                                PERMISSION_SNAPSHOTS.c.tenant_id == BUILD_PERMISSIONS.c.tenant_id,
                                PERMISSION_SNAPSHOTS.c.resource_kind
                                == BUILD_PERMISSIONS.c.resource_kind,
                                PERMISSION_SNAPSHOTS.c.resource_id
                                == BUILD_PERMISSIONS.c.resource_id,
                                PERMISSION_SNAPSHOTS.c.revision != BUILD_PERMISSIONS.c.revision,
                            ),
                        )
                        .where(
                            BUILD_PERMISSIONS.c.tenant_id == tenant_id,
                            BUILD_PERMISSIONS.c.build_id == TOPOLOGY_BUILDS.c.build_id,
                        )
                    ),
                    DOCUMENTS.c.active_document_version_id.is_(None),
                    DOCUMENTS.c.active_document_version_id != TOPOLOGY_JOBS.c.document_version_id,
                    TOPOLOGY_POLICIES.c.revision > TOPOLOGY_JOBS.c.policy_revision,
                    TOPOLOGY_JOBS.c.fence > TOPOLOGY_BUILDS.c.fence,
                    TOPOLOGY_JOBS.c.state.in_(("failed", "superseded")),
                ),
            )
        )
        if build_ids:
            query = query.where(TOPOLOGY_BUILDS.c.build_id.in_(build_ids))
        if after_build_id is not None:
            query = query.where(TOPOLOGY_BUILDS.c.build_id > after_build_id)
        async with self._client.sessions() as session:
            values = (
                (
                    await session.execute(
                        query.order_by(TOPOLOGY_BUILDS.c.build_id).limit(max(1, min(limit, 1000)))
                    )
                )
                .scalars()
                .all()
            )
        return tuple(values)
