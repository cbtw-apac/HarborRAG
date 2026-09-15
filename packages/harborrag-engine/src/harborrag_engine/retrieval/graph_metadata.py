"""Present mutable source metadata only from accepted document supports."""

from __future__ import annotations

from collections.abc import Sequence

from harborrag_core.ingestion import GraphEdgeRecord, GraphNodeRecord, KnowledgeNodeKind


def supported_nodes(
    nodes: Sequence[GraphNodeRecord],
    relations: Sequence[GraphEdgeRecord],
) -> tuple[GraphNodeRecord, ...]:
    """Choose a deterministic observation from the already-authorized support set.

    Concrete observations outrank placeholders; ties use support identity. This
    is a display reduction, not entity resolution or a last-write publication.
    Old shared properties are never trusted as a fallback during migration.
    """

    observations: dict[str, tuple[tuple[bool, str], dict[str, object]]] = {}
    for relation in relations:
        for role, key in (
            ("source", relation.source_node_key),
            ("target", relation.target_node_key),
        ):
            value = relation.attributes.get(f"{role}_metadata")
            if not isinstance(value, dict):
                continue
            attributes = value.get("attributes", {})
            if not isinstance(attributes, dict):
                continue
            priority = (attributes.get("placeholder") is True, relation.relation_id)
            if key not in observations or priority < observations[key][0]:
                observations[key] = (priority, value)
    result = []
    for node in nodes:
        if node.node_kind != KnowledgeNodeKind.SOURCE_ENTITY:
            result.append(node)
            continue
        observation = observations.get(node.node_key)
        value = observation[1] if observation is not None else {}
        result.append(
            node.model_copy(
                update={
                    "title": value.get("title") or node.logical_id[:512],
                    "attributes": value.get("attributes", {}),
                }
            )
        )
    return tuple(result)


def selector_matches(node: GraphNodeRecord, selector: str | None) -> bool:
    """Recheck title selectors after discarding unaccepted metadata observations."""

    if (
        node.node_kind != KnowledgeNodeKind.SOURCE_ENTITY
        or selector is None
        or selector in {node.node_key, node.logical_id}
    ):
        return True
    return node.title is not None and node.title.casefold() == selector.casefold()
