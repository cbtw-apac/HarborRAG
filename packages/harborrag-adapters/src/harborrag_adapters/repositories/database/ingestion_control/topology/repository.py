from __future__ import annotations

from sqlalchemy import select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert

from harborrag_adapters.repositories.backends.sqlalchemy import SQLAlchemyDBClient
from harborrag_core.topology import CONSERVATIVE_RESOLUTION_REVISION, TopologyPolicy

from .audit import TopologyAuditOperations
from .budget import TopologyBudgetOperations
from .builds import TopologyBuildOperations
from .configuration import IndexingConfigurationOperations
from .derivations import TopologyDerivationOperations
from .intent import enqueue_intent
from .jobs import TopologyJobOperations
from .permissions import TopologyPermissionOperations
from .readiness import TopologyReadinessOperations
from .reads import TopologyReader
from .reconciliation import missing_generations
from .resolution import TopologyResolutionOperations
from .resolution_state import lock_resolution_head
from .schema import TOPOLOGY_POLICIES
from .transactions import topology_transaction


class TopologyRepository(
    TopologyJobOperations,
    TopologyBuildOperations,
    TopologyReader,
    TopologyResolutionOperations,
    TopologyAuditOperations,
    IndexingConfigurationOperations,
    TopologyPermissionOperations,
    TopologyBudgetOperations,
    TopologyDerivationOperations,
    TopologyReadinessOperations,
):
    """Tenant-scoped authority; semantic publication is independent of ingestion."""

    def __init__(self, client: SQLAlchemyDBClient) -> None:
        self._client = client

    async def configure_policy(self, policy: TopologyPolicy) -> int:
        async with topology_transaction(self._client) as session:
            resolution_revision = await lock_resolution_head(session, policy.tenant_id)
            policy = policy.model_copy(
                update={
                    "resolution_revision": f"manual:{resolution_revision}"
                    if resolution_revision
                    else CONSERVATIVE_RESOLUTION_REVISION,
                }
            )
            serialized = policy.model_dump(mode="json")
            values = {
                "tenant_id": policy.tenant_id,
                "source_scope_id": policy.source_scope_id,
                "enabled": policy.enabled,
                "revision": 1,
                "fingerprint": policy.fingerprint,
                "policy": serialized,
            }
            factory = sqlite_insert if session.get_bind().dialect.name == "sqlite" else pg_insert
            await session.execute(
                factory(TOPOLOGY_POLICIES)
                .values(**values)
                .on_conflict_do_nothing(
                    index_elements=["tenant_id", "source_scope_id"],
                )
            )
            existing = (
                (
                    await session.execute(
                        select(TOPOLOGY_POLICIES)
                        .where(
                            TOPOLOGY_POLICIES.c.tenant_id == policy.tenant_id,
                            TOPOLOGY_POLICIES.c.source_scope_id == policy.source_scope_id,
                        )
                        .with_for_update()
                    )
                )
                .mappings()
                .one_or_none()
            )
            if existing is not None and existing["policy"] == serialized:
                return int(existing["revision"])
            revision = int(existing["revision"]) + 1 if existing is not None else 1
            values = {
                "tenant_id": policy.tenant_id,
                "source_scope_id": policy.source_scope_id,
                "enabled": policy.enabled,
                "revision": revision,
                "fingerprint": policy.fingerprint,
                "policy": serialized,
            }
            await session.execute(
                update(TOPOLOGY_POLICIES)
                .where(
                    TOPOLOGY_POLICIES.c.tenant_id == policy.tenant_id,
                    TOPOLOGY_POLICIES.c.source_scope_id == policy.source_scope_id,
                )
                .values(**values)
            )
        return revision

    async def reconcile(self, tenant_id: str, *, limit: int = 1000) -> int:
        """Backfill unchanged active documents without new document versions."""
        async with topology_transaction(self._client) as session:
            rows = (await session.execute(missing_generations(tenant_id, limit))).mappings().all()
            created = 0
            for document in rows:
                created += await enqueue_intent(
                    session, document, document["active_document_version_id"]
                )
        return created
