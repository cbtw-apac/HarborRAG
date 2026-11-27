"""Transport-neutral ceilings applied around one tool call.

The limits that keep a tool call safe (validated arguments, a bounded argument
payload, a bounded result) belong with the tools rather than with a transport.
The MCP server layers its configured per-tenant policy and audit on top.

A refusal names the field and the schema constraint it broke, never the value
the caller sent: the text reaches the client, logs and trace span statuses, and
an argument can be query text. ``label`` only shapes the message text, so a
transport keeps reporting failures in its own vocabulary.
"""

from __future__ import annotations

import json
import re
from collections.abc import Iterable
from dataclasses import dataclass
from functools import lru_cache

from jsonschema.exceptions import SchemaError, ValidationError, best_match
from jsonschema.protocols import Validator
from jsonschema.validators import validator_for

from harborrag_engine.tools.base import MAX_TOOL_RESULTS, ToolSpec

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
    "memories",
)

MAX_ARGUMENT_BYTES = 64 * 1024
"""Largest serialized argument payload any transport accepts for one call."""

MAX_OUTPUT_BYTES = 1024 * 1024
"""Largest serialized result any transport accepts from one call."""

_COMBINATOR_VALIDATORS = frozenset({"not", "anyOf", "oneOf", "allOf"})
_MAX_DETAIL_CHARS = 200
_ARGUMENTS_PATH = "arguments"
_UNNAMED_SEGMENT = "*"
_FIELD_NAME = re.compile(r"[A-Za-z_][A-Za-z0-9_-]{0,63}")
_CONSTRAINT_SENTENCES = {
    "type": "must be of type {value}",
    "enum": "must be one of {value}",
    "const": "must equal {value}",
    "minimum": "must be at least {value}",
    "maximum": "must be at most {value}",
    "exclusiveMinimum": "must be greater than {value}",
    "exclusiveMaximum": "must be less than {value}",
    "multipleOf": "must be a multiple of {value}",
    "minLength": "must be at least {value} characters long",
    "maxLength": "must be at most {value} characters long",
    "pattern": "must match the pattern {value}",
    "format": "must be a valid {value}",
    "minItems": "must have at least {value} items",
    "maxItems": "must have at most {value} items",
    "uniqueItems": "must not repeat items",
    "minProperties": "must have at least {value} properties",
    "maxProperties": "must have at most {value} properties",
}


@lru_cache(maxsize=512)
def _compiled(schema_key: str) -> Validator:
    """Compile and metaschema-check one schema, keyed by its canonical text.

    ``check_schema`` dominates validation cost (milliseconds on the larger
    output schemas) and a tool's schema is fixed for as long as its resolved
    configuration is. Keying on the canonical serialization rather than object
    identity keeps this correct for MCP, whose configuration layer rewrites a
    fresh schema dict per call: an actually-different schema is a different key.
    """
    schema = json.loads(schema_key)
    validator_type = validator_for(schema)
    validator_type.check_schema(schema)
    return validator_type(schema)


def _validator(schema: dict[str, object]) -> Validator:
    """Return the cached validator for a schema dict."""
    return _compiled(json.dumps(schema, sort_keys=True, separators=(",", ":")))


def _validation_detail(error: ValidationError) -> str:
    """Name the failing field and the schema constraint it broke, in one short sentence.

    Every part comes from the schema or from field names, never from the
    caller's value. jsonschema's own message quotes the value, up to the whole
    argument budget.
    """
    best = best_match([error]) or error
    path = _field_path(best.absolute_path)
    if best.validator == "required":
        return _truncated(f"{_child_path(path, _missing_field(best))}: is required")
    if best.validator == "additionalProperties":
        allowed = sorted(_declared_properties(best.schema))
        return _truncated(f"{path}: allows only {_constraint_text(allowed)}")
    return _truncated(f"{path}: {_constraint_sentence(best)}")


def _constraint_sentence(error: ValidationError) -> str:
    """Describe the failed constraint from the schema's own value."""
    validator = str(error.validator)
    if validator in _COMBINATOR_VALIDATORS:
        return f"does not satisfy {validator}: {_constraint_text(error.validator_value)}"
    sentence = _CONSTRAINT_SENTENCES.get(validator)
    if sentence is None:
        return f"does not satisfy {validator}"
    return sentence.format(value=_constraint_text(error.validator_value))


def _constraint_text(value: object) -> str:
    """Render a schema value compactly; a plain string stays unquoted."""
    if isinstance(value, str):
        return value
    return json.dumps(value, separators=(",", ":"), default=str)


def _missing_field(error: ValidationError) -> str:
    """Return the first required field the instance lacks, from the schema's list."""
    required = error.validator_value if isinstance(error.validator_value, list) else []
    present = error.instance if isinstance(error.instance, dict) else {}
    missing = [name for name in required if isinstance(name, str) and name not in present]
    return _segment_name(missing[0]) if missing else _UNNAMED_SEGMENT


