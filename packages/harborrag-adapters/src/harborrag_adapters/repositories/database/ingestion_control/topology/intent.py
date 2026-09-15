"""Durable publication intent: no provider I/O or extraction on this path."""

from sqlalchemy import insert, select
from sqlalchemy.ext.asyncio import AsyncSession

from harborrag_core.base import utc_now
from harborrag_core.topology import digest

from ..row_values import DatabaseRow
from .configuration import lock_indexing_config
from .permissions import permission_dependencies
from .policy_schema import PERMISSION_SNAPSHOTS
from .schema import TOPOLOGY_JOBS, TOPOLOGY_POLICIES


async def enqueue_intent(
    session: AsyncSession, document: DatabaseRow, document_version_id: str
) -> bool:
    indexing = await lock_indexing_config(session, document["tenant_id"])
    if not indexing.config.serves_enrichment:
        return False
    dependencies = await permission_dependencies(
        session, document["tenant_id"], document["source_scope_id"], document["document_id"]
    )
    revisions = dict(
        (
            await session.execute(
                select(
                    PERMISSION_SNAPSHOTS.c.resource_kind,
                    PERMISSION_SNAPSHOTS.c.revision,
                ).where(
                    PERMISSION_SNAPSHOTS.c.tenant_id == document["tenant_id"],
                    (
                        (PERMISSION_SNAPSHOTS.c.resource_kind == "source")
                        & (PERMISSION_SNAPSHOTS.c.resource_id == document["source_scope_id"])
                    )
                    | (
                        (PERMISSION_SNAPSHOTS.c.resource_kind == "document")
                        & (PERMISSION_SNAPSHOTS.c.resource_id == document["document_id"])
                    ),
                )
            )
        )
        .tuples()
        .all()
    )
    result = await session.execute(
        select(TOPOLOGY_POLICIES).where(
            TOPOLOGY_POLICIES.c.tenant_id == document["tenant_id"],
            TOPOLOGY_POLICIES.c.source_scope_id == document["source_scope_id"],
            TOPOLOGY_POLICIES.c.enabled.is_(True),
        )
    )
    policy = result.mappings().one_or_none()
    if policy is None:
        return False
    # Revision prevents disable/re-enable or A -> B -> A from reviving a stale worker.
    job_id = digest(
        [
            document["tenant_id"],
            document_version_id,
            policy["fingerprint"],
            policy["revision"],
            indexing.epoch,
            revisions,
            [value.model_dump(mode="json") for value in dependencies],
        ]
    )
    if (
        await session.execute(
            select(TOPOLOGY_JOBS.c.job_id).where(TOPOLOGY_JOBS.c.job_id == job_id)
        )
    ).scalar_one_or_none() is not None:
        return False
    await session.execute(
        insert(TOPOLOGY_JOBS).values(
            job_id=job_id,
            tenant_id=document["tenant_id"],
            source_scope_id=document["source_scope_id"],
            document_id=document["document_id"],
            document_version_id=document_version_id,
            policy_revision=policy["revision"],
            fingerprint=policy["fingerprint"],
            policy=policy["policy"],
            state="pending" if dependencies else "deferred",
            error_code=None if dependencies else "permissions_unknown_or_processing_denied",
            config_epoch=indexing.epoch,
            permission_dependencies=[value.model_dump(mode="json") for value in dependencies],
            source_permission_revision=revisions.get("source", ""),
            document_permission_revision=revisions.get("document", ""),
            fence=0,
            attempts=0,
            created_at=utc_now(),
        )
    )
    return True
