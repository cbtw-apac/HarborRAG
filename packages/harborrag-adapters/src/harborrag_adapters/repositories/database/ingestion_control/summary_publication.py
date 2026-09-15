"""Summary projection: publication operations."""

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from harborrag_core.contracts import HarborConflictError
from harborrag_core.ingestion import GraphNodeRecord
from harborrag_core.summaries import (
    SummaryBinding,
    SummaryLease,
    SummarySnapshot,
)

from .summary_authority import SummaryAuthority
from .summary_intent import lock_summary_tenant, upsert
from .summary_schema import SUMMARY_BINDINGS, SUMMARY_SCOPES
from .topology.transactions import topology_transaction


class SummaryPublicationOperations(SummaryAuthority):
    async def accept(
        self,
        lease: SummaryLease,
        snapshot: SummarySnapshot,
        binding: SummaryBinding,
        node: GraphNodeRecord,
    ) -> None:
        async with topology_transaction(self._client) as session:
            await lock_summary_tenant(session, lease.tenant_id)
            immutable = binding.manifest.kind in {"Structure", "DocumentVersion"}
            await self._require(session, lease, check_revision=not immutable)
            current = await self._snapshot(session, lease.tenant_id, lease.source_scope_id)
            self._validate_snapshot(current, snapshot, binding, node)
            if (
                binding.revision != lease.revision
                or binding.manifest.node_key != node.node_key
                or str(node.owner_id) != lease.tenant_id
                or binding.manifest.source_scope_id != lease.source_scope_id
                or binding.manifest.policy_fingerprint != lease.policy.fingerprint
                or binding.manifest.membership_digest != snapshot.membership_digest
                or any(
                    snapshot.document_versions.get(key) != value
                    for key, value in binding.manifest.input_document_versions.items()
                )
                or set(binding.manifest.permission_dependencies)
                != {
                    dep
                    for dep in snapshot.permission_dependencies
                    if dep.resource_kind == "source"
                    or dep.resource_id in binding.manifest.input_document_versions
                }
            ):
                raise HarborConflictError("summary binding does not match its authorized snapshot")
            if binding.manifest.child_keys:
                child_rows = (
                    (
                        await session.execute(
                            select(SUMMARY_BINDINGS.c.binding).where(
                                SUMMARY_BINDINGS.c.tenant_id == lease.tenant_id,
                                SUMMARY_BINDINGS.c.node_key.in_(binding.manifest.child_keys),
                            )
                        )
                    )
                    .scalars()
                    .all()
                )
                if len(child_rows) != len(binding.manifest.child_keys):
                    raise HarborConflictError("summary children are not ready")
                children = tuple(SummaryBinding.model_validate(value) for value in child_rows)
                if (
                    binding.manifest.child_artifact_hashes
                    != {child.manifest.node_key: child.artifact_hash for child in children}
                    or any(
                        binding.manifest.input_document_versions.get(key) != value
                        for child in children
                        for key, value in child.manifest.input_document_versions.items()
                    )
                    or not {key for child in children for key in child.manifest.input_chunk_ids}
                    <= set(binding.manifest.input_chunk_ids)
                ):
                    raise HarborConflictError(
                        "summary must preserve child artifacts and complete lineage"
                    )
                if lease.source_scope_id != "@tenant" and any(
                    child.revision != lease.revision
                    or child.manifest.membership_digest != snapshot.membership_digest
                    for child in children
                ):
                    raise HarborConflictError("summary children are stale")
                if lease.source_scope_id == "@tenant":
                    await self._validate_tenant_children(session, lease, children)
            values = {
                "tenant_id": lease.tenant_id,
                "node_key": node.node_key,
                "source_scope_id": lease.source_scope_id,
                "binding": binding.model_dump(mode="json"),
                "node": node.model_dump(mode="json"),
            }
            existing = (
                await session.execute(
                    select(SUMMARY_BINDINGS.c.binding).where(
                        SUMMARY_BINDINGS.c.tenant_id == lease.tenant_id,
                        SUMMARY_BINDINGS.c.node_key == node.node_key,
                    )
                )
            ).scalar_one_or_none()
            if existing is not None and existing["revision"] == binding.revision:
                prior = SummaryBinding.model_validate(existing)
                if prior.model_dump(exclude={"updated_at"}) != binding.model_dump(
                    exclude={"updated_at"}
                ):
                    raise HarborConflictError("accepted summary revision is immutable")
                return
            await session.execute(
                upsert(session)(SUMMARY_BINDINGS)
                .values(**values)
                .on_conflict_do_update(index_elements=["tenant_id", "node_key"], set_=values)
            )

    @staticmethod
    def _validate_snapshot(
        current: SummarySnapshot,
        snapshot: SummarySnapshot,
        binding: SummaryBinding,
        node: GraphNodeRecord,
    ) -> None:
        if node.node_kind.value != binding.manifest.kind:
            raise HarborConflictError("summary kind does not match node")
        if binding.manifest.kind not in {"Structure", "DocumentVersion"}:
            if current != snapshot:
                raise HarborConflictError("summary input snapshot changed")
            return
        versions = binding.manifest.input_document_versions
        if versions != {str(node.document_id): str(node.document_version_id)}:
            raise HarborConflictError("summary must bind its owning document version")
        if any(
            current.document_versions.get(key) != value for key, value in versions.items()
        ) or set(binding.manifest.permission_dependencies) != {
            dep
            for dep in current.permission_dependencies
            if dep.resource_kind == "source" or dep.resource_id in versions
        }:
            raise HarborConflictError("summary document snapshot changed")

    async def _validate_tenant_children(
        self, session: AsyncSession, lease: SummaryLease, children: tuple[SummaryBinding, ...]
    ) -> None:
        for child in children:
            scope = child.manifest.source_scope_id
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
            if (
                state is None
                or state["policy"] is None
                or child.revision != state["revision"]
                or child.manifest.membership_digest != snapshot.membership_digest
            ):
                raise HarborConflictError("tenant summary child is stale")
