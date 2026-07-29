from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from types import MappingProxyType


@dataclass(frozen=True, slots=True)
class ConfluenceNode:
    """Source-neutral structural node shared by ADF and markup traversal."""

    kind: str
    source_id: str
    text: str = ""
    attributes: Mapping[str, object] = field(default_factory=dict)
    children: tuple[ConfluenceNode, ...] = ()

    def __post_init__(self) -> None:
        if not self.kind.strip() or not self.source_id.strip():
            raise ValueError("Confluence node kind and source_id must be non-empty")
        object.__setattr__(self, "attributes", MappingProxyType(dict(self.attributes)))

    def visible_text(self, *, exclude_kinds: frozenset[str] = frozenset()) -> str:
        """Return visible text while allowing nested artifacts to stay separate."""

        if self.kind in exclude_kinds:
            return ""
        parts = [self.text]
        parts.extend(child.visible_text(exclude_kinds=exclude_kinds) for child in self.children)
        separator = "\n" if self.kind in _BLOCK_KINDS else ""
        return separator.join(part for part in parts if part).strip()


_BLOCK_KINDS = frozenset(
    {
        "paragraph",
        "heading",
        "list",
        "list_item",
        "code_block",
        "quote",
        "panel",
        "expand",
        "tab_set",
        "tab",
        "table",
        "table_row",
        "table_cell",
        "table_header",
    }
)
