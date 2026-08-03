from __future__ import annotations

from typing import ClassVar

from harborrag_adapters.parsers.common.normalization import compact_text
from harborrag_adapters.parsers.common.resources import (
    read_parse_input_bytes,
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
        data = guard_input_size(read_parse_input_bytes(parse_input))
        try:
            if isinstance(parse_input.content, str):
                text = parse_input.content
            elif data.startswith((b"\xff\xfe", b"\xfe\xff")):
                text = data.decode("utf-16")
            else:
                # Plain text is a deterministic UTF input boundary. Falling
                # back to statistical single-byte encodings can turn corrupt
                # UTF-8 into valid-looking but incorrect Cyrillic text.
                text = data.decode("utf-8-sig")
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


TextParser = PlainTextEngine
