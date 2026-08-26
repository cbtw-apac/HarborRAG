"""Shared construction helpers for provider graph source projectors."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from pathlib import PurePosixPath
from typing import Any

from harborrag_core.chunking import RelationType
from harborrag_core.domain.document import Document
from harborrag_core.ingestion import GraphEntityType, GraphNodeRecord

from .graph_state import GraphProjectionState, GraphRelationSpec


def source_entity_type(connector_type: str) -> GraphEntityType:
    return {
        "confluence": GraphEntityType.CONFLUENCE_PAGE,
        "jira": GraphEntityType.JIRA_ISSUE,
        "github": GraphEntityType.GITHUB_FILE,
        "sharepoint": GraphEntityType.SHAREPOINT_FILE,
        "local": GraphEntityType.LOCAL_FILE,
    }.get(connector_type.casefold(), GraphEntityType.GENERIC_SOURCE_ITEM)


def attachment_entity_type(connector_type: str) -> GraphEntityType:
    return {
        "confluence": GraphEntityType.CONFLUENCE_ATTACHMENT,
        "jira": GraphEntityType.JIRA_ATTACHMENT,
    }.get(connector_type.casefold(), GraphEntityType.GENERIC_SOURCE_ITEM)


def relation_entity_type(
    connector_type: str,
    relation_type: RelationType,
    target_type: str,
    *,
    reverse: bool = False,
) -> GraphEntityType:
    """Type the far end of a source relation, honouring the projected direction."""

    # HAS_ATTACHMENT is projected as container -> attachment, so only its forward
    # direction puts an attachment at the far end. A reversed edge was normalized from
    # `attached_to`, where the far end is the *container*: typing that as an attachment
    # puts entity_type into its node key that no concrete projection ever produces, so
    # the real page or issue stays unreachable behind an orphaned stub.
    if (relation_type == RelationType.HAS_ATTACHMENT and not reverse) or (
        "attachment" in target_type.casefold()
    ):
        return attachment_entity_type(connector_type)
    return source_entity_type(connector_type)


def project_structure_chain(
    projector: Any,
    state: GraphProjectionState,
    *,
    container: GraphNodeRecord,
    item: GraphNodeRecord,
    ancestors: Sequence[GraphNodeRecord],
) -> None:
    """Chain the structure axis from a container down to one item.

    Two axes carry two different questions. CONTAINS is *membership*: which documents
    belong to this container, flat and single-typed, so counting them is one hop. This is
    *structure*: where an item sits, which is deliberately heterogeneous -- directories,
    folders and parent pages all appear on it.

    They used to be the same edge, so a container's children mixed documents with the
    nodes above them and "how many pages in this space" needed an entity-type filter and a
    guess at depth. Neither axis derives from the other: a page is a member of its space
    *and* a child of its parent page.

    An item with no ancestors is chained straight from the container, so the tree is always
    walkable from the top whether or not anything sits in between.
    """

    parent = container
    for ancestor in ancestors:
        projector.edge(state, RelationType.PARENT_OF, parent, ancestor)
        parent = ancestor
    projector.edge(state, RelationType.PARENT_OF, parent, item)


def source_provider_id(connector_type: str, source_item_id: str) -> str:
    """Recover the provider ID/path carried by a canonical source item identity."""

    connector = connector_type.casefold()
    value = source_item_id.strip()
    if connector in {"confluence", "sharepoint"} and "/" in value:
        return value.rstrip("/").rsplit("/", 1)[-1]
    if connector == "jira" and value.casefold().startswith("jira://"):
        return value.rstrip("/").rsplit("/", 1)[-1]
    if connector == "github" and value.casefold().startswith("github://"):
        parts = value.removeprefix("github://").split("/", 2)
        return parts[2] if len(parts) == 3 else value
    return value


def source_item_provider_id(state: GraphProjectionState) -> str:
    """Reduce this document's canonical source item identity to its provider id.

    The fallback every provider projector reaches for when its own metadata key is
    missing. For several connectors that identity is a ``scheme://scope/id`` URI, so it
    has to be reduced the same way ``SourceRelationProjector`` reduces a relation target:
    an attachment dispatched as its own source item carries no page or issue key of its
    own, and keying it by the whole composite URI leaves it on a different node from the
    one its parent's attachment list already created for it.
    """

    return source_provider_id(
        state.context.connector_type.value,
        state.context.source_item_id,
    )


class BaseSourceProjector:
    """Common node and relation operations used by provider projectors."""

    entity_type = GraphEntityType.GENERIC_SOURCE_ITEM

    def source_item(  # noqa: PLR0913
        self,
        state: GraphProjectionState,
        document: Document,
        *,
        provider_id: str | None = None,
        title: str | None = None,
        attributes: dict[str, Any] | None = None,
        entity_type: GraphEntityType | None = None,
    ) -> GraphNodeRecord:
        # A caller-supplied provider_id is already provider-native (a path, an item id, a
        # page id); None means the projector found no such key and wants the fallback.
        return state.source_node(
            entity_type or self.entity_type,
            provider_id or source_item_provider_id(state),
            title=title or document.title,
            attributes=attributes,
        )

    @staticmethod
    def edge(
        state: GraphProjectionState,
        relation_type: RelationType,
        source: GraphNodeRecord,
        target: GraphNodeRecord,
        *,
        explicit: bool = False,
    ) -> None:
        state.relation(
            GraphRelationSpec(
                relation_type=relation_type,
                source=source,
                target=target,
                source_explicit=explicit,
            )
        )

    def version(
        self,
        state: GraphProjectionState,
        source_item: GraphNodeRecord,
        document_version: GraphNodeRecord,
    ) -> None:
        self.edge(state, RelationType.HAS_VERSION, source_item, document_version)


def text_value(values: Mapping[str, Any], *keys: str) -> str | None:
    for key in keys:
        value = values.get(key)
        if value is not None and str(value).strip():
            return str(value).strip()
    return None


def selected_values(values: Mapping[str, Any], *keys: str) -> dict[str, Any]:
    return {key: display_value(values[key]) for key in keys if values.get(key) is not None}


def display_value(value: Any) -> str | int | float | bool:
    if isinstance(value, (str, int, float, bool)):
        return value
    isoformat = getattr(value, "isoformat", None)
    if callable(isoformat):
        return str(isoformat())
    return str(value)


def mapping_value(value: object) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def mapping_sequence(value: object) -> tuple[Mapping[str, Any], ...]:
    if not isinstance(value, (list, tuple)):
        return ()
    return tuple(item for item in value if isinstance(item, Mapping))


def text_sequence(value: object) -> tuple[str, ...]:
    if not isinstance(value, (list, tuple)):
        return ()
    return tuple(str(item).strip() for item in value if str(item).strip())


def portable_path(value: str) -> str:
    normalized = value.replace("\\", "/").lstrip("/")
    parts = tuple(part for part in PurePosixPath(normalized).parts if part not in {"", "."})
    if not parts or ".." in parts:
        raise ValueError("provider graph path must be a portable relative path")
    return "/".join(parts)


def is_attachment(state: GraphProjectionState, extra: Mapping[str, Any]) -> bool:
    return (
        state.context.document_kind.value == "attachment"
        or str(extra.get("binding_kind") or "").casefold() == "attachment"
    )
