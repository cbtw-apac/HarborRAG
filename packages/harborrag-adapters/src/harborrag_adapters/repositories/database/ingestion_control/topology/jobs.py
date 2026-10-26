from __future__ import annotations

from datetime import datetime, timedelta

from sqlalchemy import insert, or_, select, update

from harborrag_adapters.repositories.backends.sqlalchemy import SQLAlchemyDBClient
from harborrag_core.base import utc_now
from harborrag_core.contracts import HarborConflictError
from harborrag_core.topology import ChunkExtractionCheckpoint, TopologyJob

from .configuration import lock_indexing_config
from .guards import job_from_row, lock_job, require_lease
from .policy_schema import BUDGET_RESERVATIONS
from .schema import TOPOLOGY_CHECKPOINTS, TOPOLOGY_JOBS
from .transactions import topology_transaction


def runnable_predicate():  # type: ignore[no-untyped-def]
    return or_(
        TOPOLOGY_JOBS.c.state == "pending",
        (TOPOLOGY_JOBS.c.state == "deferred") & (TOPOLOGY_JOBS.c.available_at <= utc_now()),
        (
            (TOPOLOGY_JOBS.c.state == "deferred")
            & TOPOLOGY_JOBS.c.available_at.is_(None)
            & (TOPOLOGY_JOBS.c.error_code == "operation_call_cap")
        ),
        (TOPOLOGY_JOBS.c.state == "running") & (TOPOLOGY_JOBS.c.lease_until <= utc_now()),
    )


