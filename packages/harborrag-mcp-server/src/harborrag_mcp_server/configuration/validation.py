"""Schema validation for MCP tool configuration overrides."""

from __future__ import annotations

import logging
from collections.abc import Mapping
from typing import Any

from jsonschema.exceptions import ValidationError
from jsonschema.validators import validator_for

from harborrag_core.contracts.tools import ToolSpec

from .models import McpConfiguration, ToolConfiguration

logger = logging.getLogger("harborrag.mcp.configuration")


def validate_tools(
    configuration: McpConfiguration,
    specs: Mapping[str, ToolSpec],
) -> None:
    configured_names = set(configuration.tools)
    for tenant in configuration.tenants.values():
        configured_names.update(tenant.tools)
    unknown = configured_names - set(specs)
    if unknown:
        raise ValueError(f"Unknown MCP tools: {', '.join(sorted(unknown))}")
    for tool_name, override in configuration.tools.items():
        _validate_tool_override(tool_name, override, specs[tool_name])
    for tenant_id, tenant in configuration.tenants.items():
        for tool_name, override in tenant.tools.items():
            global_override = configuration.tools.get(tool_name, ToolConfiguration())
            merged = ToolConfiguration(
                enabled=(
                    override.enabled if override.enabled is not None else global_override.enabled
                ),
                defaults={**global_override.defaults, **override.defaults},
                limits={**global_override.limits, **override.limits},
            )
            _validate_tool_override(
                tool_name,
                merged,
                specs[tool_name],
            )
            if "tenant_id" in override.defaults:
                raise ValueError(
                    f"Tenant {tenant_id!r} must not configure a tenant_id tool default"
                )


def _validate_tool_override(
    tool_name: str,
    override: ToolConfiguration,
    spec: ToolSpec,
) -> None:
    properties = spec.input_schema.get("properties", {})
    if not isinstance(properties, dict):
        properties = {}
    unknown = (set(override.defaults) | set(override.limits)) - set(properties)
    if unknown:
        raise ValueError(f"Unknown fields for MCP tool {tool_name}: {', '.join(sorted(unknown))}")
    required = set(spec.input_schema.get("required", []))
    protected_defaults = set(override.defaults) & (required | {"tenant_id"})
    if protected_defaults:
        raise ValueError(
            f"MCP tool {tool_name} must not default required or tenant fields: "
            f"{', '.join(sorted(protected_defaults))}"
        )
    for field_name, value in override.defaults.items():
        _validate_property(tool_name, field_name, value, properties[field_name])
    for field_name, limit in override.limits.items():
        _validate_limit(
            tool_name,
            field_name,
            limit,
            properties[field_name],
            override.defaults,
        )


def _validate_limit(
    tool_name: str,
    field_name: str,
    limit: int | float,
    schema: object,
    defaults: Mapping[str, Any],
) -> None:
    field_schema = schema if isinstance(schema, dict) else {}
    field_type = field_schema.get("type")
    if field_type not in {"integer", "number"}:
        raise ValueError(f"MCP tool {tool_name}.{field_name} does not support a numeric limit")
    built_in_maximum = field_schema.get("maximum")
    if built_in_maximum is not None and limit > built_in_maximum:
        raise ValueError(f"MCP tool {tool_name}.{field_name} limit exceeds its safety maximum")
    minimum = field_schema.get("minimum")
    if minimum is not None and limit < minimum:
        raise ValueError(f"MCP tool {tool_name}.{field_name} limit is below its minimum")
    if field_type == "integer" and not isinstance(limit, int):
        raise ValueError(f"MCP tool {tool_name}.{field_name} limit must be an integer")
    default = defaults.get(field_name, field_schema.get("default"))
    if isinstance(default, (int, float)) and default > limit:
        raise ValueError(f"MCP tool {tool_name}.{field_name} default exceeds its configured limit")


def _validate_property(
    tool_name: str,
    field_name: str,
    value: Any,
    schema: object,
) -> None:
    if not isinstance(schema, dict):
        raise ValueError(f"MCP tool {tool_name}.{field_name} has no configurable schema")
    try:
        validator = validator_for(schema)
        validator.check_schema(schema)
        validator(schema).validate(value)
    except ValidationError as exc:
        raise ValueError(f"Invalid default for MCP tool {tool_name}.{field_name}") from exc


def clamp_to_safety_maxima(
    configuration: McpConfiguration,
    specs: Mapping[str, ToolSpec],
) -> McpConfiguration:
    """Lower configured numeric defaults and limits above a tool's safety maximum.

    Applied to a file read from disk only. A tool's built-in ``maximum`` can
    tighten between releases (alpha2 capped graph ``max_depth`` at 4 and
    ``max_paths`` at 5), and a file written for the older ceiling must not stop
    the server starting. Each lowered value is logged as a WARNING so the
    operator can update the file; the configuration editor still rejects them.
    """

    raw = configuration.model_dump(mode="python")
    scopes = [("", raw["tools"])] + [
        (f"tenant {tenant_id!r} ", tenant["tools"]) for tenant_id, tenant in raw["tenants"].items()
    ]
    changed = False
    for scope, tools in scopes:
        for tool_name, override in tools.items():
            spec = specs.get(tool_name)
            properties = spec.input_schema.get("properties") if spec is not None else None
            if isinstance(properties, dict):
                for section in ("defaults", "limits"):
                    label = f"{scope}{tool_name}"
                    changed |= _clamp_section(label, section, override[section], properties)
    return McpConfiguration.model_validate(raw) if changed else configuration


def _clamp_section(
    label: str,
    section: str,
    values: dict[str, Any],
    properties: Mapping[str, object],
) -> bool:
    changed = False
    for field_name, value in values.items():
        field_schema = properties.get(field_name)
        maximum = field_schema.get("maximum") if isinstance(field_schema, dict) else None
        if (
            isinstance(value, bool)
            or not isinstance(value, (int, float))
            or not isinstance(maximum, (int, float))
            or value <= maximum
        ):
            continue
        logger.warning(
            "MCP configuration %s.%s %s %s exceeds its safety maximum %s; using %s. "
            "Lower it in the configuration file.",
            label,
            field_name,
            section.removesuffix("s"),
            value,
            maximum,
            maximum,
        )
        values[field_name] = maximum
        changed = True
    return changed


__all__ = ["clamp_to_safety_maxima", "validate_tools"]
