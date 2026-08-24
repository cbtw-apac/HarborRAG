from __future__ import annotations

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
from harborrag_adapters.parsers.common.validation import (
    ParseResourceBudget,
    guard_input_size,
    open_guarded_zip,
    raise_if_password_protected_document,
    wrap_parse_errors,
)
from harborrag_adapters.parsers.errors import ParseError
from harborrag_adapters.parsers.spreadsheet.base import HarborSpreadsheetEngine
from harborrag_core.domain.element import DocumentElement
from harborrag_core.domain.parser import ParsedDocument, ParseInput

from .rendering import (
    guard_declared_table_size,
    legacy_cell_to_text,
    open_data_workbook,
    openxml_cell_to_text,
)

parser_logger = get_parser_logger("excel")


class ExcelSpreadsheetEngine(HarborSpreadsheetEngine):
    """Extract workbook sheets as tab-separated table text."""

    _guard_declared_table_size = staticmethod(guard_declared_table_size)
    _cell_to_text = staticmethod(openxml_cell_to_text)
    _xls_cell_to_text = staticmethod(legacy_cell_to_text)

    supports_formulas: ClassVar[bool] = True
    supports_merged_cells: ClassVar[bool] = True

    parser_name: ClassVar[str] = "excel"
    parser_engine: ClassVar[str] = "openpyxl/xlrd"
    suffixes: ClassVar[frozenset[str]] = frozenset({"xls", "xlsx", "xlsm", "xltx", "xltm"})
    content_types: ClassVar[frozenset[str]] = frozenset(
        {
            "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            "application/vnd.ms-excel",
            "application/vnd.ms-excel.sheet.macroenabled.12",
            "application/vnd.openxmlformats-officedocument.spreadsheetml.template",
            "application/vnd.ms-excel.template.macroenabled.12",
        }
    )

    def parse(self, input: ParseInput) -> ParsedDocument:
        """Route legacy `.xls` through xlrd and OpenXML workbooks through openpyxl."""

        parse_input = self.coerce_input(input)
        source_bytes = guard_input_size(read_parse_input_bytes(parse_input))
        parser_logger.debug(
            "Starting Excel parse for %s",
            input_label(parse_input),
            extra=parser_log_extra(
                input=parse_input,
                parser_name=self.parser_name,
                parser_engine=self.parser_engine,
                input_bytes=len(source_bytes),
            ),
        )

        # A zero-byte spreadsheet carries no rows, just like a zero-byte CSV.
        # Treat it as an empty successful extraction rather than handing it to
        # openpyxl/xlrd, which report a container-format failure.
        if not source_bytes:
            return ParsedDocument(
                content="",
                elements=[],
                parser_name=self.parser_name,
                parser_version=self.parser_version,
                metadata=self.metadata_for(parse_input, sheets=[]),
            )

        if parse_input_suffix(parse_input) == ".xls":
            content, elements, sheet_names = self._parse_xls(parse_input, source_bytes)
        else:
            content, elements, sheet_names = self._parse_openxml(
                parse_input,
                source_bytes,
            )

        parser_logger.info(
            "Parsed Excel workbook %s sheets=%d content_chars=%d elements=%d",
            input_label(parse_input),
            len(sheet_names),
            len(content),
            len(elements),
            extra=parser_log_extra(
                input=parse_input,
                parser_name=self.parser_name,
                parser_engine=self.parser_engine,
                sheets=len(sheet_names),
                content_chars=len(content),
                elements=len(elements),
            ),
        )
        return ParsedDocument(
            content=content,
            elements=elements,
            parser_name=self.parser_name,
            parser_version=self.parser_version,
            metadata=self.metadata_for(parse_input, sheets=sheet_names),
        )

    def _parse_openxml(
        self,
        parse_input: ParseInput,
        source_bytes: bytes,
    ) -> tuple[str, list[DocumentElement], list[str]]:
        """Read `.xlsx`-style workbooks in read-only, data-only mode."""

        try:
            from openpyxl import load_workbook
        except ImportError as exc:
            parser_logger.error(
                "Excel parser dependency `openpyxl` is missing",
                extra=parser_log_extra(
                    input=parse_input,
                    parser_name=self.parser_name,
                    parser_engine=self.parser_engine,
                ),
            )
            raise ParseError(
                "Excel parsing requires `openpyxl`; install `harborrag-adapters[parsers]` "
                "or `pip install openpyxl`."
            ) from exc

        parser_logger.debug(
            "Extracting OpenXML Excel text from %s",
            input_label(parse_input),
            extra=parser_log_extra(
                input=parse_input,
                parser_name=self.parser_name,
                parser_engine="openpyxl",
                input_bytes=len(source_bytes),
            ),
        )
        # Keep the lazy workbook lifecycle inside the error boundary: malformed sheet XML
        # can fail during `iter_rows()` rather than the initial open.
        with wrap_parse_errors("openpyxl"):
            # Encrypted XLSX is an OLE compound file, so check before opening it as a zip.
            raise_if_password_protected_document(source_bytes, format_name="xlsx")
            # XLSX is a zip container: reject decompression-bomb shapes before
            # handing bytes to openpyxl, exactly like DOCX/EPUB/PPTX do.
            with open_guarded_zip(source_bytes) as archive:
                raise_if_password_protected_document(
                    source_bytes,
                    format_name="xlsx",
                    archive=archive,
                )
            workbook = open_data_workbook(load_workbook, source_bytes)
            try:
                sheet_names = workbook.sheetnames
                sections: list[str] = []
                elements: list[DocumentElement] = []
                budget = ParseResourceBudget()
                for sheet in workbook.worksheets:
                    self._guard_declared_table_size(
                        rows=int(sheet.max_row or 0),
                        columns=int(sheet.max_column or 0),
                        budget=budget,
                    )
                    rows: list[str] = []
                    for values in sheet.iter_rows(values_only=True):
                        rendered = "\t".join(self._cell_to_text(value) for value in values).rstrip()
                        budget.consume_row(
                            len(values),
                            output_characters=len(rendered) + 1 if rendered.strip() else 0,
                        )
                        if rendered.strip():
                            rows.append(rendered)
                    if not rows:
                        parser_logger.debug(
                            "Skipping empty Excel sheet %s",
                            sheet.title,
                            extra=parser_log_extra(
                                input=parse_input,
                                parser_name=self.parser_name,
                                parser_engine="openpyxl",
                                sheet=sheet.title,
                                rows=0,
                            ),
                        )
                        continue
                    sheet_text = "\n".join(rows)
                    budget.consume_output(len(sheet.title) + len("Sheet: \n\n"))
                    sections.append(f"Sheet: {sheet.title}\n{sheet_text}")
                    elements.append(
                        DocumentElement(
                            id=f"excel:sheet:{sheet.title}",
                            type="table",
                            content=sheet_text,
                            metadata={"sheet": sheet.title},
                        )
                    )
                    parser_logger.debug(
                        "Extracted Excel sheet %s rows=%d content_chars=%d",
                        sheet.title,
                        len(rows),
                        len(sheet_text),
                        extra=parser_log_extra(
                            input=parse_input,
                            parser_name=self.parser_name,
                            parser_engine="openpyxl",
                            sheet=sheet.title,
                            rows=len(rows),
                            content_chars=len(sheet_text),
                        ),
                    )
            finally:
                workbook.close()

        return "\n\n".join(sections).strip(), elements, sheet_names

    def _parse_xls(
        self,
        parse_input: ParseInput,
        source_bytes: bytes,
    ) -> tuple[str, list[DocumentElement], list[str]]:
        """Read legacy binary `.xls` workbooks with xlrd."""

        try:
            import xlrd
        except ImportError as exc:
            parser_logger.error(
                "Excel parser dependency `xlrd` is missing",
                extra=parser_log_extra(
                    input=parse_input,
                    parser_name=self.parser_name,
                    parser_engine=self.parser_engine,
                ),
            )
            raise ParseError(
                "Legacy .xls parsing requires `xlrd`; install "
                "`harborrag-adapters[parsers]` or `pip install xlrd`."
            ) from exc

        parser_logger.debug(
            "Extracting legacy Excel text from %s",
            input_label(parse_input),
            extra=parser_log_extra(
                input=parse_input,
                parser_name=self.parser_name,
                parser_engine="xlrd",
                input_bytes=len(source_bytes),
            ),
        )
        # As with openpyxl above, the whole workbook lifecycle stays inside
        # `wrap_parse_errors`: `on_demand=True` defers per-sheet parsing to
        # `sheet_by_index()`, so a corrupt sheet fails during iteration below,
        # not during `open_workbook()`.
        with wrap_parse_errors("xlrd"):
            workbook = xlrd.open_workbook(
                file_contents=source_bytes,
                on_demand=True,
            )
            sheet_names = workbook.sheet_names()
            sections: list[str] = []
            elements: list[DocumentElement] = []
            budget = ParseResourceBudget()
            try:
                # Load sheets one at a time (Book.sheets() would defeat on_demand and
                # load them all), unloading each after rendering to bound memory.
                for sheet_index in range(workbook.nsheets):
                    sheet = workbook.sheet_by_index(sheet_index)
                    self._guard_declared_table_size(
                        rows=sheet.nrows,
                        columns=sheet.ncols,
                        budget=budget,
                    )
                    rows: list[str] = []
                    for row_index in range(sheet.nrows):
                        row = "\t".join(
                            self._xls_cell_to_text(
                                sheet.cell(row_index, column_index),
                                workbook.datemode,
                                xlrd,
                            )
                            for column_index in range(sheet.ncols)
                        ).rstrip()
                        budget.consume_row(
                            sheet.ncols,
                            output_characters=len(row) + 1 if row.strip() else 0,
                        )
                        if row.strip():
                            rows.append(row)
                    if not rows:
                        parser_logger.debug(
                            "Skipping empty legacy Excel sheet %s",
                            sheet.name,
                            extra=parser_log_extra(
                                input=parse_input,
                                parser_name=self.parser_name,
                                parser_engine="xlrd",
                                sheet=sheet.name,
                                rows=0,
                            ),
                        )
                        workbook.unload_sheet(sheet_index)
                        continue
                    sheet_text = "\n".join(rows)
                    budget.consume_output(len(sheet.name) + len("Sheet: \n\n"))
                    sections.append(f"Sheet: {sheet.name}\n{sheet_text}")
                    elements.append(
                        DocumentElement(
                            id=f"excel:sheet:{sheet.name}",
                            type="table",
                            content=sheet_text,
                            metadata={"sheet": sheet.name},
                        )
                    )
                    parser_logger.debug(
                        "Extracted legacy Excel sheet %s rows=%d content_chars=%d",
                        sheet.name,
                        len(rows),
                        len(sheet_text),
                        extra=parser_log_extra(
                            input=parse_input,
                            parser_name=self.parser_name,
                            parser_engine="xlrd",
                            sheet=sheet.name,
                            rows=len(rows),
                            content_chars=len(sheet_text),
                        ),
                    )
                    workbook.unload_sheet(sheet_index)
            finally:
                workbook.release_resources()

        return "\n\n".join(sections).strip(), elements, sheet_names


ExcelParser = ExcelSpreadsheetEngine
