from __future__ import annotations

from io import BytesIO
from typing import Any, ClassVar

from harborrag_adapters.parsers.common.normalization import compact_text
from harborrag_adapters.parsers.common.resources import read_parse_input_bytes
from harborrag_adapters.parsers.common.utils import (
    get_parser_logger,
    input_label,
    parser_log_extra,
)
from harborrag_adapters.parsers.common.validation import (
    guard_input_size,
    open_guarded_zip,
    raise_if_password_protected_document,
    wrap_parse_errors,
)
from harborrag_adapters.parsers.document.base import HarborDocumentEngine
from harborrag_adapters.parsers.errors import ParseError
from harborrag_core.domain.element import DocumentElement
from harborrag_core.domain.parser import ParsedDocument, ParseInput

parser_logger = get_parser_logger("odt")

_TEXT_ELEMENT_NAMES = frozenset({"p", "h"})


def _iter_paragraphs(node: Any) -> Any:
    """Walk an odfpy element tree in document order, yielding paragraph/heading nodes."""
    for child in node.childNodes:
        qname = getattr(child, "qname", None)
        if qname and qname[1] in _TEXT_ELEMENT_NAMES:
            yield child
        elif hasattr(child, "childNodes"):
            yield from _iter_paragraphs(child)


class OdtDocumentEngine(HarborDocumentEngine):
    """Extract text from OpenDocument Text `.odt` files with odfpy."""

    parser_name: ClassVar[str] = "odt"
    parser_engine: ClassVar[str] = "odfpy"
    suffixes: ClassVar[frozenset[str]] = frozenset({"odt"})
    content_types: ClassVar[frozenset[str]] = frozenset({"application/vnd.oasis.opendocument.text"})

    def parse(self, input: ParseInput) -> ParsedDocument:
        """Extract paragraph/heading text from an ODT container in document order."""

        parse_input = self.coerce_input(input)
        try:
            from odf import teletype
            from odf.opendocument import load
        except ImportError as exc:
            parser_logger.error(
                "ODT parser dependency `odfpy` is missing",
                extra=parser_log_extra(
                    input=parse_input,
                    parser_name=self.parser_name,
                    parser_engine=self.parser_engine,
                ),
            )
            raise ParseError(
                "ODT parsing requires `odfpy`; install `harborrag-adapters[parsers]` "
                "or `pip install odfpy`."
            ) from exc

        source_bytes = guard_input_size(read_parse_input_bytes(parse_input))
        parser_logger.debug(
            "Extracting ODT text from %s",
            input_label(parse_input),
            extra=parser_log_extra(
                input=parse_input,
                parser_name=self.parser_name,
                parser_engine=self.parser_engine,
                input_bytes=len(source_bytes),
            ),
        )
        with wrap_parse_errors(self.parser_engine):
            raise_if_password_protected_document(source_bytes, format_name="odt")
            with open_guarded_zip(source_bytes) as archive:
                raise_if_password_protected_document(
                    source_bytes,
                    format_name="odt",
                    archive=archive,
                )
            document = load(BytesIO(source_bytes))
            paragraphs = [teletype.extractText(node) for node in _iter_paragraphs(document.text)]
        content = compact_text("\n".join(text for text in paragraphs if text))

        elements = (
            [
                DocumentElement(
                    id="odt:0",
                    type="paragraph",
                    content=content,
                    metadata={"filename": parse_input.filename},
                )
            ]
            if content
            else []
        )
        parser_logger.info(
            "Parsed ODT %s content_chars=%d elements=%d",
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


OdtParser = OdtDocumentEngine
