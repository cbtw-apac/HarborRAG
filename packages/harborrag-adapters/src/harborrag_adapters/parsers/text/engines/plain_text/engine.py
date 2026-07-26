from __future__ import annotations

from typing import ClassVar

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
from harborrag_adapters.parsers.errors import ParseError
from harborrag_adapters.parsers.text.base import HarborTextEngine
from harborrag_core.domain.element import DocumentElement
from harborrag_core.domain.parser import ParsedDocument, ParseInput

parser_logger = get_parser_logger("text")


class PlainTextEngine(HarborTextEngine):
    """Fallback parser for plain text, source files, logs, and simple config files."""

    parser_name: ClassVar[str] = "text"
    parser_engine: ClassVar[str] = "python/text"
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
        """Decode text content, compact whitespace, and emit one paragraph element."""

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
        try:
            text = read_parse_input_text(parse_input)
        except UnicodeDecodeError as exc:
            raise ParseError(f"Could not decode text input: {exc}") from exc
        content = compact_text(text)
        elements = (
            [
                DocumentElement(
                    id="text:0",
                    type="paragraph",
                    content=content,
                    metadata={"filename": parse_input.filename},
                )
            ]
            if content
            else []
        )
        return ParsedDocument(
            content=content,
            elements=elements,
            parser_name=self.parser_name,
            parser_version=self.parser_version,
            metadata=self.metadata_for(parse_input),
        )


TextParser = PlainTextEngine
