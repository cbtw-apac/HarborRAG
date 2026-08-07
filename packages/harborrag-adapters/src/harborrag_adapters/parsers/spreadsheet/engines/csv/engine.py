from __future__ import annotations

import csv
from typing import ClassVar

from harborrag_adapters.parsers.common.resources import (
    parse_input_suffix,
    read_parse_input_bytes,
)
from harborrag_adapters.parsers.common.utils import (
    get_parser_logger,
    input_label,
    parser_log_extra,
)
from harborrag_adapters.parsers.common.validation import guard_input_size
from harborrag_adapters.parsers.errors import ParseError, TextDecodingError
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
        data = guard_input_size(read_parse_input_bytes(parse_input))
        lines, warnings = self._decode_lines(parse_input, data)
        sample = "".join(lines)[:4096]
        try:
            dialect = csv.Sniffer().sniff(sample, delimiters=",;\t|")
        except csv.Error:
            dialect = csv.excel_tab if parse_input_suffix(parse_input) == ".tsv" else csv.excel

        reader = csv.reader(lines, dialect=dialect, strict=True)
        rendered_rows: list[str] = []
        expected_fields: int | None = None
        while True:
            try:
                row = next(reader)
            except StopIteration:
                break
            except csv.Error as exc:
                if "field larger than field limit" in str(exc):
                    raise ParseError(f"Invalid CSV: {exc}") from exc
                warning = f"skipped malformed CSV row at line {reader.line_num}: {exc}"
                self._warn(parse_input, warning)
                warnings.append(warning)
                continue

            if not any(cell.strip() for cell in row):
                continue
            if expected_fields is None:
                expected_fields = len(row)
            elif len(row) != expected_fields:
                warning = (
                    f"skipped malformed CSV row at line {reader.line_num}: "
                    f"expected {expected_fields} fields, found {len(row)}"
                )
                self._warn(parse_input, warning)
                warnings.append(warning)
                continue
            rendered_rows.append("\t".join(cell.strip() for cell in row).rstrip())

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
            warnings=warnings or None,
        )

    def _decode_lines(
        self,
        parse_input: ParseInput,
        data: bytes,
    ) -> tuple[list[str], list[str]]:
        """Decode independent physical rows so one bad row remains recoverable."""

        if isinstance(parse_input.content, str):
            return parse_input.content.splitlines(keepends=True), []
        if data.startswith((b"\xff\xfe", b"\xfe\xff")):
            try:
                return data.decode("utf-16").splitlines(keepends=True), []
            except UnicodeDecodeError as exc:
                raise ParseError(f"Could not decode UTF-16 CSV input: {exc}") from exc

        lines: list[str] = []
        warnings: list[str] = []
        for line_number, raw_line in enumerate(data.splitlines(keepends=True), start=1):
            encoding = "utf-8-sig" if line_number == 1 else "utf-8"
            try:
                lines.append(raw_line.decode(encoding))
            except UnicodeDecodeError as exc:
                warning = (
                    f"skipped CSV row at line {line_number}: invalid UTF-8 "
                    f"byte at offset {exc.start}"
                )
                self._warn(parse_input, warning)
                warnings.append(warning)
        if not lines and warnings:
            # Every physical row failed to decode -- this isn't a few bad rows
            # in an otherwise-good file, it's undecodable input that would
            # otherwise silently surface as an empty document.
            raise TextDecodingError(byte_length=len(data))
        return lines, warnings

    def _warn(self, parse_input: ParseInput, warning: str) -> None:
        parser_logger.warning(
            "%s for %s",
            warning,
            input_label(parse_input),
            extra=parser_log_extra(
                input=parse_input,
                parser_name=self.parser_name,
                parser_engine=self.parser_engine,
            ),
        )


CsvParser = CsvSpreadsheetEngine
