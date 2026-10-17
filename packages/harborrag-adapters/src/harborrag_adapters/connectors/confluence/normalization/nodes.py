from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from types import MappingProxyType

# Kinds whose text belongs to the block that holds them: text runs, inline
# formatting, links and media. They join exactly as written; every other kind
# is a block and starts its own line.
INLINE_KINDS = frozenset({"text", "inline", "link", "media"})


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

    def visible_text(
        self,
        *,
        exclude_kinds: frozenset[str] = frozenset(),
        verbatim: bool = False,
    ) -> str:
        """Return visible text while allowing nested artifacts to stay separate.

        The root node's own kind is always included even if it appears in
        `exclude_kinds` — only descendants are pruned. This lets a caller
        aggregate a container's own text via `exclude_kinds` while excluding
        every independently-drafted descendant, including ones that share
        the root's own kind (e.g. a list nested inside a list).

        Inline children join exactly as written, so formatting never splits a
        sentence; each block child starts a new line. ``verbatim`` (implied for
        code blocks) keeps the first line's indentation and trims only blank
        leading lines and trailing whitespace.
        """

        text = self._joined_text(exclude_kinds)
        if verbatim or self.kind == "code_block":
            return text.lstrip("\n").rstrip()
        return text.strip()

    def _joined_text(self, exclude_kinds: frozenset[str]) -> str:
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
