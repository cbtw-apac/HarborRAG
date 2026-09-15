"""Resolved source-specific ACLs; unknown, expired and mismatched revisions deny."""

from sqlalchemy import delete, insert, select
from sqlalchemy.ext.asyncio import AsyncSession

from harborrag_adapters.repositories.backends.sqlalchemy import SQLAlchemyDBClient
from harborrag_core.base import utc_now
from harborrag_core.contracts import HarborConflictError
from harborrag_core.security.context import AccessContext
from harborrag_core.topology.permissions import PermissionDependency, ResolvedPermissionSnapshot

from ..schema import DOCUMENTS
from ..summary_intent import invalidate_summary_scope, lock_summary_tenant
from .authorization import authorized_documents, readable_snapshot
from .configuration import lock_indexing_config
from .policy_schema import PERMISSION_GRANTS, PERMISSION_HISTORY, PERMISSION_SNAPSHOTS
from .transactions import topology_transaction


async def permission_dependencies(
    session: AsyncSession, tenant_id: str, source_scope_id: str, document_id: str
) -> tuple[PermissionDependency, ...]:
    rows = (
        (
            await session.execute(
                select(PERMISSION_SNAPSHOTS.c.snapshot).where(
                    PERMISSION_SNAPSHOTS.c.tenant_id == tenant_id,
                    (
                        (PERMISSION_SNAPSHOTS.c.resource_kind == "source")
                        & (PERMISSION_SNAPSHOTS.c.resource_id == source_scope_id)
                    )
                    | (
                        (PERMISSION_SNAPSHOTS.c.resource_kind == "document")
                        & (PERMISSION_SNAPSHOTS.c.resource_id == document_id)
                    ),
                )
            )
        )
        .scalars()
        .all()
    )
    snapshots = tuple(ResolvedPermissionSnapshot.model_validate(row) for row in rows)
    now = utc_now()
    if len(snapshots) != 2 or any(
        not value.known
        or not value.processing_allowed
        or value.resolved_at > now
        or value.expires_at <= now
        for value in snapshots
    ):
        return ()
    return tuple(
        sorted((value.dependency for value in snapshots), key=lambda value: value.resource_kind)
    )


