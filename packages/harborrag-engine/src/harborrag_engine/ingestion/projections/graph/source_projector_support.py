"""Shared construction helpers for provider graph source projectors."""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import PurePosixPath
from typing import Any

from harborrag_core.chunking import RelationType
from harborrag_core.domain.document import Document
from harborrag_core.ingestion import GraphEntityType, GraphNodeRecord

from .graph_state import GraphProjectionState, GraphRelationSpec


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
        return state.source_node(
            entity_type or self.entity_type,
            provider_id or state.context.source_item_id,
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
