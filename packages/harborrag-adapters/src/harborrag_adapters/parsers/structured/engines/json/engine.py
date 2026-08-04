from __future__ import annotations

import json
from typing import Any, ClassVar

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
from harborrag_adapters.parsers.structured.base import HarborStructuredEngine
from harborrag_core.domain.element import DocumentElement
from harborrag_core.domain.parser import ParsedDocument, ParseInput

parser_logger = get_parser_logger("json")


class JsonStructuredEngine(HarborStructuredEngine):
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
        source_bytes = guard_input_size(read_parse_input_bytes(parse_input))
        if not source_bytes:
            # An empty string is not valid JSON syntax, so `json.loads`
            # would otherwise reject it. There is nothing to parse, so
            # succeed with empty output like the other engines.
            return self.empty_result(parse_input, root_type=None, raw={"json": None})
        data: Any
        try:
            source = read_parse_input_text(parse_input)
            if parse_input_suffix(parse_input) in {".jsonl", ".ndjson"}:
                data = [json.loads(line) for line in source.splitlines() if line.strip()]
            else:
                data = json.loads(source)
        except (json.JSONDecodeError, RecursionError, UnicodeDecodeError) as exc:
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
        parser_logger.info(
            "Parsed JSON %s root_type=%s content_chars=%d elements=%d",
            input_label(parse_input),
            type(data).__name__,
            len(content),
            len(elements),
            extra=parser_log_extra(
                input=parse_input,
                parser_name=self.parser_name,
                parser_engine=self.parser_engine,
                root_type=type(data).__name__,
                content_chars=len(content),
                elements=len(elements),
            ),
        )
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


JsonParser = JsonStructuredEngine
