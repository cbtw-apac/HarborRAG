"""Summary projection: jobs operations."""

from datetime import timedelta

from sqlalchemy import or_, select, true, update

from harborrag_core.base import utc_now
from harborrag_core.contracts import HarborConflictError
from harborrag_core.summaries import (
    SummaryBinding,
    SummaryLease,
    SummaryPolicy,
)
from harborrag_core.topology.budget import BudgetAdmission, BudgetRequest
from harborrag_core.topology.extraction import digest

from .summary_authority import SummaryAuthority
from .summary_intent import invalidate_summary_scope, lock_summary_tenant
from .summary_schema import SUMMARY_BINDINGS, SUMMARY_SCOPES
from .topology.budget import SummaryBudgetOwner, reserve
from .topology.configuration import lock_indexing_config
from .topology.transactions import topology_transaction


class SummaryJobOperations(SummaryAuthority):
    async def configure(
        self, tenant_id: str, source_scope_id: str, policy: SummaryPolicy | None
    ) -> None:
        async with topology_transaction(self._client) as session:
            await lock_summary_tenant(session, tenant_id)
            row = (
                (
                    await session.execute(
                        select(SUMMARY_SCOPES).where(
                            SUMMARY_SCOPES.c.tenant_id == tenant_id,
                            SUMMARY_SCOPES.c.source_scope_id == source_scope_id,
                        )
                    )
                )
                .mappings()
                .one_or_none()
            )
            serialized = policy.model_dump(mode="json") if policy else None
            if row is not None and row["policy"] == serialized:
                return
            await invalidate_summary_scope(session, tenant_id, source_scope_id)
            await session.execute(
                update(SUMMARY_SCOPES)
                .where(
                    SUMMARY_SCOPES.c.tenant_id == tenant_id,
                    SUMMARY_SCOPES.c.source_scope_id == source_scope_id,
                )
                .values(
                    policy=serialized,
                    execution="queued" if policy and source_scope_id != "@tenant" else "idle",
                    lease_until=None,
                    available_at=utc_now(),
                )
            )

    async def runnable_scopes(self, tenant_id: str) -> tuple[tuple[str, int, int], ...]:
        async with self._client.sessions() as session:
            now = utc_now()
            rows = (
                (
                    await session.execute(
                        select(
                            SUMMARY_SCOPES.c.source_scope_id,
                            SUMMARY_SCOPES.c.revision,
                            SUMMARY_SCOPES.c.fence,
                        )
                        .where(
                            SUMMARY_SCOPES.c.tenant_id == tenant_id,
                            SUMMARY_SCOPES.c.policy.is_not(None),
                            or_(
                                (SUMMARY_SCOPES.c.execution.in_(("queued", "blocked", "failed")))
                                & (SUMMARY_SCOPES.c.available_at <= now),
                                (SUMMARY_SCOPES.c.execution == "running")
                                & (SUMMARY_SCOPES.c.lease_until <= now),
                            ),
                        )
                        .order_by(SUMMARY_SCOPES.c.available_at)
                        .limit(100)
                    )
                )
                .tuples()
                .all()
            )
        return tuple((str(row[0]), int(row[1]), int(row[2])) for row in rows)

    async def source_scope_ids(self, tenant_id: str) -> tuple[str, ...]:
        async with self._client.sessions() as session:
            return await self._source_ids(session, tenant_id)

    async def reconcile(self, tenant_id: str) -> int:
        """Repair missed membership/manifest changes and recover failed dispatches."""
        repaired = 0
        async with topology_transaction(self._client) as session:
            await lock_summary_tenant(session, tenant_id)
            rows = (
                (
                    await session.execute(
                        select(SUMMARY_SCOPES).where(
                            SUMMARY_SCOPES.c.tenant_id == tenant_id,
                            SUMMARY_SCOPES.c.policy.is_not(None),
                            SUMMARY_SCOPES.c.execution == "idle",
                            SUMMARY_SCOPES.c.source_scope_id != "@tenant",
                        )
                    )
                )
                .mappings()
                .all()
            )
            for row in rows:
                try:
                    snapshot = await self._snapshot(session, tenant_id, row["source_scope_id"])
                except HarborConflictError:
                    continue
                bindings = (
                    (
                        await session.execute(
                            select(SUMMARY_BINDINGS.c.binding).where(
                                SUMMARY_BINDINGS.c.tenant_id == tenant_id,
                                SUMMARY_BINDINGS.c.source_scope_id == row["source_scope_id"],
                            )
                        )
                    )
                    .scalars()
                    .all()
                )
                current = [
                    SummaryBinding.model_validate(value)
                    for value in bindings
                    if value["revision"] == row["revision"]
                ]
                if (not current and snapshot.document_versions) or any(
                    binding.manifest.membership_digest != snapshot.membership_digest
                    for binding in current
                ):
                    await invalidate_summary_scope(session, tenant_id, row["source_scope_id"])
                    repaired += 1
        return repaired

    async def claim(
        self, tenant_id: str, *, lease_seconds: int = 300, source_scope_id: str | None = None
    ) -> SummaryLease | None:
        async with topology_transaction(self._client) as session:
            await lock_summary_tenant(session, tenant_id)
            state = await lock_indexing_config(session, tenant_id)
            if state.config.prohibited or state.config.spending_paused:
                return None
            now = utc_now()
            row = (
                (
                    await session.execute(
                        select(SUMMARY_SCOPES)
                        .where(
                            SUMMARY_SCOPES.c.tenant_id == tenant_id,
                            SUMMARY_SCOPES.c.policy.is_not(None),
                            SUMMARY_SCOPES.c.source_scope_id == source_scope_id
                            if source_scope_id
                            else true(),
                            or_(
                                (SUMMARY_SCOPES.c.execution.in_(("queued", "blocked", "failed")))
                                & (SUMMARY_SCOPES.c.available_at <= now),
                                (SUMMARY_SCOPES.c.execution == "running")
                                & (SUMMARY_SCOPES.c.lease_until <= now),
                            ),
                        )
                        .order_by(SUMMARY_SCOPES.c.available_at, SUMMARY_SCOPES.c.source_scope_id)
                        .limit(1)
                    )
                )
                .mappings()
                .one_or_none()
            )
            if row is None:
                return None
            lease = SummaryLease(
                tenant_id=tenant_id,
                source_scope_id=row["source_scope_id"],
                revision=row["revision"],
                fence=row["fence"] + 1,
                policy=row["policy"],
                lease_until=now + timedelta(seconds=lease_seconds),
            )
            await session.execute(
                update(SUMMARY_SCOPES)
                .where(*self._scope(lease))
                .values(
                    fence=lease.fence,
                    execution="running",
                    lease_until=lease.lease_until,
                    error_code=None,
                )
            )
            return lease

    async def renew(self, lease: SummaryLease, *, lease_seconds: int = 300) -> None:
        async with topology_transaction(self._client) as session:
            await lock_summary_tenant(session, lease.tenant_id)
            await self._require(session, lease, check_revision=False)
            await session.execute(
                update(SUMMARY_SCOPES)
                .where(*self._scope(lease))
                .values(lease_until=utc_now() + timedelta(seconds=lease_seconds))
            )

    async def reserve(self, lease: SummaryLease, request: BudgetRequest) -> BudgetAdmission:
        async with topology_transaction(self._client) as session:
            await lock_summary_tenant(session, lease.tenant_id)
            await self._require(session, lease, check_revision=False)
            await self._snapshot(session, lease.tenant_id, lease.source_scope_id)
            return await reserve(
                session,
                SummaryBudgetOwner(
                    tenant_id=lease.tenant_id,
                    job_id="summary:" + digest([lease.source_scope_id, lease.revision]),
                    fence=lease.fence,
                    document_version_id="summary:" + lease.source_scope_id,
                ),
                request,
            )

    async def finish(
        self, lease: SummaryLease, *, error_code: str | None = None, blocked: bool = False
    ) -> bool:
        async with topology_transaction(self._client) as session:
            await lock_summary_tenant(session, lease.tenant_id)
            row = (
                (await session.execute(select(SUMMARY_SCOPES).where(*self._scope(lease))))
                .mappings()
                .one()
            )
            if row["fence"] != lease.fence:
                return False
            changed = row["revision"] != lease.revision
            execution = (
                "queued"
                if changed
                else ("blocked" if blocked else "failed")
                if error_code
                else "idle"
            )
            if row["policy"] is None:
                execution = "idle"
            await session.execute(
                update(SUMMARY_SCOPES)
                .where(*self._scope(lease))
                .values(
                    execution=execution,
                    lease_until=None,
                    available_at=utc_now() + timedelta(seconds=1 if changed else 30),
                    dirty_since=row["dirty_since"] if changed or error_code else None,
                    error_code=error_code,
                )
            )
            return not changed and error_code is None
