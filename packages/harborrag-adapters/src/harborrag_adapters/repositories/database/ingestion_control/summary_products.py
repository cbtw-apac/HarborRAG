"""Summary projection: products operations."""

from sqlalchemy import select

from harborrag_core.contracts import HarborConflictError
from harborrag_core.ingestion import GraphNodeRecord
from harborrag_core.summaries import (
    SummaryBinding,
    SummaryLease,
    SummaryPolicy,
)

from .schema import DOCUMENTS
from .summary_authority import SummaryAuthority
from .summary_intent import lock_summary_tenant
from .summary_schema import SUMMARY_BINDINGS, SUMMARY_SCOPES
from .topology.configuration import lock_indexing_config
from .topology.transactions import topology_transaction


class SummaryProductOperations(SummaryAuthority):
    async def document_bindings(
        self, tenant_id: str, document_id: str, document_version_id: str
    ) -> tuple[tuple[GraphNodeRecord, SummaryBinding], ...]:
        """Worker-only input for projections; callers use the permission-checked views API."""
        async with topology_transaction(self._client) as session:
            await lock_summary_tenant(session, tenant_id)
            state = await lock_indexing_config(session, tenant_id)
            if state.config.prohibited:
                return ()
            scope = (
                await session.execute(
                    select(DOCUMENTS.c.source_scope_id).where(
                        DOCUMENTS.c.tenant_id == tenant_id,
                        DOCUMENTS.c.document_id == document_id,
                        DOCUMENTS.c.active_document_version_id == document_version_id,
                    )
                )
            ).scalar_one_or_none()
            if scope is None:
                return ()
            snapshot = await self._snapshot(session, tenant_id, scope)
            policy_row = (
                (
                    await session.execute(
                        select(SUMMARY_SCOPES).where(
                            SUMMARY_SCOPES.c.tenant_id == tenant_id,
                            SUMMARY_SCOPES.c.source_scope_id == scope,
                        )
                    )
                )
                .mappings()
                .one_or_none()
            )
            if policy_row is None or policy_row["policy"] is None:
                return ()
            rows = (
                (
                    await session.execute(
                        select(SUMMARY_BINDINGS).where(
                            SUMMARY_BINDINGS.c.tenant_id == tenant_id,
                            SUMMARY_BINDINGS.c.source_scope_id == scope,
                        )
                    )
                )
                .mappings()
                .all()
            )
            records = []
            for row in rows:
                node = GraphNodeRecord.model_validate(row["node"])
                binding = SummaryBinding.model_validate(row["binding"])
                if (
                    str(node.document_version_id) == document_version_id
                    and binding.manifest.kind in {"Structure", "DocumentVersion"}
                    and binding.manifest.policy_fingerprint
                    == SummaryPolicy.model_validate(policy_row["policy"]).fingerprint
                    and binding.manifest.input_document_versions
                    == {document_id: document_version_id}
                    and set(binding.manifest.permission_dependencies)
                    == {
                        dep
                        for dep in snapshot.permission_dependencies
                        if dep.resource_kind == "source" or dep.resource_id == document_id
                    }
                ):
                    records.append((node, binding))
            return tuple(records)

    async def tenant_inputs(self, lease: SummaryLease) -> tuple[SummaryBinding, ...]:
        async with topology_transaction(self._client) as session:
            await lock_summary_tenant(session, lease.tenant_id)
            await self._require(session, lease)
            scopes = await self._source_ids(session, lease.tenant_id)
            inputs = []
            for scope in scopes:
                state = (
                    (
                        await session.execute(
                            select(SUMMARY_SCOPES).where(
                                SUMMARY_SCOPES.c.tenant_id == lease.tenant_id,
                                SUMMARY_SCOPES.c.source_scope_id == scope,
                            )
                        )
                    )
                    .mappings()
                    .one_or_none()
                )
                snapshot = await self._snapshot(session, lease.tenant_id, scope)
                rows = (
                    (
                        await session.execute(
                            select(SUMMARY_BINDINGS.c.binding).where(
                                SUMMARY_BINDINGS.c.tenant_id == lease.tenant_id,
                                SUMMARY_BINDINGS.c.source_scope_id == scope,
                            )
                        )
                    )
                    .scalars()
                    .all()
                )
                cards = [
                    SummaryBinding.model_validate(row)
                    for row in rows
                    if row["manifest"]["kind"] == "DataSource"
                ]
                if (
                    state is None
                    or state["policy"] is None
                    or len(cards) != 1
                    or cards[0].revision != state["revision"]
                    or cards[0].manifest.membership_digest != snapshot.membership_digest
                ):
                    raise HarborConflictError(
                        "tenant summary waits for current datasource summaries"
                    )
                inputs.append(cards[0])
            return tuple(inputs)
