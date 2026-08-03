"""Validated MCP configuration loading, resolution, and atomic persistence."""

from __future__ import annotations

import copy
import json
import os
import tempfile
import threading
from collections.abc import Mapping
from dataclasses import dataclass, replace
from hashlib import sha256
from pathlib import Path
from typing import Any

import yaml

from harborrag_mcp_server.audit import McpAuditLog
from harborrag_mcp_server.policy import McpToolPolicy
from harborrag_mcp_server.tools.base import McpToolSpec

from .models import McpConfiguration, ToolConfiguration
from .validation import validate_tools

_RESULT_LIMIT_FIELDS = frozenset({"top_k", "limit", "max_paths", "max_nodes"})
_POLICY_ENVIRONMENT = {
    "HARBORRAG_MCP_MAX_RESULTS": "max_results",
    "HARBORRAG_MCP_MAX_ARGUMENT_BYTES": "max_argument_bytes",
    "HARBORRAG_MCP_MAX_OUTPUT_BYTES": "max_output_bytes",
}


class ConfigurationRevisionError(RuntimeError):
    """Raised when an update targets a stale configuration revision."""


@dataclass(frozen=True, slots=True)
class EffectiveToolConfiguration:
    enabled: bool
    defaults: dict[str, Any]
    limits: dict[str, int | float]


class McpConfigurationStore:
    """Thread-safe configuration repository shared by transports and tools."""

    def __init__(
        self,
        *,
        path: Path,
        configuration: McpConfiguration,
        specs: list[McpToolSpec],
        audit: McpAuditLog,
        environment: Mapping[str, str] | None = None,
    ) -> None:
        self.path = path
        self.audit = audit
        self._specs = {spec.name: spec for spec in specs}
        self._environment = dict(os.environ if environment is None else environment)
        self._lock = threading.RLock()
        self._configuration = self._validate_and_copy(configuration)
        self._effective: McpConfiguration | None = None
        self._startup_advertisement = self._advertisement_digest(self.effective())

    @classmethod
    def load(
        cls,
        *,
        path: str | Path,
        specs: list[McpToolSpec],
        audit: McpAuditLog,
        environment: Mapping[str, str] | None = None,
    ) -> McpConfigurationStore:
        source = Path(path)
        configuration = _read_configuration(source)
        return cls(
            path=source,
            configuration=configuration,
            specs=specs,
            audit=audit,
            environment=environment,
        )

    def snapshot(self) -> McpConfiguration:
        with self._lock:
            return self._configuration.model_copy(deep=True)

    def effective(self) -> McpConfiguration:
        with self._lock:
            if self._effective is None:
                self._effective = _apply_environment(self._configuration, self._environment)
            return self._effective

    def describe(self) -> dict[str, object]:
        with self._lock:
            source = self._configuration.model_copy(deep=True)
            effective = _apply_environment(source, self._environment)
            return {
                "configuration": source.model_dump(mode="json", exclude_none=True),
                "effective": effective.model_dump(mode="json", exclude_none=True),
                "revision": _revision(source),
                "restart_required": (
                    self._advertisement_digest(effective) != self._startup_advertisement
                ),
                "environment_overrides": _environment_override_names(self._environment),
                "path": str(self.path),
            }

    def replace(
        self,
        configuration: McpConfiguration,
        *,
        principal_id: str,
        expected_revision: str | None = None,
    ) -> dict[str, object]:
        validated = self._validate_and_copy(configuration)
        # RLock is reentrant, so describing inside the critical section is safe and keeps the
        # returned revision the one this call actually wrote.
        with self._lock:
            current_revision = _revision(self._configuration)
            if expected_revision is not None and expected_revision != current_revision:
                raise ConfigurationRevisionError("MCP configuration revision is stale")
            previous_revision = current_revision
            _write_configuration(self.path, validated)
            self._configuration = validated
            self._effective = None
            self.audit.configuration_change(
                action="replace",
                principal_id=principal_id,
                previous_revision=previous_revision,
                current_revision=_revision(validated),
            )
            return self.describe()

    def reload(self, *, principal_id: str) -> dict[str, object]:
        loaded = self._validate_and_copy(_read_configuration(self.path))
        with self._lock:
            previous_revision = _revision(self._configuration)
            self._configuration = loaded
            self._effective = None
            self.audit.configuration_change(
                action="reload",
                principal_id=principal_id,
                previous_revision=previous_revision,
                current_revision=_revision(loaded),
            )
            return self.describe()

    def policy(self) -> McpToolPolicy:
        policy = self.effective().policy
        return McpToolPolicy(**policy.model_dump())

    def resolve(self, tool_name: str, tenant_id: str | None) -> EffectiveToolConfiguration:
        configuration = self.effective()
        global_override = configuration.tools.get(tool_name, ToolConfiguration())
        tenant_override = ToolConfiguration()
        if tenant_id is not None and tenant_id in configuration.tenants:
            tenant_override = configuration.tenants[tenant_id].tools.get(
                tool_name,
                ToolConfiguration(),
            )
        enabled = True
        if global_override.enabled is not None:
            enabled = global_override.enabled
        if tenant_override.enabled is not None:
            enabled = tenant_override.enabled
        # effective() now returns a shared cached model, so deep-copy the default values:
        # {**a, **b} is shallow, and callers merge these into tool payloads that may be
        # mutated downstream. Still far cheaper than revalidating the whole configuration.
        defaults = copy.deepcopy({**global_override.defaults, **tenant_override.defaults})
        properties = self._specs[tool_name].input_schema.get("properties", {})
        if isinstance(properties, dict):
            for field_name in _RESULT_LIMIT_FIELDS & set(properties):
                field_schema = properties[field_name]
                if not isinstance(field_schema, dict):
                    continue
                default = defaults.get(field_name, field_schema.get("default"))
                if isinstance(default, (int, float)):
                    defaults[field_name] = min(default, configuration.policy.max_results)
        return EffectiveToolConfiguration(
            enabled=enabled,
            defaults=defaults,
            limits=dict(global_override.limits | tenant_override.limits),
        )

    def tool_spec(self, spec: McpToolSpec, tenant_id: str | None = None) -> McpToolSpec:
        resolved = self.resolve(spec.name, tenant_id)
        schema = copy.deepcopy(spec.input_schema)
        properties = schema.get("properties", {})
        if isinstance(properties, dict):
            for field_name, default in resolved.defaults.items():
                field_schema = properties.get(field_name)
                if isinstance(field_schema, dict):
                    field_schema["default"] = copy.deepcopy(default)
            for field_name, limit in resolved.limits.items():
                field_schema = properties.get(field_name)
                if isinstance(field_schema, dict):
                    field_schema["maximum"] = limit
            max_results = self.policy().max_results
            for field_name in _RESULT_LIMIT_FIELDS:
                field_schema = properties.get(field_name)
                if isinstance(field_schema, dict) and "maximum" in field_schema:
                    field_schema["maximum"] = min(field_schema["maximum"], max_results)
        return replace(spec, input_schema=schema)

    def enabled_tool_names(self) -> list[str]:
        return [name for name in self._specs if self.resolve(name, None).enabled]

    def _validate_and_copy(self, configuration: McpConfiguration) -> McpConfiguration:
        """Reject a configuration that is invalid as written or once env overrides apply."""

        # Both passes are required: the stored form must be valid on its own so a later
        # environment change cannot resurrect a bad file, and the effective form must be
        # valid so an override cannot push a tool past its schema at runtime.
        validate_tools(configuration, self._specs)
        validate_tools(_apply_environment(configuration, self._environment), self._specs)
        return configuration.model_copy(deep=True)

    def _advertisement_digest(self, configuration: McpConfiguration) -> str:
        value = {
            "policy": configuration.policy.model_dump(mode="json"),
            "tools": {
                name: configuration.tools.get(name, ToolConfiguration()).model_dump(
                    mode="json",
                    exclude_none=True,
                )
                for name in self._specs
            },
        }
        return _digest(value)


