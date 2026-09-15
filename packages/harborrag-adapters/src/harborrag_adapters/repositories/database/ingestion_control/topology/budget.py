"""Transactional tenant budget reservations shared by every worker and stage."""

from dataclasses import dataclass
from datetime import timedelta
from decimal import ROUND_CEILING, Decimal

from sqlalchemy import and_, func, insert, or_, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.ext.asyncio import AsyncSession

from harborrag_adapters.repositories.backends.sqlalchemy import SQLAlchemyDBClient
from harborrag_core.base import utc_now
from harborrag_core.contracts import HarborConflictError
from harborrag_core.topology import TopologyJob
from harborrag_core.topology.budget import (
    BudgetAdmission,
    BudgetRequest,
    BudgetReservation,
    UsageSettlement,
)

from .configuration import lock_indexing_config
from .guards import job_from_row, lock_job, require_lease
from .policy_schema import BUDGET_DAYS, BUDGET_RESERVATIONS
from .reads import eligible_builds
from .schema import TOPOLOGY_ACCEPTED, TOPOLOGY_BUILDS, TOPOLOGY_JOBS
from .transactions import topology_transaction


def microusd(value: Decimal) -> int:
    return int((value * 1000000).to_integral_value(rounding=ROUND_CEILING))


@dataclass(frozen=True)
class SummaryBudgetOwner:
    tenant_id: str
    job_id: str
    fence: int
    document_version_id: str


async def reserve(
    session: AsyncSession, job: TopologyJob | SummaryBudgetOwner, request: BudgetRequest
) -> BudgetAdmission:
    state = await lock_indexing_config(session, job.tenant_id)
    now = utc_now()
    later = now + timedelta(seconds=30)
    if state.config.spending_paused:
        return BudgetAdmission(admitted=False, reason="spending_paused", retry_after=later)
    existing = (
        (
            await session.execute(
                select(BUDGET_RESERVATIONS).where(
                    BUDGET_RESERVATIONS.c.tenant_id == job.tenant_id,
                    BUDGET_RESERVATIONS.c.reservation_id == request.reservation_id,
                )
            )
        )
        .mappings()
        .one_or_none()
    )
    if existing is not None:
        if (
            existing["job_id"] != job.job_id
            or existing["fence"] != job.fence
            or existing["request"] != request.model_dump(mode="json")
        ):
            raise HarborConflictError("budget reservation identity is immutable")
        return BudgetAdmission(
            admitted=False, reason="reservation_exists", retry_after=existing["expires_at"]
        )
    if request.operation_key is not None:
        calls = (
            await session.execute(
                select(func.coalesce(func.sum(BUDGET_RESERVATIONS.c.provider_calls), 0))
                .outerjoin(TOPOLOGY_JOBS, TOPOLOGY_JOBS.c.job_id == BUDGET_RESERVATIONS.c.job_id)
                .where(
                    BUDGET_RESERVATIONS.c.tenant_id == job.tenant_id,
                    BUDGET_RESERVATIONS.c.operation_key == request.operation_key,
                    or_(
                        TOPOLOGY_JOBS.c.document_version_id == job.document_version_id,
                        BUDGET_RESERVATIONS.c.job_id == job.job_id,
                    ),
                )
            )
        ).scalar_one()
        if calls + request.provider_calls > request.max_provider_calls:
            return BudgetAdmission(admitted=False, reason="operation_call_cap")
    active = (
        await session.execute(
            select(func.count())
            .select_from(BUDGET_RESERVATIONS)
            .outerjoin(
                TOPOLOGY_JOBS,
                and_(
                    TOPOLOGY_JOBS.c.job_id == BUDGET_RESERVATIONS.c.job_id,
                    TOPOLOGY_JOBS.c.fence == BUDGET_RESERVATIONS.c.fence,
                ),
            )
            .where(
                BUDGET_RESERVATIONS.c.tenant_id == job.tenant_id,
                BUDGET_RESERVATIONS.c.state == "reserved",
                BUDGET_RESERVATIONS.c.expires_at > now,
                or_(
                    TOPOLOGY_JOBS.c.job_id.is_not(None),
                    BUDGET_RESERVATIONS.c.job_id.startswith("summary:"),
                ),
            )
        )
    ).scalar_one()
    if active >= state.config.budgets.max_concurrency:
        return BudgetAdmission(admitted=False, reason="concurrency", retry_after=later)
    day = now.date().isoformat()
    factory = sqlite_insert if session.get_bind().dialect.name == "sqlite" else pg_insert
    await session.execute(
        factory(BUDGET_DAYS)
        .values(tenant_id=job.tenant_id, day=day, tokens=0, cost_microusd=0)
        .on_conflict_do_nothing(index_elements=["tenant_id", "day"])
    )
    ledger = (
        (
            await session.execute(
                select(BUDGET_DAYS).where(
                    BUDGET_DAYS.c.tenant_id == job.tenant_id,
                    BUDGET_DAYS.c.day == day,
                )
            )
        )
        .mappings()
        .one()
    )
    tokens = request.input_tokens + request.output_tokens
    cost = microusd(request.cost_usd)
    tomorrow = (now + timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)
    if ledger["tokens"] + tokens > state.config.budgets.daily_token_cap:
        return BudgetAdmission(admitted=False, reason="daily_tokens", retry_after=tomorrow)
    if ledger["cost_microusd"] + cost > microusd(state.config.budgets.daily_cost_usd):
        return BudgetAdmission(admitted=False, reason="daily_cost", retry_after=tomorrow)
    reservation = BudgetReservation(
        reservation_id=request.reservation_id,
        tenant_id=job.tenant_id,
        job_id=job.job_id,
        fence=job.fence,
        reserved_tokens=tokens,
        reserved_cost_usd=request.cost_usd,
        expires_at=now + timedelta(seconds=state.config.budgets.reservation_seconds),
    )
    await session.execute(
        insert(BUDGET_RESERVATIONS).values(
            tenant_id=job.tenant_id,
            reservation_id=request.reservation_id,
            job_id=job.job_id,
            fence=job.fence,
            day=day,
            state="reserved",
            expires_at=reservation.expires_at,
            tokens=tokens,
            cost_microusd=cost,
            reservation=reservation.model_dump(mode="json"),
            request=request.model_dump(mode="json"),
            operation_key=request.operation_key,
            provider_calls=request.provider_calls,
        )
    )
    await session.execute(
        update(BUDGET_DAYS)
        .where(BUDGET_DAYS.c.tenant_id == job.tenant_id, BUDGET_DAYS.c.day == day)
        .values(tokens=ledger["tokens"] + tokens, cost_microusd=ledger["cost_microusd"] + cost)
    )
    return BudgetAdmission(admitted=True, reservation=reservation)


