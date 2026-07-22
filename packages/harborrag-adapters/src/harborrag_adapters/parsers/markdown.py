from __future__ import annotations

import re
from typing import ClassVar, Literal

from harborrag_core.domain.element import DocumentElement
from harborrag_core.domain.parser import ParsedDocument, ParseInput

from .base import BaseParser
from .exceptions import ParseError
from .parser_logging import get_parser_logger, input_label, parser_log_extra
from .utils import guard_input_size

parser_logger = get_parser_logger("markdown")


class MarkdownParser(BaseParser[ParseInput, ParsedDocument]):
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
        guard_input_size(parse_input.read_bytes())
        try:
            markdown = parse_input.read_text()
        except UnicodeDecodeError as exc:
            raise ParseError(f"Could not decode Markdown input: {exc}") from exc
        elements = self._elements(markdown, parse_input.filename or "markdown")
        content = self._to_text(markdown)
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

        def flush(kind: Literal["paragraph", "code"] = "paragraph") -> None:
            nonlocal block
            content = "\n".join(block).strip()
            if content:
                elements.append(
                    DocumentElement(
                        id=f"{source_id}:{len(elements)}",
                        type=kind,
                        content=(cls._to_text(content) if kind == "paragraph" else content),
                    )
                )
            block = []

        for line in markdown.splitlines():
            stripped = line.strip()
            if stripped.startswith(("```", "~~~")):
                fence = stripped[:3]
                if in_code and fence == code_fence:
                    flush("code")
                    in_code = False
                    code_fence = ""
                elif not in_code:
                    flush()
                    in_code = True
                    code_fence = fence
                else:
                    block.append(line)
                continue

            if in_code:
                block.append(line)
                continue

            heading = re.match(r"^(#{1,6})\s+(.+)$", stripped)
            if heading:
                flush()
                elements.append(
                    DocumentElement(
                        id=f"{source_id}:{len(elements)}",
                        type="heading",
                        content=cls._to_text(heading.group(2)),
                        metadata={"level": len(heading.group(1))},
                    )
                )
                continue

            if not stripped:
                flush()
                continue

            block.append(line)

        flush("code" if in_code else "paragraph")
        return elements

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
