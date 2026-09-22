"""Summary projection: authority operations."""

from sqlalchemy import select, true
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.sql.elements import ColumnElement

from harborrag_adapters.repositories.backends.sqlalchemy import SQLAlchemyDBClient
from harborrag_core.base import utc_now
from harborrag_core.contracts import HarborConflictError
from harborrag_core.summaries import (
    SummaryLease,
    SummarySnapshot,
)
from harborrag_core.topology.extraction import digest
from harborrag_core.topology.permissions import ResolvedPermissionSnapshot

from .schema import DOCUMENT_VERSIONS, DOCUMENTS, PROJECTION_MANIFESTS, SOURCE_SCOPES
from .summary_intent import lock_summary_tenant
from .summary_schema import SUMMARY_SCOPES
from .topology.configuration import lock_indexing_config
from .topology.policy_schema import PERMISSION_SNAPSHOTS
from .topology.transactions import topology_transaction


class SummaryAuthority:
    def __init__(self, client: SQLAlchemyDBClient) -> None:
        self._client = client
        self._shared_processing: dict[str, str] = {}

    def allow_shared_processing(self, tenant_id: str, revision: str) -> None:
        """Opt a trusted worker/reader into an operator-approved processing basis."""
        self._shared_processing[tenant_id] = revision

    @staticmethod
    def _scope(lease: SummaryLease) -> tuple[ColumnElement[bool], ...]:
        return (
            SUMMARY_SCOPES.c.tenant_id == lease.tenant_id,
            SUMMARY_SCOPES.c.source_scope_id == lease.source_scope_id,
        )

    async def _require(
        self, session: AsyncSession, lease: SummaryLease, *, check_revision: bool = True
    ) -> None:
        row = (
            (await session.execute(select(SUMMARY_SCOPES).where(*self._scope(lease))))
            .mappings()
            .one()
        )
        state = await lock_indexing_config(session, lease.tenant_id)
        if (
            (check_revision and row["revision"] != lease.revision)
            or row["fence"] != lease.fence
            or row["execution"] != "running"
            or row["lease_until"] <= utc_now()
            or row["policy"] != lease.policy.model_dump(mode="json")
            or state.config.prohibited
        ):
            raise HarborConflictError("summary work is superseded or its lease expired")

    async def scope_current(self, lease: SummaryLease) -> bool:
        async with topology_transaction(self._client) as session:
            await lock_summary_tenant(session, lease.tenant_id)
            await self._require(session, lease, check_revision=False)
            revision = (
                await session.execute(select(SUMMARY_SCOPES.c.revision).where(*self._scope(lease)))
            ).scalar_one()
            return bool(revision == lease.revision)

    async def source_documents(self, tenant_id: str, source_scope_id: str) -> tuple[dict, ...]:
        async with self._client.sessions() as session:
            return await self._documents(session, tenant_id, source_scope_id)

    @staticmethod
    async def _documents(
        session: AsyncSession, tenant_id: str, source_scope_id: str
    ) -> tuple[dict, ...]:
        rows = (
            (
                await session.execute(
                    select(
                        DOCUMENTS.c.document_id,
                        DOCUMENTS.c.active_document_version_id,
                        DOCUMENT_VERSIONS.c.chunk_artifact,
                        PROJECTION_MANIFESTS.c.manifest,
                    )
                    .join(
                        DOCUMENT_VERSIONS,
                        DOCUMENT_VERSIONS.c.document_version_id
                        == DOCUMENTS.c.active_document_version_id,
                    )
                    .outerjoin(
                        PROJECTION_MANIFESTS,
                        PROJECTION_MANIFESTS.c.document_version_id
                        == DOCUMENTS.c.active_document_version_id,
                    )
                    .where(
                        DOCUMENTS.c.tenant_id == tenant_id,
                        DOCUMENTS.c.source_scope_id == source_scope_id
                        if source_scope_id != "@tenant"
                        else true(),
                        DOCUMENT_VERSIONS.c.status == "ACTIVE",
                    )
                    .order_by(DOCUMENTS.c.document_id)
                    .limit(10001)
                )
            )
            .mappings()
            .all()
        )
        if len(rows) > 10000:
            raise HarborConflictError("summary source exceeds 10000-document manifest budget")
        return tuple(dict(row) for row in rows)

    async def _snapshot(
        self, session: AsyncSession, tenant_id: str, source_scope_id: str
    ) -> SummarySnapshot:
        documents = await self._documents(session, tenant_id, source_scope_id)
        ids = tuple(row["document_id"] for row in documents)
        scopes: tuple[str, ...] = (source_scope_id,)
        if source_scope_id == "@tenant":
            scopes = await self._source_ids(session, tenant_id)
        if tenant_id in self._shared_processing:
            return SummarySnapshot(
                tenant_id=tenant_id,
                source_scope_id=source_scope_id,
                document_versions={
                    row["document_id"]: row["active_document_version_id"] for row in documents
                },
                permission_dependencies=(),
                membership_digest=digest([scopes, documents, self._shared_processing[tenant_id]]),
            )
        rows = (
            (
                await session.execute(
                    select(PERMISSION_SNAPSHOTS.c.snapshot).where(
                        PERMISSION_SNAPSHOTS.c.tenant_id == tenant_id,
                        (
                            (PERMISSION_SNAPSHOTS.c.resource_kind == "source")
                            & (PERMISSION_SNAPSHOTS.c.resource_id.in_(scopes))
                        )
                        | (
                            (PERMISSION_SNAPSHOTS.c.resource_kind == "document")
                            & PERMISSION_SNAPSHOTS.c.resource_id.in_(ids)
                        ),
                    )
                )
            )
            .scalars()
            .all()
        )
        permissions = tuple(ResolvedPermissionSnapshot.model_validate(value) for value in rows)
        now = utc_now()
        if len(permissions) != len(ids) + len(scopes):
            raise HarborConflictError("SUMMARY_PERMISSION_SNAPSHOT_MISSING")
        if any(not value.known for value in permissions):
            raise HarborConflictError("SUMMARY_PERMISSION_SNAPSHOT_UNKNOWN")
        if any(value.resolved_at > now or value.expires_at <= now for value in permissions):
            raise HarborConflictError("SUMMARY_PERMISSION_SNAPSHOT_EXPIRED")
        if any(not value.processing_allowed for value in permissions):
            raise HarborConflictError("SUMMARY_PROCESSING_DISALLOWED")
        return SummarySnapshot(
            tenant_id=tenant_id,
            source_scope_id=source_scope_id,
            document_versions={
                row["document_id"]: row["active_document_version_id"] for row in documents
            },
            permission_dependencies=tuple(
                sorted(
                    (value.dependency for value in permissions),
                    key=lambda value: (value.resource_kind, value.resource_id),
                )
            ),
            membership_digest=digest([scopes, documents]),
        )

    async def snapshot(self, lease: SummaryLease) -> SummarySnapshot:
        async with topology_transaction(self._client) as session:
            await lock_summary_tenant(session, lease.tenant_id)
            await self._require(session, lease)
            return await self._snapshot(session, lease.tenant_id, lease.source_scope_id)

    @staticmethod
    async def _source_ids(session: AsyncSession, tenant_id: str) -> tuple[str, ...]:
        query = (
            select(SOURCE_SCOPES.c.source_scope_id)
            .where(SOURCE_SCOPES.c.tenant_id == tenant_id)
            .union(
                select(DOCUMENTS.c.source_scope_id).where(
                    DOCUMENTS.c.tenant_id == tenant_id,
                    DOCUMENTS.c.active_document_version_id.is_not(None),
                )
            )
            .order_by("source_scope_id")
        )
        rows = (await session.execute(query.limit(10001))).scalars().all()
        if len(rows) > 10000:
            raise HarborConflictError("summary tenant exceeds source manifest budget")
        return tuple(str(value) for value in rows)
