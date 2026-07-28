from __future__ import annotations

from collections.abc import Mapping

from harborrag_core.schemas.graph import GraphEdge, GraphNode

from .schemas import GraphMutationPlan, GraphValidationResult


class GraphValidationService:
    """Validate graph counts, identities, hierarchy, ordering, and capsules."""

    def validate(
        self,
        plan: GraphMutationPlan,
        nodes: tuple[GraphNode, ...],
        edges: tuple[GraphEdge, ...],
    ) -> GraphValidationResult:
        """Validate persisted graph records against the staged plan."""

        errors: list[str] = []
        expected_nodes = {str(node.id): node for node in plan.nodes}
        expected_edges = {str(edge.id): edge for edge in plan.edges}
        actual_nodes = {str(node.id): node for node in nodes}
        actual_edges = {str(edge.id): edge for edge in edges}
        if len(actual_nodes) != len(nodes):
            errors.append("graph repository returned duplicate node IDs")
        if len(actual_edges) != len(edges):
            errors.append("graph repository returned duplicate edge IDs")
        self._identity_errors("node", expected_nodes, actual_nodes, errors)
        self._identity_errors("edge", expected_edges, actual_edges, errors)

        for node_id in sorted(set(expected_nodes) & set(actual_nodes)):
            planned = expected_nodes[node_id]
            stored = actual_nodes[node_id]
            if stored.labels != planned.labels:
                errors.append(f"graph node {node_id} labels do not match")
            for key, value in planned.properties.items():
                if stored.properties.get(key) != value:
                    errors.append(f"graph node {node_id} property {key!r} does not match")
            self._generation_properties(node_id, stored.properties, plan, errors)
            if "Chunk" in stored.labels:
                self._capsule(node_id, stored, plan, errors)

        node_ids = set(expected_nodes)
        chunk_ids = set(plan.chunk_node_ids)
        order_edges = 0
        for edge_id in sorted(set(expected_edges) & set(actual_edges)):
            planned_edge = expected_edges[edge_id]
            stored_edge = actual_edges[edge_id]
            if (
                stored_edge.source_id != planned_edge.source_id
                or stored_edge.target_id != planned_edge.target_id
                or stored_edge.relationship_type != planned_edge.relationship_type
            ):
                errors.append(f"graph edge {edge_id} structure does not match")
            if (
                str(stored_edge.source_id) not in node_ids
                or str(stored_edge.target_id) not in node_ids
            ):
                errors.append(f"graph edge {edge_id} has an invalid parent reference")
            self._generation_properties(edge_id, stored_edge.properties, plan, errors)
            if stored_edge.relationship_type in {"PREVIOUS_CHUNK", "NEXT_CHUNK"}:
                order_edges += 1
                if (
                    str(stored_edge.source_id) not in chunk_ids
                    or str(stored_edge.target_id) not in chunk_ids
                    or stored_edge.source_id == stored_edge.target_id
                ):
                    errors.append(f"graph edge {edge_id} has invalid chunk order")
        expected_order_edges = max(len(chunk_ids) - 1, 0) * 2
        if order_edges != expected_order_edges:
            errors.append("graph chunk order edge count does not match")

        return GraphValidationResult(
            valid=not errors,
            checked_node_count=len(nodes),
            checked_edge_count=len(edges),
            errors=tuple(errors),
        )

    @staticmethod
    def _identity_errors(
        label: str,
        expected: Mapping[str, object],
        actual: Mapping[str, object],
        errors: list[str],
    ) -> None:
        missing = sorted(set(expected) - set(actual))
        unexpected = sorted(set(actual) - set(expected))
        if missing:
            errors.append(f"graph {label}s are missing: {', '.join(missing)}")
        if unexpected:
            errors.append(f"unexpected graph {label}s were returned: {', '.join(unexpected)}")

    @staticmethod
    def _generation_properties(
        identity: str,
        properties: Mapping[str, object],
        plan: GraphMutationPlan,
        errors: list[str],
    ) -> None:
        expected = {
            "artifact_id": plan.artifact_id,
            "artifact_revision_id": plan.artifact_revision_id,
            "generation_id": plan.generation_id,
            "index_state": "staged",
            "is_active": False,
        }
        for key, value in expected.items():
            if properties.get(key) != value:
                errors.append(f"graph record {identity} property {key!r} is invalid")

    @staticmethod
    def _capsule(
        node_id: str,
        node: GraphNode,
        plan: GraphMutationPlan,
        errors: list[str],
    ) -> None:
        preview = node.properties.get("preview")
        if not isinstance(preview, str):
            errors.append(f"graph chunk {node_id} has no bounded preview")
        elif len(preview) > plan.capsule_maximum_characters:
            errors.append(f"graph chunk {node_id} preview exceeds configured bound")
        if "content" in node.properties:
            errors.append(f"graph chunk {node_id} contains unrestricted content")
        for field in ("logical_chunk_id", "chunk_revision_id", "content_hash"):
            if not node.properties.get(field):
                errors.append(f"graph chunk {node_id} capsule is missing {field}")
