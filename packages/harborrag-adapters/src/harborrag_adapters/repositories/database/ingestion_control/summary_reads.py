"""Summary projection: reads operations."""

from sqlalchemy import select, tuple_, update
from sqlalchemy.ext.asyncio import AsyncSession

from harborrag_core.base import utc_now
from harborrag_core.contracts import HarborConflictError
from harborrag_core.ingestion import GraphNodeRecord
from harborrag_core.security.context import AccessContext
from harborrag_core.summaries import (
    SummaryBinding,
    SummaryPolicy,
    SummarySnapshot,
    SummaryView,
)
from harborrag_core.topology.permissions import ResolvedPermissionSnapshot

from .summary_authority import SummaryAuthority
from .summary_intent import lock_summary_tenant
from .summary_schema import SUMMARY_BINDINGS, SUMMARY_SCOPES
from .topology.configuration import lock_indexing_config
from .topology.policy_schema import PERMISSION_SNAPSHOTS
from .topology.transactions import topology_transaction


class SummaryReadOperations(SummaryAuthority):
    async def retained_nodes(
        self, tenant_id: str, source_scope_id: str
    ) -> tuple[GraphNodeRecord, ...]:
        async with self._client.sessions() as session:
            rows = (
                (
                    await session.execute(
                        select(SUMMARY_BINDINGS.c.node).where(
                            SUMMARY_BINDINGS.c.tenant_id == tenant_id,
                            SUMMARY_BINDINGS.c.source_scope_id == source_scope_id,
                        )
                    )
                )
                .scalars()
                .all()
            )
        return tuple(GraphNodeRecord.model_validate(row) for row in rows)

    async def views(
        self,
        tenant_id: str,
        node_keys: tuple[str, ...],
        *,
        access: AccessContext,
        source_scopes: dict[str, str] | None = None,
    ) -> dict[str, SummaryView]:
        if str(access.tenant_id) != tenant_id or len(node_keys) > 1000:
            return {}
        async with topology_transaction(self._client) as session:
            await lock_summary_tenant(session, tenant_id)
            state = await lock_indexing_config(session, tenant_id)
            if state.config.prohibited:
                return {key: SummaryView() for key in node_keys}
            if source_scopes and "@tenant" in source_scopes.values():
                await self._request_tenant(session, tenant_id, access)
            rows = (
                (
                    await session.execute(
                        select(
                            SUMMARY_BINDINGS,
                            SUMMARY_SCOPES.c.revision,
                            SUMMARY_SCOPES.c.execution,
                            SUMMARY_SCOPES.c.policy,
                        )
                        .join(
                            SUMMARY_SCOPES,
                            (SUMMARY_SCOPES.c.tenant_id == SUMMARY_BINDINGS.c.tenant_id)
                            & (
                                SUMMARY_SCOPES.c.source_scope_id
                                == SUMMARY_BINDINGS.c.source_scope_id
                            ),
                        )
                        .where(
                            SUMMARY_BINDINGS.c.tenant_id == tenant_id,
                            SUMMARY_BINDINGS.c.node_key.in_(node_keys),
                        )
                    )
                )
                .mappings()
                .all()
            )
            result = {row["node_key"]: SummaryView() for row in rows}
            snapshots: dict[str, SummarySnapshot | None] = {}
            permissions = (
                (
                    await session.execute(
                        select(PERMISSION_SNAPSHOTS.c.snapshot).where(
                            PERMISSION_SNAPSHOTS.c.tenant_id == tenant_id
                        )
                    )
                )
                .scalars()
                .all()
            )
            acl = {
                (value.resource_kind, value.resource_id): value
                for value in (
                    ResolvedPermissionSnapshot.model_validate(item) for item in permissions
                )
            }
            for row in rows:
                if row["policy"] is None:
                    continue
                binding = SummaryBinding.model_validate(row["binding"])
                deps = binding.manifest.permission_dependencies
                if not deps or any(
                    (value := acl.get((dep.resource_kind, dep.resource_id))) is None
                    or value.revision != dep.revision
                    or not value.can_read(access, now=utc_now())
                    for dep in deps
                ):
                    continue
                scope = row["source_scope_id"]
                if scope not in snapshots:
                    try:
                        snapshots[scope] = await self._snapshot(session, tenant_id, scope)
                    except HarborConflictError:
                        snapshots[scope] = None
                snapshot = snapshots[scope]
                versions_valid = snapshot is not None and all(
                    snapshot.document_versions.get(key) == value
                    for key, value in binding.manifest.input_document_versions.items()
                )
                current = (
                    versions_valid
                    and snapshot is not None
                    and (
                        binding.manifest.kind in {"Structure", "DocumentVersion"}
                        or (
                            binding.revision == row["revision"]
                            and binding.manifest.membership_digest == snapshot.membership_digest
                        )
                    )
                    and binding.manifest.policy_fingerprint
                    == SummaryPolicy.model_validate(row["policy"]).fingerprint
                )
                # Retired inputs must not leak through old ancestor descriptions.
                result[row["node_key"]] = SummaryView(
                    status="current" if current else "stale",
                    execution=row["execution"],
                    card=binding.card if versions_valid else None,
                    coverage_mode=binding.coverage_mode if versions_valid else None,
                    included_chunks=len(binding.manifest.input_chunk_ids)
                    if versions_valid
                    else None,
                    child_count=len(binding.manifest.child_keys) if versions_valid else None,
                    artifact_hash=binding.artifact_hash if versions_valid else None,
                    revision=binding.revision,
                    updated_at=binding.updated_at,
                )
            if source_scopes:
                await self._pending_views(session, tenant_id, source_scopes, result)
            return result

    async def _request_tenant(
        self, session: AsyncSession, tenant_id: str, access: AccessContext
    ) -> None:
        try:
            snapshot = await self._snapshot(session, tenant_id, "@tenant")
        except HarborConflictError:
            return
        required = [
            (value.resource_kind, value.resource_id) for value in snapshot.permission_dependencies
        ]
        permissions = (
            (
                await session.execute(
                    select(PERMISSION_SNAPSHOTS.c.snapshot).where(
                        PERMISSION_SNAPSHOTS.c.tenant_id == tenant_id,
                        tuple_(
                            PERMISSION_SNAPSHOTS.c.resource_kind, PERMISSION_SNAPSHOTS.c.resource_id
                        ).in_(required),
                    )
                )
            )
            .scalars()
            .all()
        )
        if any(
            not ResolvedPermissionSnapshot.model_validate(value).can_read(access, now=utc_now())
            for value in permissions
        ):
            return
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
        if (
            tenant_scope
            and tenant_scope["policy"]
            and tenant_scope["dirty_since"]
            and tenant_scope["execution"] == "idle"
        ):
            await session.execute(
                update(SUMMARY_SCOPES)
                .where(
                    SUMMARY_SCOPES.c.tenant_id == tenant_id,
                    SUMMARY_SCOPES.c.source_scope_id == "@tenant",
                )
                .values(execution="queued", available_at=utc_now())
            )

    @staticmethod
    async def _pending_views(
        session: AsyncSession,
        tenant_id: str,
        source_scopes: dict[str, str],
        result: dict[str, SummaryView],
    ) -> None:
        scopes = (
            (
                await session.execute(
                    select(SUMMARY_SCOPES).where(
                        SUMMARY_SCOPES.c.tenant_id == tenant_id,
                        SUMMARY_SCOPES.c.source_scope_id.in_(set(source_scopes.values())),
                        SUMMARY_SCOPES.c.policy.is_not(None),
                    )
                )
            )
            .mappings()
            .all()
        )
        states = {row["source_scope_id"]: row for row in scopes}
        for key, scope in source_scopes.items():
            if key not in result and scope in states:
                row = states[scope]
                result[key] = SummaryView(status="pending", execution=row["execution"])
