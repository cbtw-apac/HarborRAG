from __future__ import annotations

import re
from typing import ClassVar, Literal

from harborrag_adapters.parsers.common.resources import (
    read_parse_input_bytes,
    read_parse_input_text,
)
from harborrag_adapters.parsers.common.utils import (
    get_parser_logger,
    input_label,
    parser_log_extra,
)
from harborrag_adapters.parsers.common.validation import guard_input_size
from harborrag_adapters.parsers.markup.base import HarborMarkupEngine
from harborrag_core.domain.element import DocumentElement
from harborrag_core.domain.parser import ParsedDocument, ParseInput

parser_logger = get_parser_logger("markdown")


class MarkdownMarkupEngine(HarborMarkupEngine):
    """Parse Markdown sources into normalized text and lightweight block elements."""

    parser_name: ClassVar[str] = "markdown"
    parser_engine: ClassVar[str] = "python/regex"
    suffixes: ClassVar[frozenset[str]] = frozenset({"md", "markdown", "mdx"})
    content_types: ClassVar[frozenset[str]] = frozenset({"text/markdown", "text/x-markdown"})

    def parse(self, input: ParseInput) -> ParsedDocument:
        """Extract searchable text while retaining headings, paragraphs, and code."""

        parse_input = self.coerce_input(input)
        parser_logger.debug(
            "Extracting Markdown text from %s",
            input_label(parse_input),
            extra=parser_log_extra(
                input=parse_input,
                parser_name=self.parser_name,
                parser_engine=self.parser_engine,
            ),
        )
        guard_input_size(read_parse_input_bytes(parse_input))
        markdown = read_parse_input_text(parse_input)
        elements = self._elements(markdown, parse_input.filename or "markdown")
        content = self._to_text(markdown)
        parser_logger.info(
            "Parsed Markdown %s content_chars=%d elements=%d",
            input_label(parse_input),
            len(content),
            len(elements),
            extra=parser_log_extra(
                input=parse_input,
                parser_name=self.parser_name,
                parser_engine=self.parser_engine,
                content_chars=len(content),
                elements=len(elements),
            ),
        )
        return ParsedDocument(
            content=content,
            elements=elements,
            parser_name=self.parser_name,
            parser_version=self.parser_version,
            metadata=self.metadata_for(parse_input),
            raw={"markdown": markdown},
        )

    @classmethod
    def _elements(cls, markdown: str, source_id: str) -> list[DocumentElement]:
        """Split Markdown into stable block elements without depending on HTML output."""

        elements: list[DocumentElement] = []
        block: list[str] = []
        in_code = False
        code_fence = ""
        lines = markdown.splitlines()

        index = 0
        while index < len(lines):
            line = lines[index]
            stripped = line.strip()
            if stripped.startswith(("```", "~~~")):
                fence = stripped[:3]
                if in_code and fence == code_fence:
                    cls._flush(elements, block, source_id, "code")
                    in_code = False
                    code_fence = ""
                elif not in_code:
                    cls._flush(elements, block, source_id)
                    in_code = True
                    code_fence = fence
                else:
                    block.append(line)
                index += 1
                continue

            if in_code:
                block.append(line)
                index += 1
                continue

            table = cls._markdown_table(lines, index)
            if table is not None:
                rows, next_index = table
                cls._flush(elements, block, source_id)
                elements.append(
                    DocumentElement(
                        id=f"{source_id}:{len(elements)}",
                        type="table",
                        content="\n".join("\t".join(row) for row in rows),
                        metadata={
                            "rows": len(rows),
                            "columns": max(map(len, rows)),
                            "header_rows": 1,
                            "table_format": "markdown",
                            "start_line": index + 1,
                            "end_line": next_index,
                        },
                    )
                )
                index = next_index
                continue

            cls._append_text_line(
                elements,
                block,
                line=line,
                source_id=source_id,
            )
            index += 1

        cls._flush(
            elements,
            block,
            source_id,
            "code" if in_code else "paragraph",
        )
        return elements

    @classmethod
    def _append_text_line(
        cls,
        elements: list[DocumentElement],
        block: list[str],
        *,
        line: str,
        source_id: str,
    ) -> None:
        stripped = line.strip()
        heading = re.match(r"^(#{1,6})\s+(.+)$", stripped)
        if heading:
            cls._flush(elements, block, source_id)
            elements.append(
                DocumentElement(
                    id=f"{source_id}:{len(elements)}",
                    type="heading",
                    content=cls._to_text(heading.group(2)),
                    metadata={"level": len(heading.group(1))},
                )
            )
        elif not stripped:
            cls._flush(elements, block, source_id)
        else:
            block.append(line)

    @classmethod
    def _flush(
        cls,
        elements: list[DocumentElement],
        block: list[str],
        source_id: str,
        kind: Literal["paragraph", "code"] = "paragraph",
    ) -> None:
        content = "\n".join(block).strip()
        if content:
            elements.append(
                DocumentElement(
                    id=f"{source_id}:{len(elements)}",
                    type=kind,
                    content=(cls._to_text(content) if kind == "paragraph" else content),
                )
            )
        block.clear()

    @classmethod
    def _markdown_table(
        cls,
        lines: list[str],
        start: int,
    ) -> tuple[list[list[str]], int] | None:
        if start + 1 >= len(lines) or "|" not in lines[start]:
            return None
        header = cls._table_row(lines[start])
        separator = cls._table_row(lines[start + 1])
        if (
            len(header) < 2
            or len(separator) != len(header)
            or not all(re.fullmatch(r":?-{3,}:?", cell.replace(" ", "")) for cell in separator)
        ):
            return None
        rows = [header]
        index = start + 2
        while index < len(lines):
            line = lines[index]
            if not line.strip() or "|" not in line:
                break
            row = cls._table_row(line)
            if len(row) != len(header):
                break
            rows.append(row)
            index += 1
        return rows, index

    @staticmethod
    def _table_row(line: str) -> list[str]:
        value = line.strip()
        if value.startswith("|"):
            value = value[1:]
        if value.endswith("|") and not value.endswith(r"\|"):
            value = value[:-1]
        return [cell.replace(r"\|", "|").strip() for cell in re.split(r"(?<!\\)\|", value)]

    @staticmethod
    def _to_text(markdown: str) -> str:
        """Remove common Markdown markup while preserving human-readable content."""

        text = markdown
        text = re.sub(r"!\[([^\]]*)\]\([^)]+\)", r"\1", text)
        text = re.sub(r"\[([^\]]+)\]\([^)]+\)", r"\1", text)
        text = re.sub(r"^\s{0,3}#{1,6}\s+", "", text, flags=re.MULTILINE)
        text = re.sub(r"^\s{0,3}>\s?", "", text, flags=re.MULTILINE)
        text = re.sub(r"^\s*[-*+]\s+", "", text, flags=re.MULTILINE)
        text = re.sub(r"^\s*\d+[.)]\s+", "", text, flags=re.MULTILINE)
        text = re.sub(r"[*_`~]", "", text)
        return "\n".join(line.strip() for line in text.splitlines()).strip()


MarkdownParser = MarkdownMarkupEngine
