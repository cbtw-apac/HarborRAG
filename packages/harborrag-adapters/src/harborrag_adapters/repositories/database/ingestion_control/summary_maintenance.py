"""Summary projection: maintenance operations."""

from datetime import timedelta

from sqlalchemy import delete, select, true, update

from harborrag_core.base import utc_now
from harborrag_core.summaries import (
    SummaryBinding,
)

from .schema import DOCUMENTS
from .summary_authority import SummaryAuthority
from .summary_intent import invalidate_summary_scope, lock_summary_tenant
from .summary_schema import SUMMARY_BINDINGS, SUMMARY_CACHE, SUMMARY_SCOPES
from .topology.transactions import topology_transaction


class SummaryMaintenanceOperations(SummaryAuthority):
    async def status(self, tenant_id: str) -> tuple[dict, ...]:
        async with self._client.sessions() as session:
            rows = (
                (
                    await session.execute(
                        select(
                            SUMMARY_SCOPES.c.source_scope_id,
                            SUMMARY_SCOPES.c.revision,
                            SUMMARY_SCOPES.c.execution,
                            SUMMARY_SCOPES.c.error_code,
                            SUMMARY_SCOPES.c.dirty_since,
                            SUMMARY_SCOPES.c.available_at,
                        )
                        .where(SUMMARY_SCOPES.c.tenant_id == tenant_id)
                        .order_by(SUMMARY_SCOPES.c.source_scope_id)
                        .limit(100)
                    )
                )
                .mappings()
                .all()
            )
        return tuple(dict(row) for row in rows)

    async def backfill(self, tenant_id: str, source_scope_id: str | None = None) -> int:
        async with topology_transaction(self._client) as session:
            await lock_summary_tenant(session, tenant_id)
            scopes = (
                (
                    await session.execute(
                        select(SUMMARY_SCOPES.c.source_scope_id).where(
                            SUMMARY_SCOPES.c.tenant_id == tenant_id,
                            SUMMARY_SCOPES.c.policy.is_not(None),
                            SUMMARY_SCOPES.c.source_scope_id == source_scope_id
                            if source_scope_id
                            else true(),
                        )
                    )
                )
                .scalars()
                .all()
            )
            for scope in scopes:
                await invalidate_summary_scope(session, tenant_id, scope)
                await session.execute(
                    update(SUMMARY_SCOPES)
                    .where(
                        SUMMARY_SCOPES.c.tenant_id == tenant_id,
                        SUMMARY_SCOPES.c.source_scope_id == scope,
                        SUMMARY_SCOPES.c.execution != "running",
                    )
                    .values(execution="queued", available_at=utc_now())
                )
            return len(scopes)

    async def cleanup(
        self, tenant_id: str, *, retention_days: int = 30, apply: bool = False
    ) -> dict[str, object]:
        """Bounded GC of replaceable summaries; canonical documents remain untouched."""
        if retention_days < 1:
            raise ValueError("summary retention must be at least one day")
        async with topology_transaction(self._client) as session:
            await lock_summary_tenant(session, tenant_id)
            running = (
                await session.execute(
                    select(SUMMARY_SCOPES.c.source_scope_id)
                    .where(
                        SUMMARY_SCOPES.c.tenant_id == tenant_id,
                        SUMMARY_SCOPES.c.execution == "running",
                    )
                    .limit(1)
                )
            ).first()
            if running:
                return {"dry_run": not apply, "deferred": "summary_work_running", "removed": 0}
            cutoff = utc_now() - timedelta(days=retention_days)
            rows = (
                (
                    await session.execute(
                        select(SUMMARY_BINDINGS)
                        .where(SUMMARY_BINDINGS.c.tenant_id == tenant_id)
                        .limit(10001)
                    )
                )
                .mappings()
                .all()
            )
            if len(rows) > 10000:
                return {
                    "dry_run": not apply,
                    "deferred": "summary_binding_scan_limit",
                    "removed": 0,
                }
            active = dict(
                (
                    await session.execute(
                        select(
                            DOCUMENTS.c.document_id, DOCUMENTS.c.active_document_version_id
                        ).where(DOCUMENTS.c.tenant_id == tenant_id)
                    )
                )
                .tuples()
                .all()
            )
            bindings = tuple(SummaryBinding.model_validate(row["binding"]) for row in rows)
            retired = set(
                sorted(
                    {
                        binding.manifest.node_key
                        for binding in bindings
                        if binding.updated_at < cutoff
                        and any(
                            active.get(key) != value
                            for key, value in binding.manifest.input_document_versions.items()
                        )
                    }
                )[:1000]
            )
            retained = {
                binding.generation_key
                for binding in bindings
                if binding.manifest.node_key not in retired
            }
            keys = tuple(
                (
                    await session.execute(
                        select(SUMMARY_CACHE.c.generation_key)
                        .where(
                            SUMMARY_CACHE.c.tenant_id == tenant_id,
                            SUMMARY_CACHE.c.created_at < cutoff,
                            SUMMARY_CACHE.c.generation_key.not_in(retained),
                        )
                        .order_by(SUMMARY_CACHE.c.generation_key)
                        .limit(1000)
                    )
                )
                .scalars()
                .all()
            )
            if apply:
                await session.execute(
                    delete(SUMMARY_BINDINGS).where(
                        SUMMARY_BINDINGS.c.tenant_id == tenant_id,
                        SUMMARY_BINDINGS.c.node_key.in_(retired),
                    )
                )
                await session.execute(
                    delete(SUMMARY_CACHE).where(
                        SUMMARY_CACHE.c.tenant_id == tenant_id,
                        SUMMARY_CACHE.c.generation_key.in_(keys),
                    )
                )
            return {
                "dry_run": not apply,
                "binding_candidates": len(retired),
                "cache_candidates": len(keys),
                "removed": len(retired) + len(keys) if apply else 0,
                "canonical_artifacts_retained": True,
            }
