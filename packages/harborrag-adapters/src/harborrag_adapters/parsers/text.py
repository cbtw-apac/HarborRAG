from __future__ import annotations

from typing import ClassVar

from harborrag_core.domain.element import DocumentElement
from harborrag_core.domain.parser import ParsedDocument, ParseInput

from .base import BaseParser
from .parser_logging import get_parser_logger, input_label, parser_log_extra
from .utils import compact_text


parser_logger = get_parser_logger("text")


class TextParser(BaseParser[ParseInput, ParsedDocument]):
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
        content = compact_text(parse_input.read_text())
        elements = [
            DocumentElement(
                id="text:0",
                type="paragraph",
                content=content,
                metadata={"filename": parse_input.filename},
            )
        ] if content else []
        return ParsedDocument(
            content=content,
            elements=elements,
            parser_name=self.parser_name,
            parser_version=self.parser_version,
            metadata=self.metadata_for(parse_input),
        )
