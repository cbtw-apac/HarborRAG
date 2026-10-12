"""Syntax-aware splitting for oversized code units.

The strongest strategy is cAST-style split-then-merge over a tree-sitter parse
(Zhang et al., "cAST", EMNLP Findings 2025): keep a syntax node whole when it
fits, otherwise descend into its children, and merge neighbouring siblings back
up to the limit. Three tiers are tried in order:

1. **Syntax tier** -- needs ``harborrag-adapters[chunking-code]`` and a known
   language. Cuts sit at the start of the line of each statement or member in
   a run of siblings; the deeper the run, the weaker the cut. The first member
   of a run is never cut away from its parent, so a header such as
   ``def a(self):`` always stays with its body, and comments, decorators and
   attributes stay with the definition that follows them. A tree with syntax
   errors is not trusted and falls through.
2. **Separator tier** -- definition keywords at line starts, then indented
   definitions, then blank lines, then line breaks.
3. The base class's token window, for a single line that alone is too long.

Each split records the signatures of the definitions that enclose it (for
example ``class Foo:`` / ``def a(self):``) in ``TextSplit.prefix``, so a piece
of a long method can still be found by the method's name.
"""

from __future__ import annotations

import importlib
import logging
import re
from bisect import bisect_right
from collections.abc import Callable, Sequence
from typing import Any

from harborrag_core.contracts.chunking import TextRefinementRequest

from ._base import BaseTextRefiner, Cut
from .languages import GRAMMAR_SET, GRAMMARS, normalize_language

logger = logging.getLogger(__name__)

ParserLoader = Callable[[str], Any | None]

_SIGNATURE_LIMIT = 120
_ATTACHED_TYPES = frozenset(
    {
        "comment",
        "line_comment",
        "block_comment",
        "decorator",
        "attribute",
        "attribute_item",
        "inner_attribute_item",
        "annotation",
        "marker_annotation",
        "attribute_list",
    }
)
_DEFINITION_SUFFIXES = ("_definition", "_declaration", "_item", "_specifier")
_DEFINITION_TYPES = frozenset(
    {"class", "module", "method", "singleton_method", "singleton_class", "arrow_function"}
)
_NOT_SCOPES = frozenset({"decorated_definition", "variable_declaration", "field_declaration"})

_DEFINITION_KEYWORDS = (
    r"(?:export\s+)?(?:default\s+)?(?:pub(?:\([^)]*\))?\s+)?(?:public\s+|private\s+|protected\s+"
    r"|internal\s+|static\s+|final\s+|abstract\s+|async\s+|override\s+|open\s+)*"
    r"(?:def|class|function|func|fn|interface|struct|enum|trait|impl|module|namespace"
    r"|record|fun|sub|proc|procedure)\b"
)
_TOP_DEFINITION = re.compile(rf"^(?:@[^\n]*\n)*{_DEFINITION_KEYWORDS}", re.MULTILINE)
_NESTED_DEFINITION = re.compile(rf"^[ \t]+(?:@[^\n]*\n[ \t]*)*{_DEFINITION_KEYWORDS}", re.MULTILINE)
_BLANK_LINES = re.compile(r"\n[ \t\r]*\n[ \t\r\n]*")
_LINE_BREAK = re.compile(r"\n")

SYNTAX_LEVEL_LIMIT = 64
_SEPARATOR_BASE = SYNTAX_LEVEL_LIMIT


def default_parser_loader(language: str) -> Any | None:
    """Build a tree-sitter parser for ``language`` from its bundled grammar wheel."""

    grammar = GRAMMARS.get(language)
    if grammar is None:
        return None
    try:
        from tree_sitter import Language, Parser

        module = importlib.import_module(grammar[0])
        return Parser(Language(getattr(module, grammar[1])()))
    except (ImportError, AttributeError, ValueError, TypeError, OSError):
        return None


