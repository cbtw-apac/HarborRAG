from __future__ import annotations

from typing import Any, ClassVar

from harborrag_adapters.parsers.common.legacy_office import (
    BINARY_DECODE_ERRORS,
    ContainerKind,
    import_olefile,
    open_ole,
    raise_if_ole_encrypted_package,
    raise_unsupported_container,
    read_ole_stream,
    sniff_container,
)
from harborrag_adapters.parsers.common.resources import read_parse_input_bytes
from harborrag_adapters.parsers.common.utils import (
    get_parser_logger,
    input_label,
    parser_log_extra,
)
from harborrag_adapters.parsers.common.validation import ParseResourceBudget, guard_input_size
from harborrag_adapters.parsers.document.base import HarborDocumentEngine
from harborrag_adapters.parsers.document.engines.docx.engine import DocxDocumentEngine
from harborrag_adapters.parsers.document.engines.msword.text import story_blocks, story_text
from harborrag_adapters.parsers.document.engines.msword.word97 import (
    read_characters,
    read_fib,
    read_pieces,
    split_stories,
    total_story_characters,
)
from harborrag_adapters.parsers.errors import ParseError, UnsupportedFormatError
from harborrag_core.domain.element import DocumentElement
from harborrag_core.domain.parser import ParsedDocument, ParseInput

parser_logger = get_parser_logger("doc")

# Stories rendered as separate elements after the main text, in this order.
_SECONDARY_STORIES = ("header", "textbox", "header_textbox", "footnote", "endnote", "comment")


class MsWordBinaryDocumentEngine(HarborDocumentEngine):
    """Extract text from legacy Word 97-2003 binary `.doc` files with olefile."""

    parser_name: ClassVar[str] = "doc"
    parser_engine: ClassVar[str] = "olefile"
    # Not ".dot": that suffix is also a Graphviz graph, which must keep reaching the
    # text family. A Word template still routes here by its MIME type.
    suffixes: ClassVar[frozenset[str]] = frozenset({"doc"})
    content_types: ClassVar[frozenset[str]] = frozenset({"application/msword"})

    def parse(self, input: ParseInput) -> ParsedDocument:
        """Sniff the real container, then decode the Word binary piece table."""

        parse_input = self.coerce_input(input)
        source_bytes = guard_input_size(read_parse_input_bytes(parse_input))
        if not source_bytes:
            return self.empty_result(parse_input)
        kind = sniff_container(source_bytes)
        if kind is ContainerKind.ZIP:
            # A renamed OOXML package: the DOCX engine owns that container.
            return DocxDocumentEngine().parse(parse_input)
        if kind is not ContainerKind.OLE:
            raise_unsupported_container(kind, format_name="Word .doc")
        olefile = import_olefile("Legacy Word .doc")
        parser_logger.debug(
            "Extracting Word binary text from %s",
            input_label(parse_input),
            extra=parser_log_extra(
                input=parse_input,
                parser_name=self.parser_name,
                parser_engine=self.parser_engine,
                input_bytes=len(source_bytes),
            ),
        )
        try:
            stories, n_fib = self._read_stories(olefile, source_bytes)
        except ParseError:
            raise
        except BINARY_DECODE_ERRORS as exc:
            raise ParseError(f"{self.parser_engine} failed to parse Word document: {exc}") from exc

        elements = self._elements(stories)
        content = "\n\n".join(element.content or "" for element in elements)
        ParseResourceBudget().consume_output(len(content))
        parser_logger.info(
            "Parsed DOC %s content_chars=%d elements=%d",
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
            metadata=self.metadata_for(parse_input, word_nfib=n_fib),
        )

    @staticmethod
    def _read_stories(olefile: Any, source_bytes: bytes) -> tuple[dict[str, str], int]:
        limit = len(source_bytes)
        with open_ole(olefile, source_bytes) as ole:
            if not ole.exists("WordDocument"):
                raise_if_ole_encrypted_package(ole, format_name="DOC")
                raise UnsupportedFormatError(
                    "Compound file has no WordDocument stream; it is not a Word document."
                )
            word_stream = read_ole_stream(ole, "WordDocument", max_bytes=limit)
            fib = read_fib(word_stream)
            if not ole.exists(fib.table_stream):
                raise ParseError(f"Word document is missing its {fib.table_stream} stream")
            table_stream = read_ole_stream(ole, fib.table_stream, max_bytes=limit)
        pieces = read_pieces(table_stream, fib)
        cp_limit = total_story_characters(fib)
        if cp_limit > len(word_stream):
            # Every CP needs at least one byte of the WordDocument stream.
            raise ParseError("FIB character counts exceed the WordDocument stream size")
        text = read_characters(word_stream, pieces, cp_limit)
        return dict(split_stories(fib, text)), fib.n_fib

    @staticmethod
    def _elements(stories: dict[str, str]) -> list[DocumentElement]:
        elements: list[DocumentElement] = []
        for block in story_blocks(stories.get("main", "")):
            content = block.render()
            if not content:
                continue
            metadata: dict[str, Any] = {"part": "body"}
            if block.kind == "table":
                metadata.update(rows=len(block.rows), columns=len(block.rows[0]))
            elements.append(
                DocumentElement(
                    id=f"doc:{len(elements)}",
                    type="table" if block.kind == "table" else "paragraph",
                    content=content,
                    metadata=metadata,
                )
            )
        for part in _SECONDARY_STORIES:
            content = story_text(stories.get(part, ""))
            if content:
                elements.append(
                    DocumentElement(
                        id=f"doc:{len(elements)}",
                        type="paragraph",
                        content=content,
                        metadata={"part": part},
                    )
                )
        return elements


MsWordParser = MsWordBinaryDocumentEngine