def _read_configuration(path: Path) -> McpConfiguration:
    if not path.is_file():
        raise ValueError(f"MCP configuration does not exist: {path}")
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except (OSError, yaml.YAMLError) as exc:
        raise ValueError(f"Unable to read MCP configuration: {path}") from exc
    if not isinstance(raw, dict):
        raise ValueError("MCP configuration root must be a mapping")
    return McpConfiguration.model_validate(raw)


def _write_configuration(path: Path, configuration: McpConfiguration) -> None:
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    payload = yaml.safe_dump(
        configuration.model_dump(mode="json", exclude_none=True),
        sort_keys=False,
        allow_unicode=True,
    )
    descriptor, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    temporary = Path(temporary_name)
    try:
        os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    except BaseException:
        temporary.unlink(missing_ok=True)
        raise


def _apply_environment(
    configuration: McpConfiguration,
    environment: Mapping[str, str],
) -> McpConfiguration:
    raw = configuration.model_dump(mode="python")
    policy = raw["policy"]
    for variable, field_name in _POLICY_ENVIRONMENT.items():
        if variable in environment and environment[variable].strip():
            try:
                policy[field_name] = int(environment[variable])
            except ValueError as exc:
                raise ValueError(f"{variable} must be an integer") from exc
    disabled = {
        name.strip()
        for name in environment.get("HARBORRAG_MCP_DISABLED_TOOLS", "").split(",")
        if name.strip()
    }
    tools = raw["tools"]
    for tool_name in disabled:
        override = tools.setdefault(tool_name, {})
        override["enabled"] = False
    return McpConfiguration.model_validate(raw)


def _revision(configuration: McpConfiguration) -> str:
    return _digest(configuration.model_dump(mode="json", exclude_none=True))


def _digest(value: object) -> str:
    serialized = json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False)
    return sha256(serialized.encode("utf-8")).hexdigest()


def _environment_override_names(environment: Mapping[str, str]) -> list[str]:
    names = [name for name in _POLICY_ENVIRONMENT if environment.get(name, "").strip()]
    if environment.get("HARBORRAG_MCP_DISABLED_TOOLS", "").strip():
        names.append("HARBORRAG_MCP_DISABLED_TOOLS")
    return sorted(names)


__all__ = [
    "ConfigurationRevisionError",
    "EffectiveToolConfiguration",
    "McpConfigurationStore",
]
