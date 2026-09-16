from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy import select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.ext.asyncio import AsyncSession

from harborrag_adapters.repositories.backends.sqlalchemy import SQLAlchemyDBClient
from harborrag_core.topology.config import TenantIndexingConfig, TenantIndexingState

from .policy_schema import INDEXING_CONFIGS
from .schema import TOPOLOGY_JOBS
from .transactions import topology_transaction


@dataclass(frozen=True, slots=True)
class _BudgetPolicyChange:
    """Budget deferrals that a more permissive configuration can now admit."""

    reasons: frozenset[str]

    @classmethod
    def between(
        cls, before: TenantIndexingConfig, after: TenantIndexingConfig
    ) -> _BudgetPolicyChange:
        reasons: set[str] = set()
        if before.spending_paused and not after.spending_paused:
            reasons.add("spending_paused")
        if after.budgets.max_concurrency > before.budgets.max_concurrency:
            reasons.add("concurrency")
        if after.budgets.daily_token_cap > before.budgets.daily_token_cap:
            reasons.add("daily_tokens")
        if after.budgets.daily_cost_usd > before.budgets.daily_cost_usd:
            reasons.add("daily_cost")
        return cls(frozenset(reasons))

    async def wake_deferred_jobs(self, session: AsyncSession, tenant_id: str) -> None:
        if not self.reasons:
            return
        await session.execute(
            update(TOPOLOGY_JOBS)
            .where(
                TOPOLOGY_JOBS.c.tenant_id == tenant_id,
                TOPOLOGY_JOBS.c.state == "deferred",
                TOPOLOGY_JOBS.c.error_code.in_(self.reasons),
            )
            .values(
                state="pending",
                available_at=None,
                lease_until=None,
                error_code=None,
            )
        )


async def _wake_retryable_failed_jobs(
    session: AsyncSession,
    tenant_id: str,
    *,
    max_job_attempts: int,
) -> None:
    """Recover failures created under an older, smaller attempt allowance."""

    await session.execute(
        update(TOPOLOGY_JOBS)
        .where(
            TOPOLOGY_JOBS.c.tenant_id == tenant_id,
            TOPOLOGY_JOBS.c.state == "failed",
            TOPOLOGY_JOBS.c.attempts < max_job_attempts,
        )
        .values(
            state="pending",
            available_at=None,
            lease_until=None,
            error_code=None,
        )
    )


async def lock_indexing_config(session: AsyncSession, tenant_id: str) -> TenantIndexingState:
    default = TenantIndexingConfig(tenant_id=tenant_id)
    factory = sqlite_insert if session.get_bind().dialect.name == "sqlite" else pg_insert
    await session.execute(
        factory(INDEXING_CONFIGS)
        .values(
            tenant_id=tenant_id,
            epoch=0,
            enabled=False,
            prohibited=False,
            spending_paused=False,
            config=default.model_dump(mode="json"),
        )
        .on_conflict_do_nothing(index_elements=["tenant_id"])
    )
    row = (
        (
            await session.execute(
                select(INDEXING_CONFIGS)
                .where(
                    INDEXING_CONFIGS.c.tenant_id == tenant_id,
                )
                .with_for_update()
            )
        )
        .mappings()
        .one()
    )
    return TenantIndexingState(
        config=TenantIndexingConfig.model_validate(row["config"]), epoch=row["epoch"]
    )


class IndexingConfigurationOperations:
    _client: SQLAlchemyDBClient

    async def get_indexing(self, tenant_id: str) -> TenantIndexingState:
        async with self._client.sessions() as session:
            row = (
                (
                    await session.execute(
                        select(INDEXING_CONFIGS).where(
                            INDEXING_CONFIGS.c.tenant_id == tenant_id,
                        )
                    )
                )
                .mappings()
                .one_or_none()
            )
        if row is None:
            return TenantIndexingState(config=TenantIndexingConfig(tenant_id=tenant_id))
        return TenantIndexingState(
            config=TenantIndexingConfig.model_validate(row["config"]), epoch=row["epoch"]
        )

    async def configure_indexing(self, config: TenantIndexingConfig) -> TenantIndexingState:
        async with topology_transaction(self._client) as session:
            before = await lock_indexing_config(session, config.tenant_id)
            budget_change = _BudgetPolicyChange.between(before.config, config)
            epoch = before.epoch + int(
                (before.config.enabled, before.config.prohibited)
                != (config.enabled, config.prohibited)
            )
            await session.execute(
                update(INDEXING_CONFIGS)
                .where(
                    INDEXING_CONFIGS.c.tenant_id == config.tenant_id,
                )
                .values(
                    epoch=epoch,
                    enabled=config.enabled,
                    prohibited=config.prohibited,
                    spending_paused=config.spending_paused,
                    config=config.model_dump(mode="json"),
                )
            )
            if config.serves_enrichment:
                await budget_change.wake_deferred_jobs(session, config.tenant_id)
                await _wake_retryable_failed_jobs(
                    session,
                    config.tenant_id,
                    max_job_attempts=config.budgets.max_job_attempts,
                )
        return TenantIndexingState(config=config, epoch=epoch)