class TopologyPermissionOperations:
    _client: SQLAlchemyDBClient

    async def set_permissions(self, snapshot: ResolvedPermissionSnapshot) -> None:
        async with topology_transaction(self._client) as session:
            await lock_summary_tenant(session, snapshot.tenant_id)
            await lock_indexing_config(session, snapshot.tenant_id)
            predicate = (
                PERMISSION_SNAPSHOTS.c.tenant_id == snapshot.tenant_id,
                PERMISSION_SNAPSHOTS.c.resource_kind == snapshot.resource_kind,
                PERMISSION_SNAPSHOTS.c.resource_id == snapshot.resource_id,
            )
            previous = (
                await session.execute(select(PERMISSION_SNAPSHOTS.c.snapshot).where(*predicate))
            ).scalar_one_or_none()
            if previous is not None:
                before = ResolvedPermissionSnapshot.model_validate(previous)
                if snapshot.resolved_at < before.resolved_at:
                    raise HarborConflictError("permission synchronization cannot move backwards")
                if before.revision == snapshot.revision and before.model_dump(
                    exclude={"resolved_at", "expires_at"}
                ) != snapshot.model_dump(exclude={"resolved_at", "expires_at"}):
                    raise HarborConflictError(
                        "changed effective permissions require a new revision"
                    )
            old_revision = (
                await session.execute(
                    select(PERMISSION_HISTORY.c.revision).where(
                        PERMISSION_HISTORY.c.tenant_id == snapshot.tenant_id,
                        PERMISSION_HISTORY.c.resource_kind == snapshot.resource_kind,
                        PERMISSION_HISTORY.c.resource_id == snapshot.resource_id,
                        PERMISSION_HISTORY.c.revision == snapshot.revision,
                    )
                )
            ).scalar_one_or_none()
            if old_revision is not None and (
                previous is None or previous["revision"] != snapshot.revision
            ):
                raise HarborConflictError(
                    "a retired permission revision cannot become current again"
                )
            if old_revision is None:
                await session.execute(
                    insert(PERMISSION_HISTORY).values(
                        tenant_id=snapshot.tenant_id,
                        resource_kind=snapshot.resource_kind,
                        resource_id=snapshot.resource_id,
                        revision=snapshot.revision,
                        snapshot=snapshot.model_dump(mode="json"),
                    )
                )
            await session.execute(delete(PERMISSION_SNAPSHOTS).where(*predicate))
            await session.execute(
                insert(PERMISSION_SNAPSHOTS).values(
                    tenant_id=snapshot.tenant_id,
                    resource_kind=snapshot.resource_kind,
                    resource_id=snapshot.resource_id,
                    revision=snapshot.revision,
                    known=snapshot.known,
                    processing_allowed=snapshot.processing_allowed,
                    public=snapshot.public,
                    resolved_at=snapshot.resolved_at,
                    expires_at=snapshot.expires_at,
                    snapshot=snapshot.model_dump(mode="json"),
                )
            )
            await session.execute(
                delete(PERMISSION_GRANTS).where(
                    PERMISSION_GRANTS.c.tenant_id == snapshot.tenant_id,
                    PERMISSION_GRANTS.c.resource_kind == snapshot.resource_kind,
                    PERMISSION_GRANTS.c.resource_id == snapshot.resource_id,
                )
            )
            principals = set(snapshot.allowed_principal_ids) | set(snapshot.denied_principal_ids)
            if previous is None or previous["revision"] != snapshot.revision:
                scope_id = (
                    snapshot.resource_id
                    if snapshot.resource_kind == "source"
                    else (
                        await session.execute(
                            select(DOCUMENTS.c.source_scope_id).where(
                                DOCUMENTS.c.tenant_id == snapshot.tenant_id,
                                DOCUMENTS.c.document_id == snapshot.resource_id,
                            )
                        )
                    ).scalar_one_or_none()
                )
                if scope_id is not None:
                    await invalidate_summary_scope(session, snapshot.tenant_id, scope_id)
            if principals:
                await session.execute(
                    insert(PERMISSION_GRANTS),
                    [
                        {
                            "tenant_id": snapshot.tenant_id,
                            "resource_kind": snapshot.resource_kind,
                            "resource_id": snapshot.resource_id,
                            "principal_id": principal,
                            "allowed": principal in snapshot.allowed_principal_ids,
                            "denied": principal in snapshot.denied_principal_ids,
                        }
                        for principal in sorted(principals)
                    ],
                )

    async def allowed_document_ids(
        self, tenant_id: str, *, access: AccessContext | None, limit: int = 10000
    ) -> tuple[str, ...]:
        bound = max(1, min(limit, 10000))
        async with self._client.sessions() as session:
            values = (
                (
                    await session.execute(
                        authorized_documents(tenant_id, access)
                        .order_by("document_id")
                        .limit(bound + 1)
                    )
                )
                .mappings()
                .all()
            )
        if len(values) > bound:
            raise HarborConflictError(
                "authorized document enumeration exceeds the configured budget"
            )
        return tuple(str(value["document_id"]) for value in values)

    async def authorized_document_ids(
        self, tenant_id: str, document_ids: tuple[str, ...], *, access: AccessContext | None
    ) -> set[str]:
        if not document_ids:
            return set()
        async with self._client.sessions() as session:
            rows = (
                (
                    await session.execute(
                        authorized_documents(tenant_id, access).where(
                            DOCUMENTS.c.document_id.in_(document_ids),
                        )
                    )
                )
                .mappings()
                .all()
            )
        return {str(row["document_id"]) for row in rows}

    async def authorized_source_scope_ids(
        self,
        tenant_id: str,
        source_scope_ids: tuple[str, ...],
        *,
        access: AccessContext | None,
    ) -> set[str]:
        """Return source scopes with a current readable source permission snapshot."""

        if not source_scope_ids or access is None:
            return set()
        source = PERMISSION_SNAPSHOTS.alias("requested_source_acl")
        async with self._client.sessions() as session:
            rows = (
                (
                    await session.execute(
                        select(source.c.resource_id).where(
                            source.c.tenant_id == tenant_id,
                            source.c.resource_kind == "source",
                            source.c.resource_id.in_(source_scope_ids),
                            readable_snapshot(source, access),
                        )
                    )
                )
                .scalars()
                .all()
            )
        return {str(value) for value in rows}
