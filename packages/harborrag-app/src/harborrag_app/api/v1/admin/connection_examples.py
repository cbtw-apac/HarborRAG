"""Swagger UI request bodies for connection writes and tests, one per source type and auth method."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any, Self

from fastapi.openapi.models import Example

from harborrag_runtime.config.connectors.models import METHOD_KEY, SourceTypeRegistry

_PLACEHOLDERS: Mapping[str, Any] = {
    "string": "…",
    "integer": 0,
    "number": 0,
    "boolean": False,
    "array": [],
}


class ConnectionExampleBuilder:
    """Build named request examples from the schemas ``GET /v1/admin/source-types`` publishes.

    Each example is a complete body: credentials for one auth method, with its
    ``method`` when the type has a choice, and a config holding every field
    that has an example or a default, so the body shows what can be edited.
    """

    def __init__(self, registry: SourceTypeRegistry) -> None:
        self._registry = registry
        self._sync_now: bool | None = None

    def with_sync_now(self, value: bool = False) -> Self:
        """Add ``sync_now`` to every body, as the ``PUT`` route takes it."""
        self._sync_now = value
        return self

    def build(self) -> dict[str, Example]:
        """Return the examples keyed ``<source_type>-<method>``, in registration order."""
        examples: dict[str, Example] = {}
        for spec in self._registry.all():
            described = spec.describe()
            config = _values(described["config_schema"], with_defaults=True)
            for method in described["auth_methods"]:
                credentials = _values(method["schema"], with_defaults=False)
                if spec.has_method_choice:
                    credentials = {METHOD_KEY: method["id"], **credentials}
                body: dict[str, Any] = {
                    "source_type": spec.source_type,
                    "name": f"{spec.name} connection",
                    "credentials": credentials,
                    "config": config,
                }
                if self._sync_now is not None:
                    body["sync_now"] = self._sync_now
                examples[f"{spec.source_type}-{method['id']}"] = Example(
                    summary=f"{spec.name} · {method['title']}", value=body
                )
        return examples


def _values(schema: Mapping[str, Any], *, with_defaults: bool) -> dict[str, Any]:
    """Example values of a published object schema.

    Args:
        schema: A schema as ``SourceTypeSpec.describe`` publishes it.
        with_defaults: Also include fields that only have a default.

    Returns:
        Each field's first example, else its default, else a placeholder when required.
    """
    required = set(schema.get("required", ()))
    values: dict[str, Any] = {}
    for name, field in schema["properties"].items():
        if field.get("examples"):
            values[name] = field["examples"][0]
        elif with_defaults and "default" in field:
            values[name] = field["default"]
        elif name in required:
            values[name] = _PLACEHOLDERS.get(field.get("type", "string"), "…")
    return values
