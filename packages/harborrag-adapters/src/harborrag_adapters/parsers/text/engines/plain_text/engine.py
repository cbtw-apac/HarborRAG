from __future__ import annotations

from typing import ClassVar

from harborrag_adapters.chunking.languages import guess_language
from harborrag_adapters.parsers.common.normalization import compact_text
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
from harborrag_adapters.parsers.text.base import HarborTextEngine
from harborrag_core.domain.element import DocumentElement
from harborrag_core.domain.parser import ParsedDocument, ParseInput

parser_logger = get_parser_logger("text")


class PlainTextEngine(HarborTextEngine):
    """Fallback parser for plain text, source files, logs, and simple config files."""

    parser_name: ClassVar[str] = "text"
    parser_engine: ClassVar[str] = "python/text"
    # 1.1.0: source files keep their indentation and become one ``code`` element
    # carrying ``code_language``, so chunking splits them at syntax boundaries.
    parser_version: ClassVar[str | None] = "1.1.0"
    suffixes: ClassVar[frozenset[str]] = frozenset(
        {
            "bat",
            "bash",
            "c",
            "cfg",
            "cmd",
            "conf",
            "cpp",
            "cs",
            "css",
            "go",
            "h",
            "hpp",
            "ini",
            "java",
            "js",
            "jsx",
            "kt",
            "kts",
            "less",
            "log",
            "php",
            "ps1",
            "py",
            "rb",
            "rs",
            "rst",
            "scala",
            "scss",
            "sh",
            "sql",
            "swift",
            "text",
            "toml",
            "ts",
            "tsx",
            "txt",
            "xml",
            "yaml",
            "yml",
            "zsh",
        }
    )
    content_types: ClassVar[frozenset[str]] = frozenset(
        {
            "application/toml",
            "application/x-sh",
            "application/x-yaml",
            "application/xml",
            "text/css",
            "text/plain",
            "text/xml",
            "text/x-python",
            "text/yaml",
        }
    )

    def parse(self, input: ParseInput) -> ParsedDocument:
        """Decode text and emit one element: ``code`` for source files, else ``paragraph``.

        Prose is compacted (lines trimmed, blank runs collapsed). Source code
        keeps its indentation, which is syntax in Python and YAML and is what
        code-aware chunking splits on; only trailing spaces and the blank lines
        around the file are dropped.
        """

        parse_input = self.coerce_input(input)
        parser_logger.debug(
            "Extracting plain text from %s",
            input_label(parse_input),
            extra=parser_log_extra(
                input=parse_input,
                parser_name=self.parser_name,
                parser_engine=self.parser_engine,
            ),
        )
        guard_input_size(read_parse_input_bytes(parse_input))
        text = read_parse_input_text(parse_input)
        language = guess_language(filename=parse_input.filename)
        content = _source_text(text) if language else compact_text(text)
        metadata: dict[str, object] = {"filename": parse_input.filename}
        if language:
            metadata["code_language"] = language
        elements = (
            [
                DocumentElement(
                    id="text:0",
                    type="code" if language else "paragraph",
                    content=content,
                    metadata=metadata,
                )
            ]
            if content
            else []
        )
        parser_logger.info(
            "Parsed text %s content_chars=%d elements=%d",
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
        )


def _source_text(text: str) -> str:
    """Keep source code as written, minus trailing spaces and surrounding blank lines."""

    return "\n".join(line.rstrip() for line in text.splitlines()).strip("\n")


TextParser = PlainTextEngine
