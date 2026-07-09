from __future__ import annotations

import csv
import json
from io import StringIO
from typing import Any, ClassVar

from harborrag_core.domain.element import DocumentElement
from harborrag_core.domain.parser import ParsedDocument, ParseInput

from .base import BaseParser
from .exceptions import ParseError
from .parser_logging import get_parser_logger, input_label, parser_log_extra


parser_logger = get_parser_logger("structured")


class CsvParser(BaseParser[ParseInput, ParsedDocument]):
    """Render CSV and TSV inputs as tab-separated table text."""

    parser_name: ClassVar[str] = "csv"
    parser_engine: ClassVar[str] = "python/csv"
    suffixes: ClassVar[frozenset[str]] = frozenset({"csv", "tsv"})
    content_types: ClassVar[frozenset[str]] = frozenset(
        {"text/csv", "text/tab-separated-values"}
    )

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
        text = parse_input.read_text()
        sample = text[:4096]
        try:
            dialect = csv.Sniffer().sniff(sample)
        except csv.Error:
            dialect = csv.excel_tab if parse_input.suffix == ".tsv" else csv.excel

        try:
            rows = list(csv.reader(StringIO(text), dialect=dialect))
        except csv.Error as exc:
            # e.g. a single cell larger than csv.field_size_limit — surface as an
            # expected ParseError so bulk callers can quarantine the document.
            raise ParseError(f"Invalid CSV: {exc}") from exc
        rendered_rows = [
            "\t".join(cell.strip() for cell in row).rstrip()
            for row in rows
            if any(cell.strip() for cell in row)
        ]
        content = "\n".join(rendered_rows)
        elements = [
            DocumentElement(
                id="csv:0",
                type="table",
                content=content,
                metadata={"rows": len(rendered_rows)},
            )
        ] if content else []
        return ParsedDocument(
            content=content,
            elements=elements,
            parser_name=self.parser_name,
            parser_version=self.parser_version,
            metadata=self.metadata_for(parse_input, rows=len(rendered_rows)),
        )


class JsonParser(BaseParser[ParseInput, ParsedDocument]):
    """Flatten JSON and JSON Lines inputs into path-value text."""

    parser_name: ClassVar[str] = "json"
    parser_engine: ClassVar[str] = "python/json"
    suffixes: ClassVar[frozenset[str]] = frozenset({"json", "jsonl", "ndjson"})
    content_types: ClassVar[frozenset[str]] = frozenset(
        {"application/json", "application/x-ndjson", "application/jsonl"}
    )

    def parse(self, input: ParseInput) -> ParsedDocument:
        """Decode JSON/NDJSON, expose flattened text, and keep raw JSON payloads."""

        parse_input = self.coerce_input(input)
        parser_logger.debug(
            "Extracting JSON text from %s",
            input_label(parse_input),
            extra=parser_log_extra(
                input=parse_input,
                parser_name=self.parser_name,
                parser_engine=self.parser_engine,
            ),
        )
        source = parse_input.read_text()
        data: Any
        try:
            if parse_input.suffix in {".jsonl", ".ndjson"}:
                data = [
                    json.loads(line)
                    for line in source.splitlines()
                    if line.strip()
                ]
            else:
                data = json.loads(source)
        except (json.JSONDecodeError, RecursionError) as exc:
            # RecursionError comes from adversarially deep nesting; both are
            # expected "bad document" outcomes, not internal bugs.
            parser_logger.warning(
                "Invalid JSON in %s: %s",
                input_label(parse_input),
                exc,
                extra=parser_log_extra(
                    input=parse_input,
                    parser_name=self.parser_name,
                    parser_engine=self.parser_engine,
                ),
            )
            raise ParseError(f"Invalid JSON: {exc}") from exc

        flattened = list(self._flatten(data))
        content = "\n".join(flattened) if flattened else json.dumps(data, ensure_ascii=False)
        elements = [
            DocumentElement(
                id="json:0",
                type="metadata",
                content=content,
                metadata={"root_type": type(data).__name__},
            )
        ]
        return ParsedDocument(
            content=content,
            elements=elements,
            parser_name=self.parser_name,
            parser_version=self.parser_version,
            metadata=self.metadata_for(parse_input, root_type=type(data).__name__),
            raw={"json": data},
        )

    # Bound recursion so hostile deeply-nested JSON cannot exhaust the stack;
    # beyond the cap the remaining subtree is summarized rather than walked.
    MAX_FLATTEN_DEPTH: ClassVar[int] = 200

    @classmethod
    def _flatten(cls, value: Any, path: str = "$", depth: int = 0) -> list[str]:
        """Convert nested JSON into deterministic JSONPath-like text lines."""

        if depth >= cls.MAX_FLATTEN_DEPTH:
            return [f"{path}: <max-depth {cls.MAX_FLATTEN_DEPTH} reached>"]

        if isinstance(value, dict):
            if not value:
                return [f"{path}: {{}}"]
            lines: list[str] = []
            for key, child in value.items():
                lines.extend(cls._flatten(child, f"{path}.{key}", depth + 1))
            return lines

        if isinstance(value, list):
            if not value:
                return [f"{path}: []"]
            lines = []
            for index, child in enumerate(value):
                lines.extend(cls._flatten(child, f"{path}[{index}]", depth + 1))
            return lines

        return [f"{path}: {value}"]
