"""Durable invalidation in the publication/permissions transaction; no model I/O."""

from collections.abc import Callable
from datetime import timedelta

from sqlalchemy import select, update
from sqlalchemy.dialects.postgresql import Insert as PostgreSQLInsert
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.dialects.sqlite import Insert as SQLiteInsert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.ext.asyncio import AsyncSession

from harborrag_core.base import utc_now
from harborrag_core.summaries import SummaryPolicy

from .summary_schema import SUMMARY_SCOPES, SUMMARY_TENANTS


def upsert(session: AsyncSession) -> Callable[..., PostgreSQLInsert | SQLiteInsert]:
    return sqlite_insert if session.get_bind().dialect.name == "sqlite" else pg_insert


async def lock_summary_tenant(session: AsyncSession, tenant_id: str) -> None:
    await session.execute(
        upsert(session)(SUMMARY_TENANTS)
        .values(tenant_id=tenant_id, revision=0)
        .on_conflict_do_nothing(index_elements=["tenant_id"])
    )
    await session.execute(
        select(SUMMARY_TENANTS).where(SUMMARY_TENANTS.c.tenant_id == tenant_id).with_for_update()
    )


async def invalidate_summary_scope(
    session: AsyncSession, tenant_id: str, source_scope_id: str
) -> None:
    """Caller holds the tenant summary lock before document/configuration locks."""
    now = utc_now()
    predicate = (
        SUMMARY_SCOPES.c.tenant_id == tenant_id,
        SUMMARY_SCOPES.c.source_scope_id == source_scope_id,
    )
    await session.execute(
        upsert(session)(SUMMARY_SCOPES)
        .values(
            tenant_id=tenant_id,
            source_scope_id=source_scope_id,
            revision=0,
            fence=0,
            execution="idle",
            policy=None,
        )
        .on_conflict_do_nothing(index_elements=["tenant_id", "source_scope_id"])
    )
    row = (await session.execute(select(SUMMARY_SCOPES).where(*predicate))).mappings().one()
    policy = SummaryPolicy.model_validate(row["policy"]) if row["policy"] else None
    dirty_since = row["dirty_since"] or now
    available = (
        min(
            now + timedelta(seconds=policy.debounce_seconds),
            dirty_since + timedelta(seconds=policy.max_wait_seconds),
        )
        if policy
        else now
    )
    await session.execute(
        update(SUMMARY_SCOPES)
        .where(*predicate)
        .values(
            revision=row["revision"] + 1,
            dirty_since=dirty_since,
            available_at=available,
            # Do not give a second worker the live lease. Its final CAS will detect the revision.
            execution="running"
            if row["execution"] == "running"
            else ("queued" if policy else "idle"),
            error_code=None,
        )
    )
    await session.execute(
        update(SUMMARY_TENANTS)
        .where(SUMMARY_TENANTS.c.tenant_id == tenant_id)
        .values(revision=SUMMARY_TENANTS.c.revision + 1)
    )
    if source_scope_id != "@tenant":
        tenant_scope = (
            (
                await session.execute(
                    select(SUMMARY_SCOPES).where(
                        SUMMARY_SCOPES.c.tenant_id == tenant_id,
                        SUMMARY_SCOPES.c.source_scope_id == "@tenant",
                    )
                )
            )
            .mappings()
            .one_or_none()
        )
        if tenant_scope is not None and tenant_scope["policy"]:
            await invalidate_summary_scope(session, tenant_id, "@tenant")
            if tenant_scope["execution"] == "idle":
                await session.execute(
                    update(SUMMARY_SCOPES)
                    .where(
                        SUMMARY_SCOPES.c.tenant_id == tenant_id,
                        SUMMARY_SCOPES.c.source_scope_id == "@tenant",
                    )
                    .values(execution="idle")
                )