def _declared_properties(schema: object) -> list[str]:
    """Return the property names a schema declares."""
    if not isinstance(schema, dict):
        return []
    properties = schema.get("properties")
    return [str(name) for name in properties] if isinstance(properties, dict) else []


def _field_path(segments: Iterable[str | int]) -> str:
    """Render an instance path such as ``items[0].chunk_id``.

    A key that is not a plain field name, such as a free-form map key the
    caller chose, becomes ``*``.
    """
    path = ""
    for segment in segments:
        if isinstance(segment, int):
            path = f"{path}[{segment}]"
        else:
            path = _child_path(path, _segment_name(segment))
    return path or _ARGUMENTS_PATH


def _child_path(parent: str, name: str) -> str:
    """Join a field name onto a path, where ``arguments`` is the root."""
    if parent in {"", _ARGUMENTS_PATH}:
        return name
    return f"{parent}.{name}"


def _segment_name(segment: str) -> str:
    """Return the key when it looks like a field name, else ``*``."""
    return segment if _FIELD_NAME.fullmatch(segment) else _UNNAMED_SEGMENT


def _truncated(detail: str) -> str:
    """Cap a detail sentence at a fixed length."""
    if len(detail) > _MAX_DETAIL_CHARS:
        return f"{detail[:_MAX_DETAIL_CHARS]}..."
    return detail


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
    """Serialize a payload and return it with its size in UTF-8 bytes."""
    try:
        serialized = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{label} {subject} must be JSON serializable.") from exc
    return serialized, len(serialized.encode("utf-8"))


@dataclass(frozen=True, slots=True)
class ToolBudget:
    """Ceilings enforced around one tool call, independent of transport.

    Attributes:
        label: Word that starts every refusal, such as ``MCP``.
        detail_in_errors: Name the failing field and constraint in a schema
            refusal, such as ``top_k: must be at least 1``. A model corrects
            its next call from the refusal text alone.
        max_results: Most results one call may return.
        max_argument_bytes: Largest serialized argument payload.
        max_output_bytes: Largest serialized result.
        allow_ingestion: Allow tools whose capability is ingestion.
    """

    label: str = "Tool"
    detail_in_errors: bool = False
    max_results: int = MAX_TOOL_RESULTS
    max_argument_bytes: int = MAX_ARGUMENT_BYTES
    max_output_bytes: int = MAX_OUTPUT_BYTES
    allow_ingestion: bool = False

    def narrowed(self, requested: ToolBudget) -> ToolBudget:
        """Apply caller-specific limits without widening the shared ceiling."""
        return ToolBudget(
            label=requested.label,
            detail_in_errors=requested.detail_in_errors,
            max_results=min(self.max_results, requested.max_results),
            max_argument_bytes=min(self.max_argument_bytes, requested.max_argument_bytes),
            max_output_bytes=min(self.max_output_bytes, requested.max_output_bytes),
            allow_ingestion=self.allow_ingestion and requested.allow_ingestion,
        )

    def check_call(self, spec: ToolSpec, arguments: dict[str, object]) -> None:
        """Reject a call whose arguments are oversized or off-schema."""
        if spec.capability == "ingestion" and not self.allow_ingestion:
            raise PermissionError(f"{self.label} ingestion tools are disabled.")
        serialized, size = _serialized_size(arguments, self.label, "arguments")
        if size > self.max_argument_bytes:
            raise ValueError(f"{self.label} argument budget exceeded.")
        try:
            validator = _validator(spec.input_schema)
        except SchemaError as exc:
            raise RuntimeError(f"{self.label} tool has an invalid input schema.") from exc
        try:
            validator.validate(json.loads(serialized))
        except ValidationError as exc:
            message = f"{self.label} arguments do not match the tool schema."
            if self.detail_in_errors:
                message = f"{message} {_validation_detail(exc)}"
            raise ValueError(message) from exc

    def check_results(self, count: int) -> None:
        """Reject a result list longer than ``max_results``."""
        if count > self.max_results:
            raise ValueError(f"{self.label} result budget exceeded.")

    def check_output(self, result: dict[str, object]) -> None:
        """Reject a result larger than ``max_output_bytes`` once serialized."""
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
        Only transports whose whole catalog declares output schemas can apply this.
        """
        if schema is None:
            raise RuntimeError(f"{self.label} tool has no output schema.")
        serialized, _ = _serialized_size(result, self.label, "output")
        try:
            validator = _validator(schema)
        except SchemaError as exc:
            raise RuntimeError(f"{self.label} tool has an invalid output schema.") from exc
        try:
            validator.validate(json.loads(serialized))
        except ValidationError as exc:
            raise RuntimeError(f"{self.label} tool returned an invalid output.") from exc
