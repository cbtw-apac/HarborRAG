"""In-memory storage fakes shared by ingestion tests."""

from __future__ import annotations

from harborrag_core.ingestion import GraphProjectionVerification
from harborrag_core.schemas.vector import VectorIndexScanPage


class InMemoryVectorRepository:
    def __init__(self) -> None:
        self.points: dict[str, dict[str, object]] = {}

    async def ensure_index(self, spec, *, context) -> None:
        del context
        self.points.setdefault(spec.index_name, {})

    async def upsert_records(self, collection, points, *, context) -> None:
        del context
        self.points.setdefault(collection, {}).update((point.id, point) for point in points)

    async def get_records(self, collection, ids, *, context):
        del context
        values = self.points.get(collection, {})
        return [values[point_id] for point_id in ids if point_id in values]

    async def delete_records(self, collection, ids, *, context) -> None:
        del context
        for point_id in ids:
            self.points.get(collection, {}).pop(point_id, None)

    async def scan_records(self, collection, *, limit, cursor, filters=None, context):
        del context
        records = list(self.points.get(collection, {}).values())
        if filters is not None:
            versions = set(filters.must[0].value)
            records = [
                record
                for record in records
                if record.payload.get("document_version_id") in versions
            ]
        start = int(cursor or 0)
        end = min(len(records), start + limit)
        return VectorIndexScanPage(
            records=records[start:end],
            next_cursor=str(end) if end < len(records) else None,
        )


class InMemoryKnowledgeGraph:
    def __init__(self) -> None:
        self.nodes = {}
        self.relations = {}
        self.write_batches = []
        self.fail_writes = False

    async def write_projection(self, nodes, relations, *, context) -> None:
        del context
        if self.fail_writes:
            raise ConnectionError("graph unavailable")
        self.write_batches.append((tuple(nodes), tuple(relations)))
        self.nodes.update((node.node_key, node) for node in nodes)
        self.relations.update((relation.relation_id, relation) for relation in relations)

    async def verify_projection(
        self,
        nodes,
        relations,
        *,
        context,
    ) -> GraphProjectionVerification:
        del context
        expected_nodes = {node.node_key for node in nodes}
        expected_relations = {relation.relation_id for relation in relations}
        actual_nodes = expected_nodes & self.nodes.keys()
        actual_relations = expected_relations & self.relations.keys()
        missing_nodes = tuple(sorted(expected_nodes - actual_nodes))
        missing_relations = tuple(sorted(expected_relations - actual_relations))
        return GraphProjectionVerification(
            valid=not any((missing_nodes, missing_relations)),
            expected_node_count=len(nodes),
            actual_node_count=len(actual_nodes),
            expected_relation_count=len(relations),
            actual_relation_count=len(actual_relations),
            missing_node_keys=missing_nodes,
            missing_relation_ids=missing_relations,
        )

    async def delete_version(
        self,
        document_version_id,
        *,
        context,
    ) -> None:
        del context
        removed_node_keys = {
            key
            for key, node in self.nodes.items()
            if str(node.document_version_id) == document_version_id
        }
        self.nodes = {key: node for key, node in self.nodes.items() if key not in removed_node_keys}
        self.relations = {
            key: relation
            for key, relation in self.relations.items()
            if str(relation.document_version_id) != document_version_id
            and relation.source_node_key not in removed_node_keys
            and relation.target_node_key not in removed_node_keys
        }
