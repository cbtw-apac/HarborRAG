from __future__ import annotations

import json
from dataclasses import dataclass

from jsonschema.exceptions import SchemaError, ValidationError
from jsonschema.validators import validator_for

from harborrag_mcp_server.tools.base import McpToolSpec


@dataclass(frozen=True, slots=True)
class McpToolPolicy:
    max_results: int = 20
    max_argument_bytes: int = 64 * 1024
    max_output_bytes: int = 1024 * 1024
    allow_ingestion: bool = False

    def check_call(self, spec: McpToolSpec, arguments: dict[str, object]) -> None:
        if spec.capability == "ingestion" and not self.allow_ingestion:
            raise PermissionError("MCP ingestion tools are disabled.")
        try:
            serialized = json.dumps(
                arguments,
                ensure_ascii=False,
                separators=(",", ":"),
            )
            size = len(serialized.encode("utf-8"))
        except (TypeError, ValueError) as exc:
            raise ValueError("MCP arguments must be JSON serializable.") from exc
        if size > self.max_argument_bytes:
            raise ValueError("MCP argument budget exceeded.")
        try:
            validator_type = validator_for(spec.input_schema)
            validator_type.check_schema(spec.input_schema)
            validator_type(spec.input_schema).validate(json.loads(serialized))
        except SchemaError as exc:
            raise RuntimeError("MCP tool has an invalid input schema.") from exc
        except ValidationError as exc:
            raise ValueError("MCP arguments do not match the tool schema.") from exc

    def check_results(self, count: int) -> None:
        if count > self.max_results:
            raise ValueError("MCP result budget exceeded.")

    def check_output(self, result: dict[str, object]) -> None:
        try:
            size = len(
                json.dumps(
                    result,
                    ensure_ascii=False,
                    separators=(",", ":"),
                ).encode("utf-8")
            )
        except (TypeError, ValueError) as exc:
            raise ValueError("MCP output must be JSON serializable.") from exc
        if size > self.max_output_bytes:
            raise ValueError("MCP output budget exceeded.")
