"""In-memory KnowledgeGraphRepositoryPort, kept beside the port it implements.

The port is where the graph contract is written down, so this is where a change to it
gets its first exercise -- before any adapter, and without a FalkorDB. The conformance
binding at the bottom of the module is the part that catches a port method nobody
implemented: mypy checks it on every run.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field

from harborrag_core.ingestion import (
    GraphEdgeRecord,
    GraphNodeRecord,
    GraphOwnershipScope,
    GraphProjectionVerification,
    GraphSchemaMigrationVerification,
    KnowledgeGraphTraversal,
)
from harborrag_core.ports.indexing import KnowledgeGraphRepositoryPort
from harborrag_core.schemas.storage import StorageOperationContext


@dataclass(slots=True)
class FakeKnowledgeGraphRepository:
    """Hold one tenant's projection in two dicts, keyed the way the real store keys it.

    Nodes are keyed by ``node_key`` and relations by ``relation_id``, so writes merge and
    re-writes are idempotent, which is what the FalkorDB repository's ``MERGE`` gives.
    """

    nodes: dict[str, GraphNodeRecord] = field(default_factory=dict)
    relations: dict[str, GraphEdgeRecord] = field(default_factory=dict)
    retracted_relations: list[GraphEdgeRecord] = field(default_factory=list)

    async def connect(self) -> None:
        return None

    async def close(self) -> None:
        return None

    async def write_projection(
        self,
        nodes: Sequence[GraphNodeRecord],
        relations: Sequence[GraphEdgeRecord],
        *,
        context: StorageOperationContext,
    ) -> None:
        self._require_tenant(context, (node.owner_id for node in nodes))
        self._require_tenant(context, (relation.owner_id for relation in relations))
        self.nodes.update((node.node_key, node) for node in nodes)
        self.relations.update((relation.relation_id, relation) for relation in relations)

    async def verify_projection(
        self,
        nodes: Sequence[GraphNodeRecord],
        relations: Sequence[GraphEdgeRecord],
        *,
        context: StorageOperationContext,
    ) -> GraphProjectionVerification:
        del context
        missing_nodes = tuple(
            sorted(node.node_key for node in nodes if node.node_key not in self.nodes)
        )
        missing_relations = tuple(
            sorted(
                relation.relation_id
                for relation in relations
                if relation.relation_id not in self.relations
            )
        )
        return GraphProjectionVerification(
            valid=not (missing_nodes or missing_relations),
            expected_node_count=len(nodes),
            actual_node_count=len(nodes) - len(missing_nodes),
            expected_relation_count=len(relations),
            actual_relation_count=len(relations) - len(missing_relations),
            missing_node_keys=missing_nodes,
            missing_relation_ids=missing_relations,
        )

    async def traverse(
        self,
        start_node_key: str,
        *,
        max_depth: int,
        max_nodes: int,
        direction: str,
        context: StorageOperationContext,
    ) -> KnowledgeGraphTraversal:
        del max_depth, direction, context
        # One hop, undirected, which is all a fake needs to show that an edgeless node
        # reaches nothing -- the property the retraction contract below depends on.
        reached = tuple(
            relation
            for relation in self.relations.values()
            if start_node_key in (relation.source_node_key, relation.target_node_key)
        )
        keys = {start_node_key} | {
            node_key
            for relation in reached
            for node_key in (relation.source_node_key, relation.target_node_key)
        }
        if not reached:
            return KnowledgeGraphTraversal(nodes=(), relations=())
        nodes = tuple(self.nodes[key] for key in sorted(keys) if key in self.nodes)
        return KnowledgeGraphTraversal(
            nodes=nodes[:max_nodes],
            relations=reached,
            truncated=len(nodes) > max_nodes,
        )

    async def delete_relations(
        self,
        relations: Sequence[GraphEdgeRecord],
        *,
        context: StorageOperationContext,
    ) -> None:
        """Retract relations by relation_id and leave every node in place.

        Relations only, matching the FalkorDB repository. A far end left edgeless is not
        deleted, because deciding that from its degree races a concurrent projection that
        has staged the same placeholder without its relation yet; an edgeless node is
        unreachable by ``traverse`` above, so cleanup can reap it later.
        """

        self._require_tenant(context, (relation.owner_id for relation in relations))
        self.retracted_relations.extend(relations)
        for relation in relations:
            self.relations.pop(relation.relation_id, None)

    async def delete_version(
        self,
        document_version_id: str,
        *,
        context: StorageOperationContext,
    ) -> None:
        del context
        removed = {
            key
            for key, node in self.nodes.items()
            if str(node.document_version_id) == document_version_id
        }
        self.nodes = {key: node for key, node in self.nodes.items() if key not in removed}
        self._drop_relations_touching(removed)
        self._prune_orphans()

    async def delete_source_item(
        self,
        source_item_node_key: str,
        *,
        context: StorageOperationContext,
    ) -> None:
        del context
        self.nodes.pop(source_item_node_key, None)
        self._drop_relations_touching({source_item_node_key})
        self._prune_orphans()

    async def delete_source_scope(
        self,
        source_scope_id: str,
        *,
        context: StorageOperationContext,
    ) -> None:
        del context
        removed = {
            key for key, node in self.nodes.items() if node.source_scope_id == source_scope_id
        }
        self.nodes = {key: node for key, node in self.nodes.items() if key not in removed}
        self._drop_relations_touching(removed)

    async def verify_schema_v2_migration(
        self,
        *,
        evidence_chunk_ids: Sequence[str],
        active_source_item_node_keys: Sequence[str],
        context: StorageOperationContext,
    ) -> GraphSchemaMigrationVerification:
        del context
        missing = tuple(
            sorted(chunk_id for chunk_id in evidence_chunk_ids if chunk_id not in self.nodes)
        )
        invalid = tuple(
            sorted(key for key in active_source_item_node_keys if key not in self.nodes)
        )
        return GraphSchemaMigrationVerification(
            valid=not (missing or invalid),
            missing_chunk_ids=missing,
            invalid_source_item_ids=invalid,
        )

    async def delete_legacy_tenant_projection(
        self,
        *,
        context: StorageOperationContext,
    ) -> None:
        del context

    async def tenant_projection_counts(
        self,
        *,
        context: StorageOperationContext,
    ) -> tuple[int, int]:
        del context
        return (len(self.nodes), len(self.relations))

    async def delete_tenant_projection(
        self,
        *,
        context: StorageOperationContext,
    ) -> None:
        del context
        self.nodes.clear()
        self.relations.clear()

    def _drop_relations_touching(self, node_keys: set[str]) -> None:
        self.relations = {
            key: relation
            for key, relation in self.relations.items()
            if relation.source_node_key not in node_keys
            and relation.target_node_key not in node_keys
        }

    def _prune_orphans(self) -> None:
        """The tenant-wide sweep the cleanup paths run, and only they run it."""

        attached = {
            node_key
            for relation in self.relations.values()
            for node_key in (relation.source_node_key, relation.target_node_key)
        }
        self.nodes = {
            key: node
            for key, node in self.nodes.items()
            if key in attached or node.ownership_scope is not GraphOwnershipScope.SOURCE_SCOPE
        }

    @staticmethod
    def _require_tenant(context: StorageOperationContext, owners: Iterable[object]) -> None:
        """Reject a cross-tenant record, the way the FalkorDB repository does."""

        for owner_id in owners:
            if str(owner_id) != str(context.tenant_id):
                raise ValueError("graph record owner does not match storage tenant context")


# Checked by mypy on every run: a port method nobody implemented fails here, which is the
# point of keeping the fake in core rather than only in the adapter and runtime suites.
_CONFORMS: KnowledgeGraphRepositoryPort = FakeKnowledgeGraphRepository()

__all__ = ["FakeKnowledgeGraphRepository"]
