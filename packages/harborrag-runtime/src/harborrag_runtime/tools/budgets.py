"""Transport-neutral ceilings applied around one tool call.

Both transports dispatch the same catalog, so the limits that keep a tool call
safe -- validated arguments, a bounded argument payload, a bounded result --
belong with the tools rather than with either transport. The MCP server layers
its configured per-tenant policy and audit on top of this; the agent loop
applies the same ceilings before a result is appended to model context, where an
unbounded payload is spent budget rather than a rejected request.

``label`` only shapes the message text, so each transport keeps reporting
failures in its own established vocabulary.
"""

from __future__ import annotations

import json
from dataclasses import dataclass

from jsonschema.exceptions import SchemaError, ValidationError
from jsonschema.validators import validator_for

from harborrag_runtime.tools.base import MAX_TOOL_RESULTS, ToolSpec

_COUNTED_LIST_FIELDS = (
    "results",
    "items",
    "chunks",
    "sources",
    "documents",
    "candidates",
    "triplets",
    "paths",
    "nodes",
)


def result_count(result: dict[str, object]) -> int:
    """Best-effort item count for a tool result, for budget checks.

    Tools that return a list (e.g. retrieval) are counted by list length;
    single-payload tools (e.g. health checks) count as one result.
    """

    for field_name in _COUNTED_LIST_FIELDS:
        results = result.get(field_name)
        if isinstance(results, list):
            return len(results)
    data = result.get("data")
    if isinstance(data, dict):
        counts = [len(value) for value in data.values() if isinstance(value, list)]
        if counts:
            return max(counts)
    return 1


def _serialized_size(payload: dict[str, object], label: str, subject: str) -> tuple[str, int]:
    try:
        serialized = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{label} {subject} must be JSON serializable.") from exc
    return serialized, len(serialized.encode("utf-8"))


@dataclass(frozen=True, slots=True)
class ToolBudget:
    """Ceilings enforced around one tool call, independent of transport."""

    label: str = "Tool"
    # Name the offending field and bound in the rejection. A model has to
    # self-correct from the error text alone, so "$.top_k: 0 is less than the
    # minimum of 1" is worth far more to it than "arguments do not match".
    detail_in_errors: bool = False
    max_results: int = MAX_TOOL_RESULTS
    max_argument_bytes: int = 64 * 1024
    max_output_bytes: int = 1024 * 1024
    allow_ingestion: bool = False

    def check_call(self, spec: ToolSpec, arguments: dict[str, object]) -> None:
        """Reject a call whose arguments are oversized or off-schema."""

        if spec.capability == "ingestion" and not self.allow_ingestion:
            raise PermissionError(f"{self.label} ingestion tools are disabled.")
        serialized, size = _serialized_size(arguments, self.label, "arguments")
        if size > self.max_argument_bytes:
            raise ValueError(f"{self.label} argument budget exceeded.")
        try:
            validator_type = validator_for(spec.input_schema)
            validator_type.check_schema(spec.input_schema)
            validator_type(spec.input_schema).validate(json.loads(serialized))
        except SchemaError as exc:
            raise RuntimeError(f"{self.label} tool has an invalid input schema.") from exc
        except ValidationError as exc:
            message = f"{self.label} arguments do not match the tool schema."
            if self.detail_in_errors:
                message = f"{message} {exc.json_path}: {exc.message}"
            raise ValueError(message) from exc

    def check_results(self, count: int) -> None:
        if count > self.max_results:
            raise ValueError(f"{self.label} result budget exceeded.")

    def check_output(self, result: dict[str, object]) -> None:
        _, size = _serialized_size(result, self.label, "output")
        if size > self.max_output_bytes:
            raise ValueError(f"{self.label} output budget exceeded.")

    def check_output_schema(
        self,
        result: dict[str, object],
        schema: dict[str, object] | None,
    ) -> None:
        """Validate a result against its declared output schema.

        A mismatch is our own bug rather than a caller's, hence ``RuntimeError``.
        Only transports whose whole catalog declares output schemas can apply
        this; the agent catalog includes memory tools that declare none.
        """

        if schema is None:
            raise RuntimeError(f"{self.label} tool has no output schema.")
        try:
            serialized = json.dumps(result, ensure_ascii=False, separators=(",", ":"))
            validator_type = validator_for(schema)
            validator_type.check_schema(schema)
            validator_type(schema).validate(json.loads(serialized))
        except SchemaError as exc:
            raise RuntimeError(f"{self.label} tool has an invalid output schema.") from exc
        except ValidationError as exc:
            raise RuntimeError(f"{self.label} tool returned an invalid output.") from exc


__all__ = ["ToolBudget", "result_count"]
