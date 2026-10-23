"""Source-neutral structural node shared by the ADF and markup parsers."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from types import MappingProxyType

INLINE_KINDS = frozenset({"text", "inline", "link", "media"})


@dataclass(frozen=True, slots=True)
class ConfluenceNode:
    """Immutable node of the parsed page tree.

    Attributes:
        kind: Node type; kinds in ``INLINE_KINDS`` are inline, all others are blocks.
        source_id: Stable identifier of the node within the source body.
        text: The node's own text, before its children.
        attributes: Read-only parser attributes.
        children: Child nodes in document order.
    """

    kind: str
    source_id: str
    text: str = ""
    attributes: Mapping[str, object] = field(default_factory=dict)
    children: tuple[ConfluenceNode, ...] = ()

    def __post_init__(self) -> None:
        if not self.kind.strip() or not self.source_id.strip():
            raise ValueError("Confluence node kind and source_id must be non-empty")
        object.__setattr__(self, "attributes", MappingProxyType(dict(self.attributes)))

    def visible_text(
        self,
        *,
        exclude_kinds: frozenset[str] = frozenset(),
        verbatim: bool = False,
    ) -> str:
        """Return the node's visible text, leaving excluded descendants out.

        Inline children join as written; each block child starts a new line. Only
        descendants are pruned by ``exclude_kinds``, never the root itself.

        Args:
            exclude_kinds: Descendant kinds to skip, including nested ones of the root's kind.
            verbatim: Keep the first line's indentation and trim only blank leading lines
                and trailing whitespace (implied for code blocks).

        Returns:
            The stripped text.
        """

        text = self._joined_text(exclude_kinds)
        if verbatim or self.kind == "code_block":
            return text.lstrip("\n").rstrip()
        return text.strip()

    def _joined_text(self, exclude_kinds: frozenset[str]) -> str:
        """Join the node's text and non-excluded descendants without final stripping."""

        run = [self.text]
        lines: list[str] = []
        for child in self.children:
            if child.kind in exclude_kinds:
                continue
            child_text = child._joined_text(exclude_kinds)
            if child.kind in INLINE_KINDS:
                run.append(child_text)
                continue
            lines.append("".join(run).strip())
            if child.kind == "code_block":
                lines.append(child_text.lstrip("\n").rstrip())
            else:
                lines.append(child_text.strip())
            run = []
        if not lines:
            return "".join(run)
        lines.append("".join(run).strip())
        return "\n".join(line for line in lines if line)
