"""Turn a Word binary character stream into paragraphs and tables.

Word stores structure in-band with special characters ([MS-DOC] 2.8.x):
``\\r`` ends a paragraph, ``\\x07`` ends a table cell (and, doubled, a table
row), ``\\x0b`` is a manual line break and ``\\x13``/``\\x14``/``\\x15``
delimit field codes, whose instruction part is hidden and whose result part
is the visible text.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from harborrag_adapters.parsers.common.normalization import compact_text

FIELD_BEGIN = "\x13"
FIELD_SEPARATOR = "\x14"
FIELD_END = "\x15"
CELL_MARK = "\x07"
_PARAGRAPH_MARKS = frozenset({"\r", "\x0c"})

_INLINE_TRANSLATION: dict[int, str | None] = {
    code: None for code in range(0x20) if chr(code) not in {"\t", "\n"}
}
_INLINE_TRANSLATION.update(
    {
        0x0B: "\n",  # manual line break
        0x1E: "-",  # non-breaking hyphen
        0xA0: " ",  # non-breaking space
        0xFFFD: None,  # undecodable character
    }
)


@dataclass(slots=True)
class Block:
    """A run of paragraphs, or one table, in document order."""

    kind: str
    paragraphs: list[str] = field(default_factory=list)
    rows: list[list[str]] = field(default_factory=list)

    def render(self) -> str:
        if self.kind == "table":
            return "\n".join("\t".join(row) for row in self.rows)
        return compact_text("\n".join(self.paragraphs))


def strip_fields(text: str) -> str:
    """Drop field instructions while keeping each field's displayed result.

    Fields nest, so a stack records whether each open field has reached its
    separator. Characters are kept only when every open field is showing its
    result. Unbalanced separators/ends from damaged files are ignored.
    """

    kept: list[str] = []
    stack: list[bool] = []
    hidden = 0
    for char in text:
        if char == FIELD_BEGIN:
            stack.append(False)
            hidden += 1
        elif char == FIELD_SEPARATOR:
            if stack and not stack[-1]:
                stack[-1] = True
                hidden -= 1
        elif char == FIELD_END:
            if stack and not stack.pop():
                hidden -= 1
        elif not hidden:
            kept.append(char)
    return "".join(kept)


def clean_inline(text: str) -> str:
    """Map Word inline control characters to plain text."""

    return text.translate(_INLINE_TRANSLATION)


def _clean_cell(text: str) -> str:
    return " ".join(clean_inline(text).split())


class _BlockBuilder:
    """Accumulate characters into paragraph runs and tables."""

    def __init__(self) -> None:
        self.blocks: list[Block] = []
        self.paragraphs: list[str] = []
        self.rows: list[list[str]] = []
        self.row: list[str] = []
        self.buffer: list[str] = []
        self.previous = ""

    def feed(self, char: str) -> None:
        if char == CELL_MARK:
            self._end_cell()
        elif char in _PARAGRAPH_MARKS:
            if self.row:
                # A paragraph break inside a table cell keeps the cell open.
                self.buffer.append("\n")
            else:
                self._end_paragraph()
        else:
            self.buffer.append(char)
        self.previous = char

    def _end_cell(self) -> None:
        text = "".join(self.buffer)
        self.buffer.clear()
        if not text and self.previous == CELL_MARK and self.row:
            # The second consecutive mark is the end-of-row marker.
            self.rows.append(self.row)
            self.row = []
            return
        if not self.rows and not self.row:
            self._flush_paragraphs()
        self.row.append(_clean_cell(text))

    def _end_paragraph(self) -> None:
        text = clean_inline("".join(self.buffer))
        self.buffer.clear()
        if self.rows:
            self._flush_table()
        self.paragraphs.append(text)

    def _flush_paragraphs(self) -> None:
        if any(paragraph.strip() for paragraph in self.paragraphs):
            self.blocks.append(Block("paragraphs", paragraphs=self.paragraphs))
        self.paragraphs = []

    def _flush_table(self) -> None:
        rows = [row for row in self.rows if any(cell for cell in row)]
        self.rows = []
        if not rows:
            return
        width = max(len(row) for row in rows)
        padded = [row + [""] * (width - len(row)) for row in rows]
        self.blocks.append(Block("table", rows=padded))

    def finish(self) -> list[Block]:
        if self.buffer and self.row:
            self._end_cell()
        if self.row:
            self.rows.append(self.row)
            self.row = []
        if self.buffer:
            self._end_paragraph()
        if self.rows:
            self._flush_paragraphs()
            self._flush_table()
        self._flush_paragraphs()
        return self.blocks


def story_blocks(story: str) -> list[Block]:
    """Split one decoded story into paragraph runs and tables."""

    builder = _BlockBuilder()
    for char in strip_fields(story):
        builder.feed(char)
    return builder.finish()


def story_text(story: str) -> str:
    """Render a whole story as plain text, tables as tab-separated rows."""

    rendered = (block.render() for block in story_blocks(story))
    return compact_text("\n\n".join(text for text in rendered if text))
