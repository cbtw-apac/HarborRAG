from __future__ import annotations

from pathlib import Path
from tempfile import NamedTemporaryFile
from typing import ClassVar

from harborrag_core.domain.element import DocumentElement
from harborrag_core.domain.parser import ParsedDocument, ParseInput

from .base import BaseParser
from .exceptions import ParseError
from .parser_logging import get_parser_logger, input_label, parser_log_extra
from .utils import compact_text, guard_input_size, open_guarded_zip, wrap_parse_errors

parser_logger = get_parser_logger("docx")


class DocxParser(BaseParser[ParseInput, ParsedDocument]):
    """Extract text from Word `.docx` files with docx2txt."""

    parser_name: ClassVar[str] = "docx"
    parser_engine: ClassVar[str] = "docx2txt"
    suffixes: ClassVar[frozenset[str]] = frozenset({"docx"})
    content_types: ClassVar[frozenset[str]] = frozenset(
        {"application/vnd.openxmlformats-officedocument.wordprocessingml.document"}
    )

    def parse(self, input: ParseInput) -> ParsedDocument:
        """Materialize DOCX bytes to a temp path because docx2txt expects a file."""

        parse_input = self.coerce_input(input)
        try:
            import docx2txt
        except ImportError as exc:
            parser_logger.error(
                "DOCX parser dependency `docx2txt` is missing",
                extra=parser_log_extra(
                    input=parse_input,
                    parser_name=self.parser_name,
                    parser_engine=self.parser_engine,
                ),
            )
            raise ParseError(
                "DOCX parsing requires `docx2txt`; install `harborrag-adapters[parsers]` "
                "or `pip install docx2txt`."
            ) from exc

        tmp_path: Path | None = None
        try:
            source_bytes = guard_input_size(parse_input.read_bytes())
            parser_logger.debug(
                "Extracting DOCX text from %s",
                input_label(parse_input),
                extra=parser_log_extra(
                    input=parse_input,
                    parser_name=self.parser_name,
                    parser_engine=self.parser_engine,
                    input_bytes=len(source_bytes),
                ),
            )
            with wrap_parse_errors(self.parser_engine):
                open_guarded_zip(source_bytes).close()
                with NamedTemporaryFile(delete=False, suffix=".docx") as tmp:
                    tmp.write(source_bytes)
                    tmp_path = Path(tmp.name)
                content = compact_text(docx2txt.process(str(tmp_path)) or "")
        finally:
            if tmp_path is not None:
                tmp_path.unlink(missing_ok=True)

        elements = (
            [
                DocumentElement(
                    id="docx:0",
                    type="paragraph",
                    content=content,
                    metadata={"filename": parse_input.filename},
                )
            ]
            if content
            else []
        )
        parser_logger.info(
            "Parsed DOCX %s content_chars=%d elements=%d",
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
