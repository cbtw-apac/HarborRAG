"""Make a Pydantic schema acceptable to OpenAI strict structured output."""

from __future__ import annotations

from typing import Any

from pydantic import ConfigDict


def strict_schema(schema: dict[str, Any]) -> None:
    """Declare the two things OpenAI strict mode requires of an object schema.

    ``additionalProperties: false`` and a ``required`` array naming *every*
    property. Pydantic supplies neither here: it omits the first entirely and
    emits the second only for fields without defaults, and these models
    default every field on purpose so one malformed entry cannot invalidate a
    whole response.

    Applied to the emitted schema rather than through ``extra="forbid"`` and
    mandatory fields, so parse-time behaviour is untouched: a provider that
    omits a key still gets the default instead of a validation error. Computed
    from ``properties`` rather than written out, so adding a field cannot
    silently reintroduce the 400 this exists to prevent.
    """

    schema["additionalProperties"] = False
    schema["required"] = list(schema.get("properties", {}))


STRICT_SCHEMA_CONFIG = ConfigDict(json_schema_extra=strict_schema)
"""``model_config`` for any model bound by ``with_structured_output``."""


__all__ = ["STRICT_SCHEMA_CONFIG", "strict_schema"]
