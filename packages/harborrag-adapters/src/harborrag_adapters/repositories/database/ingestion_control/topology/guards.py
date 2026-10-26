"""One lock order and eligibility predicate for every canonical topology write."""

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from harborrag_core.base import utc_now
from harborrag_core.contracts import HarborConflictError
from harborrag_core.topology import TopologyJob, TopologyPolicy

from ..row_values import DatabaseRow
from ..schema import DOCUMENTS
from .configuration import lock_indexing_config
from .permissions import permission_dependencies
from .schema import TOPOLOGY_JOBS, TOPOLOGY_POLICIES


def job_from_row(row: DatabaseRow) -> TopologyJob:
    return TopologyJob(**{key: row[key] for key in TopologyJob.model_fields})


async def lock_job(session: AsyncSession, job: TopologyJob) -> DatabaseRow:
    """Document -> policy -> job prevents activation/replacement deadlocks."""
    document = (
        (
            await session.execute(
                select(DOCUMENTS)
                .where(
                    DOCUMENTS.c.tenant_id == job.tenant_id,
                    DOCUMENTS.c.document_id == job.document_id,
                )
                .with_for_update()
            )
        )
        .mappings()
        .one_or_none()
    )
    indexing = await lock_indexing_config(session, job.tenant_id)
    dependencies = await permission_dependencies(
        session, job.tenant_id, job.source_scope_id, job.document_id
    )
    if (
        not indexing.config.serves_enrichment
        or indexing.epoch != job.config_epoch
        or not dependencies
        or dependencies != job.permission_dependencies
    ):
        raise HarborConflictError("topology mode or permission dependencies are obsolete")
    policy = (
        (
            await session.execute(
                select(TOPOLOGY_POLICIES)
                .where(
                    TOPOLOGY_POLICIES.c.tenant_id == job.tenant_id,
                    TOPOLOGY_POLICIES.c.source_scope_id == job.source_scope_id,
                )
                .with_for_update()
            )
        )
        .mappings()
        .one_or_none()
    )
    row = (
        (
            await session.execute(
                select(TOPOLOGY_JOBS)
                .where(
                    TOPOLOGY_JOBS.c.tenant_id == job.tenant_id,
                    TOPOLOGY_JOBS.c.job_id == job.job_id,
                )
                .with_for_update()
            )
        )
        .mappings()
        .one_or_none()
    )
    if (
        row is None
        or document is None
        or policy is None
        or document["active_document_version_id"] != job.document_version_id
        or document["source_scope_id"] != job.source_scope_id
        or not policy["enabled"]
        or policy["revision"] != job.policy_revision
        or policy["fingerprint"] != job.policy.fingerprint
        or row["fence"] != job.fence
        or row["document_id"] != job.document_id
        or row["document_version_id"] != job.document_version_id
        or row["policy_revision"] != job.policy_revision
        or TopologyPolicy.model_validate(row["policy"]) != job.policy
    ):
        raise HarborConflictError("topology job is obsolete or belongs to another scope")
    return row


async def require_lease(session: AsyncSession, job: TopologyJob) -> DatabaseRow:
    row = await lock_job(session, job)
    if row["state"] != "running" or row["lease_until"] is None or row["lease_until"] <= utc_now():
        raise HarborConflictError("topology job lease is expired or no longer running")
    return row
