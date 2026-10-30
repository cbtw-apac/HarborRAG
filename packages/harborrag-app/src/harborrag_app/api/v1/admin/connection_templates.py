"""Connection templates for Swagger UI: one complete request body per source type deployment."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any, Self

from fastapi.openapi.models import Example
from pydantic import BaseModel
from pydantic.fields import FieldInfo
from pydantic_core import PydanticUndefined

from harborrag_runtime.config.connectors.models import (
    METHOD_KEY,
    Deployment,
    SourceTypeRegistry,
    SourceTypeSpec,
)

_PLACEHOLDERS: Mapping[object, Any] = {str: "…", int: 0, float: 0.0, bool: False}


class ConnectionTemplateBuilder:
    """Build one connection template per deployment, such as Jira Cloud or Jira Data Center.

    A template lists every config and advanced field with the value a sync
    would use when it is left as is; ``FieldValueFactory`` picks each value.
    """

    def __init__(self, registry: SourceTypeRegistry) -> None:
        self._registry = registry
        self._sync_now: bool | None = None

    def with_sync_now(self, value: bool = False) -> Self:
        """Add ``sync_now`` to every template, as the ``PUT`` route takes it."""
        self._sync_now = value
        return self

    def build(self) -> dict[str, Example]:
        """Return the templates keyed ``<source_type>-<deployment>``, in registration order."""
        templates: dict[str, Example] = {}
        for spec in self._registry.all():
            defaults = self._registry.connector_defaults(spec.source_type)
            for deployment in spec.deployments:
                body = self._body(spec, deployment, defaults)
                templates[f"{spec.source_type}-{deployment.id}"] = Example(
                    summary=deployment.title,
                    description=f"Template: {deployment.note}. Replace the placeholder values.",
                    value=body,
                )
        return templates

    def _body(
        self, spec: SourceTypeSpec, deployment: Deployment, defaults: Mapping[str, Any]
    ) -> dict[str, Any]:
        """Return the request body of one deployment."""
        method = spec.auth_method(deployment.method)
        values = FieldValueFactory(deployment.credentials).values(method.model)
        credentials = {name: value for name, value in values.items() if value is not None}
        if spec.has_method_choice:
            credentials = {METHOD_KEY: method.id, **credentials}
        body: dict[str, Any] = {
            "source_type": spec.source_type,
            "name": deployment.title,
            "credentials": credentials,
            "config": FieldValueFactory(deployment.config, defaults).values(spec.config_model),
            "advanced": FieldValueFactory(connector_defaults=defaults).values(spec.tuning_model),
        }
        if self._sync_now is not None:
            body["sync_now"] = self._sync_now
        return body


@dataclass(frozen=True, slots=True)
class FieldValueFactory:
    """Create a template value for each field of a model from the first source that has one.

    The sources, in order: the deployment's preset, the field's first example,
    the connector's own default, the schema default, and for a required field
    a placeholder of its type.
    """

    presets: Mapping[str, Any] = field(default_factory=dict)
    connector_defaults: Mapping[str, Any] = field(default_factory=dict)

    def values(self, model: type[BaseModel]) -> dict[str, Any]:
        """Return a value for every field of ``model``, in declaration order."""
        return {name: self.value(name, info) for name, info in model.model_fields.items()}

    def value(self, name: str, info: FieldInfo) -> Any:
        """Return the value of one field."""
        if name in self.presets:
            return self.presets[name]
        if info.examples:
            return info.examples[0]
        if name in self.connector_defaults:
            return self.connector_defaults[name]
        if info.default is not PydanticUndefined or info.default_factory is not None:
            return info.get_default(call_default_factory=True)
        return _PLACEHOLDERS.get(info.annotation, "…")
