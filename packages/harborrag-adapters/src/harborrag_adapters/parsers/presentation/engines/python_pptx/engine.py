"""PowerPoint ``.pptx`` engine that keeps slide tables as Markdown tables.

Each slide becomes one paragraph element. A table's rows and columns reach chunk
context as a Markdown table instead of tab-separated lines.
"""

from __future__ import annotations

from collections.abc import Iterable
from io import BytesIO
from typing import TYPE_CHECKING, Any, ClassVar

from harborrag_adapters._shared.text.markdown_tables import TableCell, markdown_table
from harborrag_adapters._shared.text.normalization import compact_text
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
from harborrag_adapters.parsers.errors import ParseError
from harborrag_adapters.parsers.presentation.base import HarborPresentationEngine
from harborrag_core.domain.element import DocumentElement
from harborrag_core.domain.parser import ParsedDocument, ParseInput

if TYPE_CHECKING:
    from pptx.table import Table

parser_logger = get_parser_logger("pptx")


class PythonPptxPresentationEngine(HarborPresentationEngine):
    """Extract slide text, tables, and grouped-shape text from PowerPoint files.

    Attributes:
        supports_speaker_notes: The engine can read speaker notes.
        parser_name: Parser family name used for routing.
        parser_engine: Engine name recorded in parse metadata and errors.
        parser_version: Version recorded with each parse result.
        suffixes: File suffixes this engine accepts.
        content_types: Media types this engine accepts.
    """

    supports_speaker_notes: ClassVar[bool] = True

    parser_name: ClassVar[str] = "pptx"
    parser_engine: ClassVar[str] = "python-pptx"
    parser_version: ClassVar[str | None] = "1.1.0"
    suffixes: ClassVar[frozenset[str]] = frozenset({"pptx", "pptm"})
    content_types: ClassVar[frozenset[str]] = frozenset(
        {
            "application/vnd.openxmlformats-officedocument.presentationml.presentation",
            "application/vnd.ms-powerpoint.presentation.macroenabled.12",
        }
    )

    def parse(self, input: ParseInput) -> ParsedDocument:
        """Walk slides and return one document element per slide with text.

        Empty input succeeds with empty output like the other engines, because
        0 bytes is never a valid zip archive. An encrypted deck is an OLE
        compound file, so it is refused before the zip checks, which reject
        decompression-bomb shapes before python-pptx reads anything.

        Args:
            input: The ``.pptx`` bytes or path to parse.

        Returns:
            One paragraph element per slide that has text.

        Raises:
            ParseError: python-pptx is missing, or the deck is corrupt,
                encrypted or too large.
        """
        parse_input = self.coerce_input(input)
        try:
            from pptx import Presentation
        except ImportError as exc:
            parser_logger.error(
                "PPTX parser dependency `python-pptx` is missing",
                extra=parser_log_extra(
                    input=parse_input,
                    parser_name=self.parser_name,
                    parser_engine=self.parser_engine,
                ),
            )
            raise ParseError(
                "PPTX parsing requires `python-pptx`; install "
                "`harborrag-adapters[parsers]` or `pip install python-pptx`."
            ) from exc

        source_bytes = guard_input_size(read_parse_input_bytes(parse_input))
        if not source_bytes:
            return self.empty_result(parse_input, slide_count=0)
        parser_logger.debug(
            "Extracting PPTX text from %s",
            input_label(parse_input),
            extra=parser_log_extra(
                input=parse_input,
                parser_name=self.parser_name,
                parser_engine=self.parser_engine,
                input_bytes=len(source_bytes),
            ),
        )
        sections: list[str] = []
        elements: list[DocumentElement] = []
        with wrap_parse_errors(self.parser_engine):
            raise_if_password_protected_document(source_bytes, format_name="pptx")
            with open_guarded_zip(source_bytes) as archive:
                raise_if_password_protected_document(
                    source_bytes,
                    format_name="pptx",
                    archive=archive,
                )
            presentation = Presentation(BytesIO(source_bytes))
            for slide_index, slide in enumerate(presentation.slides, start=1):
                slide_lines = list(self._shape_text(slide.shapes))
                slide_content = compact_text("\n".join(slide_lines))
                if not slide_content:
                    continue
                sections.append(f"Slide {slide_index}\n{slide_content}")
                elements.append(
                    DocumentElement(
                        id=f"pptx:slide:{slide_index}",
                        type="paragraph",
                        content=slide_content,
                        metadata={"slide": slide_index},
                    )
                )

        content = "\n\n".join(sections).strip()
        parser_logger.info(
            "Parsed PPTX %s slides=%d content_chars=%d elements=%d",
            input_label(parse_input),
            len(presentation.slides),
            len(content),
            len(elements),
            extra=parser_log_extra(
                input=parse_input,
                parser_name=self.parser_name,
                parser_engine=self.parser_engine,
                input_bytes=len(source_bytes),
                slides=len(presentation.slides),
                content_chars=len(content),
                elements=len(elements),
            ),
        )
        return ParsedDocument(
            content=content,
            elements=elements,
            parser_name=self.parser_name,
            parser_version=self.parser_version,
            metadata=self.metadata_for(
                parse_input,
                slide_count=len(presentation.slides),
            ),
        )

    @classmethod
    def _shape_text(cls, shapes: Iterable[Any], depth: int = 0) -> Iterable[str]:
        """Yield text from shapes, tables, and nested groups in display order.

        A table is yielded with a blank line on each side, so Markdown does not
        read the text around it as more table rows.
        """
        for shape in shapes:
            if getattr(shape, "has_text_frame", False):
                for paragraph in shape.text_frame.paragraphs:
                    text = "".join(run.text for run in paragraph.runs).strip()
                    if not text:
                        text = paragraph.text.strip()
                    if text:
                        yield text

            if getattr(shape, "has_table", False):
                table_text = _markdown_table(shape.table)
                if table_text:
                    yield f"\n{table_text}\n"

            child_shapes = getattr(shape, "shapes", None)
            if child_shapes is not None and depth < 8:
                yield from cls._shape_text(child_shapes, depth + 1)


PptxParser = PythonPptxPresentationEngine


def _markdown_table(table: Table) -> str:
    """Render one slide table as a Markdown table, or ``""`` when it has no text.

    A merged cell keeps its text at its origin and names its spans. The cells it
    covers are left out, as the shared Markdown table renderer expects.
    """
    rows = [
        [
            TableCell(
                text=cell.text.strip(),
                row_span=cell.span_height if cell.is_merge_origin else 1,
                column_span=cell.span_width if cell.is_merge_origin else 1,
                header=table.first_row and row_index == 0,
            )
            for cell in row.cells
            if not cell.is_spanned
        ]
        for row_index, row in enumerate(table.rows)
    ]
    if not any(cell.text for row in rows for cell in row):
        return ""
    return markdown_table(rows)