class CodeStructureRefiner(BaseTextRefiner):
    """Split oversized code at syntax boundaries, keeping enclosing scope."""

    name = "code-structure"
    version = "1"

    def __init__(
        self,
        token_counter: Any,
        *,
        parser_loader: ParserLoader | None = None,
    ) -> None:
        super().__init__(token_counter)
        self._parser_loader = parser_loader or default_parser_loader
        self._parsers: dict[str, Any | None] = {}

    def fingerprint(self) -> str:
        return f"{super().fingerprint()}[{GRAMMAR_SET}]"

    def _propose(self, request: TextRefinementRequest) -> Sequence[Cut]:
        content = request.content
        language = normalize_language(request.language)
        syntax_cuts = self._syntax_cuts(content, language) if language else None
        separator_cuts = _separator_cuts(content)
        if syntax_cuts is None:
            return separator_cuts
        return [*syntax_cuts, *separator_cuts]

    def _parser(self, language: str) -> Any | None:
        if language not in self._parsers:
            self._parsers[language] = self._parser_loader(language)
        return self._parsers[language]

    def _syntax_cuts(self, content: str, language: str) -> list[Cut] | None:
        parser = self._parser(language)
        if parser is None:
            return None
        source = content.encode("utf-8")
        try:
            tree = parser.parse(source)
        except (ValueError, TypeError, RuntimeError):
            logger.debug("tree-sitter parse failed for %s; using separators", language)
            return None
        if tree.root_node.has_error:
            logger.debug("tree-sitter tree has errors for %s; using separators", language)
            return None
        return _SyntaxWalker(content, source).cuts(tree.root_node)


class _SyntaxWalker:
    """Collect sibling-run cuts and scopes from a parse tree."""

    def __init__(self, content: str, source: bytes) -> None:
        self._content = content
        self._source = source
        self._byte_starts = _char_byte_starts(content)

    def cuts(self, root: Any) -> list[Cut]:
        cuts: list[Cut] = []
        stack: list[tuple[Any, int, tuple[str, ...]]] = [(root, 0, ())]
        while stack:
            node, level, scope = stack.pop()
            children = [child for child in node.named_children if self._starts_line(child)]
            sequence = len(children) >= 2
            if sequence and level < SYNTAX_LEVEL_LIMIT:
                cuts.extend(self._sequence_cuts(node, children, level, scope))
            child_scope = scope
            if self._is_scope(node):
                child_scope = (*scope, self._signature(node))
            for child in reversed(node.named_children):
                stack.append((child, level + 1 if sequence else level, child_scope))
        return cuts

    def _sequence_cuts(
        self,
        parent: Any,
        children: list[Any],
        level: int,
        scope: tuple[str, ...],
    ) -> list[Cut]:
        inner_scope = (*scope, self._signature(parent)) if self._is_scope(parent) else scope
        cuts: list[Cut] = []
        for previous, child in zip(children, children[1:], strict=False):
            attached = (
                previous.type in _ATTACHED_TYPES
                and child.start_point[0] - previous.end_point[0] <= 1
            )
            if not attached:
                cuts.append(Cut(self._line_start(child), level, inner_scope))
        return cuts

    def _starts_line(self, node: Any) -> bool:
        start = self._offset(node.start_byte)
        line_start = self._content.rfind("\n", 0, start) + 1
        return not self._content[line_start:start].strip()

    def _line_start(self, node: Any) -> int:
        start = self._offset(node.start_byte)
        return self._content.rfind("\n", 0, start) + 1

    def _offset(self, byte_offset: int) -> int:
        return bisect_right(self._byte_starts, byte_offset) - 1

    @staticmethod
    def _is_scope(node: Any) -> bool:
        if node.parent is None:
            return False
        kind = node.type
        if kind in _NOT_SCOPES:
            return False
        return kind in _DEFINITION_TYPES or kind.endswith(_DEFINITION_SUFFIXES)

    def _signature(self, node: Any) -> str:
        start = self._line_start(node)
        end = self._content.find("\n", start)
        line = self._content[start : len(self._content) if end < 0 else end].strip()
        return line[:_SIGNATURE_LIMIT] or node.type


def _char_byte_starts(content: str) -> list[int]:
    """Byte offset at which each character starts, plus the total length."""

    starts: list[int] = []
    position = 0
    for character in content:
        starts.append(position)
        position += len(character.encode("utf-8"))
    starts.append(position)
    return starts


def _separator_cuts(content: str) -> list[Cut]:
    """Language-agnostic cuts at definition keywords, blank lines and lines."""

    cuts: list[Cut] = []
    for match in _TOP_DEFINITION.finditer(content):
        cuts.append(Cut(match.start(), _SEPARATOR_BASE))
    for match in _NESTED_DEFINITION.finditer(content):
        cuts.append(Cut(match.start(), _SEPARATOR_BASE + 1))
    for match in _BLANK_LINES.finditer(content):
        cuts.append(Cut(match.end(), _SEPARATOR_BASE + 2))
    for match in _LINE_BREAK.finditer(content):
        cuts.append(Cut(match.end(), _SEPARATOR_BASE + 3))
    return cuts