class TopologyBudgetOperations:
    _client: SQLAlchemyDBClient

    async def reserve_budget(self, job: TopologyJob, request: BudgetRequest) -> BudgetAdmission:
        async with topology_transaction(self._client) as session:
            await require_lease(session, job)
            return await reserve(session, job, request)

    async def reserve_for_build(
        self, tenant_id: str, build_id: str, request: BudgetRequest
    ) -> BudgetAdmission:
        async with topology_transaction(self._client) as session:
            row = (
                (
                    await session.execute(
                        select(TOPOLOGY_JOBS)
                        .join(TOPOLOGY_BUILDS, TOPOLOGY_BUILDS.c.job_id == TOPOLOGY_JOBS.c.job_id)
                        .where(
                            TOPOLOGY_BUILDS.c.tenant_id == tenant_id,
                            TOPOLOGY_BUILDS.c.build_id == build_id,
                        )
                    )
                )
                .mappings()
                .one_or_none()
            )
            if row is None:
                raise HarborConflictError("derived-stage build is unknown")
            job = job_from_row(row)
            await lock_job(session, job)
            if (
                await session.execute(
                    eligible_builds(tenant_id).where(TOPOLOGY_ACCEPTED.c.build_id == build_id)
                )
            ).first() is None:
                raise HarborConflictError("derived-stage build is not accepted")
            return await reserve(session, job, request)

    async def settle_budget(
        self, tenant_id: str, reservation_id: str, usage: UsageSettlement
    ) -> None:
        async with topology_transaction(self._client) as session:
            await lock_indexing_config(session, tenant_id)
            row = (
                (
                    await session.execute(
                        select(BUDGET_RESERVATIONS).where(
                            BUDGET_RESERVATIONS.c.tenant_id == tenant_id,
                            BUDGET_RESERVATIONS.c.reservation_id == reservation_id,
                        )
                    )
                )
                .mappings()
                .one_or_none()
            )
            if row is None:
                raise HarborConflictError("budget reservation is unknown")
            serialized = usage.model_dump(mode="json")
            if row["state"] == "settled":
                if row["settlement"] != serialized:
                    raise HarborConflictError("usage settlement is immutable")
                return
            known_tokens = (usage.input_tokens or 0) + (usage.output_tokens or 0)
            tokens = (
                known_tokens
                if usage.input_tokens is not None and usage.output_tokens is not None
                else max(row["tokens"], known_tokens)
            )
            cost = microusd(usage.cost_usd) if usage.cost_usd is not None else row["cost_microusd"]
            await session.execute(
                update(BUDGET_DAYS)
                .where(BUDGET_DAYS.c.tenant_id == tenant_id, BUDGET_DAYS.c.day == row["day"])
                .values(
                    tokens=BUDGET_DAYS.c.tokens + tokens - row["tokens"],
                    cost_microusd=BUDGET_DAYS.c.cost_microusd + cost - row["cost_microusd"],
                )
            )
            await session.execute(
                update(BUDGET_RESERVATIONS)
                .where(
                    BUDGET_RESERVATIONS.c.tenant_id == tenant_id,
                    BUDGET_RESERVATIONS.c.reservation_id == reservation_id,
                )
                .values(state="settled", tokens=tokens, cost_microusd=cost, settlement=serialized)
            )