class TopologyJobOperations:
    _client: SQLAlchemyDBClient

    async def prepare(self, job: TopologyJob, chunk_ids: tuple[str, ...]) -> None:
        if len(chunk_ids) > 10000 or len(set(chunk_ids)) != len(chunk_ids):
            raise ValueError("expected chunk IDs must be unique and bounded")
        async with topology_transaction(self._client) as session:
            row = await require_lease(session, job)
            expected = sorted(chunk_ids)
            if row["expected_chunks"] is not None and row["expected_chunks"] != expected:
                raise HarborConflictError("topology input manifest is immutable")
            await session.execute(
                update(TOPOLOGY_JOBS)
                .where(
                    TOPOLOGY_JOBS.c.job_id == job.job_id,
                )
                .values(expected_chunks=expected)
            )

    async def reusable_checkpoint(
        self, tenant_id: str, extraction_fingerprint: str, input_digest: str
    ) -> ChunkExtractionCheckpoint | None:
        async with self._client.sessions() as session:
            value = (
                await session.execute(
                    select(TOPOLOGY_CHECKPOINTS.c.checkpoint)
                    .where(
                        TOPOLOGY_CHECKPOINTS.c.tenant_id == tenant_id,
                        TOPOLOGY_CHECKPOINTS.c.extraction_fingerprint == extraction_fingerprint,
                        TOPOLOGY_CHECKPOINTS.c.input_digest == input_digest,
                    )
                    .order_by(TOPOLOGY_CHECKPOINTS.c.job_id)
                    .limit(1)
                )
            ).scalar_one_or_none()
        return ChunkExtractionCheckpoint.model_validate(value) if value is not None else None

    async def claim(
        self,
        tenant_id: str,
        *,
        lease_seconds: int = 300,
        job_id: str | None = None,
    ) -> TopologyJob | None:
        if not 1 <= lease_seconds <= 3600:
            raise ValueError("lease_seconds must be between 1 and 3600")
        now = utc_now()
        async with topology_transaction(self._client) as session:
            query = (
                select(TOPOLOGY_JOBS)
                .where(
                    TOPOLOGY_JOBS.c.tenant_id == tenant_id,
                    runnable_predicate(),
                )
                .order_by(TOPOLOGY_JOBS.c.created_at, TOPOLOGY_JOBS.c.job_id)
                .limit(100)
            )
            if job_id is not None:
                query = query.where(TOPOLOGY_JOBS.c.job_id == job_id)
            rows = (await session.execute(query)).mappings().all()
            for candidate in rows:
                job = job_from_row(candidate)
                try:
                    row = await lock_job(session, job)
                except HarborConflictError:
                    await session.execute(
                        update(TOPOLOGY_JOBS)
                        .where(
                            TOPOLOGY_JOBS.c.job_id == job.job_id,
                            TOPOLOGY_JOBS.c.fence == job.fence,
                            TOPOLOGY_JOBS.c.state.in_(("pending", "running", "deferred")),
                        )
                        .values(state="superseded", lease_until=None)
                    )
                    continue
                indexing = await lock_indexing_config(session, tenant_id)
                if row["state"] not in ("pending", "running", "deferred"):
                    continue
                if row["state"] == "running" and row["lease_until"] > now:
                    continue
                if indexing.config.spending_paused:
                    await session.execute(
                        update(TOPOLOGY_JOBS)
                        .where(TOPOLOGY_JOBS.c.job_id == job.job_id)
                        .values(
                            state="deferred",
                            available_at=now + timedelta(seconds=30),
                            error_code="spending_paused",
                        )
                    )
                    continue
                if row["attempts"] >= indexing.config.budgets.max_job_attempts:
                    await session.execute(
                        update(TOPOLOGY_JOBS)
                        .where(
                            TOPOLOGY_JOBS.c.job_id == job.job_id,
                        )
                        .values(state="failed", lease_until=None, error_code="attempts_exhausted")
                    )
                    continue
                result = await session.execute(
                    update(TOPOLOGY_JOBS)
                    .where(
                        TOPOLOGY_JOBS.c.job_id == job.job_id,
                        TOPOLOGY_JOBS.c.fence == job.fence,
                    )
                    .values(
                        state="running",
                        fence=job.fence + 1,
                        attempts=job.attempts + 1,
                        lease_until=now + timedelta(seconds=lease_seconds),
                        error_code=None,
                    )
                    .returning(TOPOLOGY_JOBS)
                )
                claimed = result.mappings().one_or_none()
                if claimed is not None:
                    return job_from_row(claimed)
        return None

    async def defer(
        self, job: TopologyJob, reason: str, *, retry_after: datetime | None = None
    ) -> None:
        if retry_after is not None and (retry_after.tzinfo is None or retry_after <= utc_now()):
            raise ValueError("deferred jobs require a future timezone-aware deadline")
        async with topology_transaction(self._client) as session:
            await require_lease(session, job)
            dispatched = (
                await session.execute(
                    select(BUDGET_RESERVATIONS.c.reservation_id)
                    .where(
                        BUDGET_RESERVATIONS.c.tenant_id == job.tenant_id,
                        BUDGET_RESERVATIONS.c.job_id == job.job_id,
                        BUDGET_RESERVATIONS.c.fence == job.fence,
                    )
                    .limit(1)
                )
            ).scalar_one_or_none()
            await session.execute(
                update(TOPOLOGY_JOBS)
                .where(TOPOLOGY_JOBS.c.job_id == job.job_id, TOPOLOGY_JOBS.c.fence == job.fence)
                .values(
                    state="deferred",
                    available_at=retry_after,
                    lease_until=None,
                    attempts=job.attempts if dispatched else max(0, job.attempts - 1),
                    error_code=reason[:128],
                )
            )

    async def defer_job(
        self, job: TopologyJob, reason: str, retry_after: datetime | None = None
    ) -> None:
        await self.defer(job, reason, retry_after=retry_after)

    async def renew(self, job: TopologyJob, *, lease_seconds: int = 300) -> None:
        if not 1 <= lease_seconds <= 3600:
            raise ValueError("lease_seconds must be between 1 and 3600")
        async with topology_transaction(self._client) as session:
            await require_lease(session, job)
            await session.execute(
                update(TOPOLOGY_JOBS)
                .where(
                    TOPOLOGY_JOBS.c.job_id == job.job_id,
                    TOPOLOGY_JOBS.c.fence == job.fence,
                )
                .values(lease_until=utc_now() + timedelta(seconds=lease_seconds))
            )

    async def checkpoints(self, job: TopologyJob) -> tuple[ChunkExtractionCheckpoint, ...]:
        async with self._client.sessions() as session:
            rows = (
                (
                    await session.execute(
                        select(TOPOLOGY_CHECKPOINTS.c.checkpoint)
                        .where(
                            TOPOLOGY_CHECKPOINTS.c.tenant_id == job.tenant_id,
                            TOPOLOGY_CHECKPOINTS.c.job_id == job.job_id,
                        )
                        .order_by(TOPOLOGY_CHECKPOINTS.c.chunk_id)
                    )
                )
                .scalars()
                .all()
            )
        return tuple(ChunkExtractionCheckpoint.model_validate(row) for row in rows)

    async def checkpoint(
        self, job: TopologyJob, value: ChunkExtractionCheckpoint
    ) -> ChunkExtractionCheckpoint:
        if value.deployment_revision != job.policy.profile.deployment_revision:
            raise HarborConflictError(
                "checkpoint deployment revision differs from the pinned profile"
            )
        async with topology_transaction(self._client) as session:
            row = await require_lease(session, job)
            if row["expected_chunks"] is None or value.chunk_id not in row["expected_chunks"]:
                raise HarborConflictError("chunk was not declared in the topology input manifest")
            previous = (
                await session.execute(
                    select(TOPOLOGY_CHECKPOINTS.c.checkpoint).where(
                        TOPOLOGY_CHECKPOINTS.c.tenant_id == job.tenant_id,
                        TOPOLOGY_CHECKPOINTS.c.job_id == job.job_id,
                        TOPOLOGY_CHECKPOINTS.c.chunk_id == value.chunk_id,
                    )
                )
            ).scalar_one_or_none()
            if previous is not None:
                frozen = ChunkExtractionCheckpoint.model_validate(previous)
                if frozen != value:
                    raise HarborConflictError("chunk extraction checkpoint is immutable")
                return frozen
            await session.execute(
                insert(TOPOLOGY_CHECKPOINTS).values(
                    tenant_id=job.tenant_id,
                    job_id=job.job_id,
                    chunk_id=value.chunk_id,
                    input_digest=value.input_digest,
                    extraction_fingerprint=job.policy.profile.fingerprint,
                    checkpoint=value.model_dump(mode="json"),
                )
            )
        return value

    async def fail(self, job: TopologyJob, error_code: str) -> None:
        async with topology_transaction(self._client) as session:
            row = await require_lease(session, job)
            indexing = await lock_indexing_config(session, job.tenant_id)
            await session.execute(
                update(TOPOLOGY_JOBS)
                .where(
                    TOPOLOGY_JOBS.c.job_id == job.job_id,
                    TOPOLOGY_JOBS.c.fence == job.fence,
                )
                .values(
                    state="failed"
                    if row["attempts"] >= indexing.config.budgets.max_job_attempts
                    else "pending",
                    lease_until=None,
                    error_code=error_code[:128],
                )
            )
