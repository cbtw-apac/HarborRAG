from __future__ import annotations

import csv
from io import StringIO
from typing import ClassVar

from harborrag_adapters.parsers.common.resources import (
    parse_input_suffix,
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
from harborrag_adapters.parsers.spreadsheet.base import HarborSpreadsheetEngine
from harborrag_core.domain.element import DocumentElement
from harborrag_core.domain.parser import ParsedDocument, ParseInput

parser_logger = get_parser_logger("csv")


class CsvSpreadsheetEngine(HarborSpreadsheetEngine):
    """Render CSV and TSV inputs as tab-separated table text."""

    parser_name: ClassVar[str] = "csv"
    parser_engine: ClassVar[str] = "python/csv"
    suffixes: ClassVar[frozenset[str]] = frozenset({"csv", "tsv"})
    content_types: ClassVar[frozenset[str]] = frozenset({"text/csv", "text/tab-separated-values"})

    def parse(self, input: ParseInput) -> ParsedDocument:
        """Sniff the dialect when possible and emit a single table element."""

        parse_input = self.coerce_input(input)
        parser_logger.debug(
            "Extracting CSV text from %s",
            input_label(parse_input),
            extra=parser_log_extra(
                input=parse_input,
                parser_name=self.parser_name,
                parser_engine=self.parser_engine,
            ),
        )
        guard_input_size(read_parse_input_bytes(parse_input))
        text = read_parse_input_text(parse_input)
        sample = text[:4096]
        try:
            dialect = csv.Sniffer().sniff(sample)
        except csv.Error:
            dialect = csv.excel_tab if parse_input_suffix(parse_input) == ".tsv" else csv.excel

        try:
            rows = list(csv.reader(StringIO(text), dialect=dialect))
        except csv.Error as exc:
            # Surface expected CSV failures so bulk callers can quarantine them.
            raise ParseError(f"Invalid CSV: {exc}") from exc
        rendered_rows = [
            "\t".join(cell.strip() for cell in row).rstrip()
            for row in rows
            if any(cell.strip() for cell in row)
        ]
        content = "\n".join(rendered_rows)
        elements = (
            [
                DocumentElement(
                    id="csv:0",
                    type="table",
                    content=content,
                    metadata={"rows": len(rendered_rows)},
                )
            ]
            if content
            else []
        )
        parser_logger.info(
            "Parsed CSV %s rows=%d content_chars=%d elements=%d",
            input_label(parse_input),
            len(rendered_rows),
            len(content),
            len(elements),
            extra=parser_log_extra(
                input=parse_input,
                parser_name=self.parser_name,
                parser_engine=self.parser_engine,
                rows=len(rendered_rows),
                content_chars=len(content),
                elements=len(elements),
            ),
        )
        return ParsedDocument(
            content=content,
            elements=elements,
            parser_name=self.parser_name,
            parser_version=self.parser_version,
            metadata=self.metadata_for(parse_input, rows=len(rendered_rows)),
        )


CsvParser = CsvSpreadsheetEngine
