"""Validated MCP configuration loading, resolution, and atomic persistence."""

from __future__ import annotations

import copy
import errno
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

from harborrag_core.contracts.tools import ToolSpec
from harborrag_mcp_server.audit import McpAuditLog
from harborrag_mcp_server.policy import McpToolPolicy

from .models import McpConfiguration, ToolConfiguration
from .validation import clamp_to_safety_maxima, validate_tools

_RESULT_LIMIT_FIELDS = frozenset({"top_k", "limit", "max_paths", "max_nodes", "max_hops"})
_REFUSED_WRITE_ERRORS = frozenset({errno.EACCES, errno.EPERM, errno.EROFS})
_POLICY_ENVIRONMENT = {
    "HARBORRAG_MCP_MAX_RESULTS": "max_results",
    "HARBORRAG_MCP_MAX_ARGUMENT_BYTES": "max_argument_bytes",
    "HARBORRAG_MCP_MAX_OUTPUT_BYTES": "max_output_bytes",
}


class ConfigurationRevisionError(RuntimeError):
    """An update targets a configuration revision that is no longer current.

    The caller reads the configuration again and repeats its change.
    """


class ConfigurationReadOnlyError(RuntimeError):
    """The configuration file cannot be written where this server runs.

    A container that mounts its configuration read-only is the usual case. The
    operator edits the file where it is stored and reloads it.
    """

    def __init__(self) -> None:
        super().__init__(
            "MCP configuration is read-only in this deployment; "
            "edit the file where it is stored and reload it"
        )


@dataclass(frozen=True, slots=True)
class EffectiveToolConfiguration:
    """One tool's settings after the global and tenant overrides are merged.

    Attributes:
        enabled: Whether the tool is advertised and callable.
        defaults: Argument defaults, with result limits capped by the policy.
        limits: Upper bounds for numeric arguments.
    """

    enabled: bool
    defaults: dict[str, Any]
    limits: dict[str, int | float]


class McpConfigurationStore:
    """Thread-safe configuration repository shared by transports and tools.

    Args:
        path: The YAML file the configuration is stored in.
        configuration: The configuration to start from, as stored in that file.
        specs: The tools the configuration may refer to.
        audit: The log that records each configuration change.
        environment: The variables that override the stored policy and disable
            tools. Left out, the process environment is used.
    """

    def __init__(
        self,
        *,
        path: Path,
        configuration: McpConfiguration,
        specs: list[ToolSpec],
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
        specs: list[ToolSpec],
        audit: McpAuditLog,
        environment: Mapping[str, str] | None = None,
    ) -> McpConfigurationStore:
        """Build a store from the configuration file at ``path``.

        A value above a tool's safety maximum is lowered and logged at WARNING, so
        a file written for an older, looser maximum still starts the server.
        ``replace`` stays strict.

        Args:
            path: The YAML file to read and later write.
            specs: The tools the configuration may refer to.
            audit: The log that records each configuration change.
            environment: The variables that override the stored policy and disable
                tools. Left out, the process environment is used.

        Returns:
            The store holding the loaded configuration.
        """
        source = Path(path)
        configuration = clamp_to_safety_maxima(
            _read_configuration(source), {spec.name: spec for spec in specs}
        )
        return cls(
            path=source,
            configuration=configuration,
            specs=specs,
            audit=audit,
            environment=environment,
        )

    def snapshot(self) -> McpConfiguration:
        """Return a copy of the configuration as stored, before environment overrides."""
        with self._lock:
            return self._configuration.model_copy(deep=True)

    def effective(self) -> McpConfiguration:
        """Return a copy of the configuration with environment overrides applied.

        The copy is deep, because a frozen model still shares its nested
        dictionaries and lists with the cached original.
        """
        with self._lock:
            return self._cached().model_copy(deep=True)

    def _cached(self) -> McpConfiguration:
        """Return the cached effective configuration, uncopied, for internal readers.

        ``resolve`` and ``policy`` only read it and deep-copy what they hand back.
        The caller must hold ``self._lock`` and must not change the result.
        """
        if self._effective is None:
            self._effective = _apply_environment(self._configuration, self._environment)
        return self._effective

    def describe(self) -> dict[str, object]:
        """Describe the configuration for the status UI and the management API.

        Returns:
            The stored and effective configuration, the revision, the names of the
            environment overrides, the file path, whether that file can be written,
            and whether the advertised tools changed since start-up.
        """
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
                "writable": _is_writable(self.path),
            }

    def replace(
        self,
        configuration: McpConfiguration,
        *,
        principal_id: str,
        expected_revision: str | None = None,
    ) -> dict[str, object]:
        """Validate, persist and adopt a new configuration, then describe it.

        The file is written before the store adopts the configuration, and the
        change is recorded in the audit log. The description is taken under the
        same lock, so its revision is the one this call wrote.

        Args:
            configuration: The configuration to store.
            principal_id: Who makes the change, for the audit log.
            expected_revision: The revision the caller edited. Left out, the
                current revision is replaced whatever it is.

        Returns:
            The description of the stored configuration, as ``describe`` gives it.

        Raises:
            ConfigurationRevisionError: ``expected_revision`` is no longer current.
            ConfigurationReadOnlyError: The file system refuses the write.
            OSError: The write failed for another reason, such as a full disk.
        """
        validated = self._validate_and_copy(configuration)
        with self._lock:
            current_revision = _revision(self._configuration)
            if expected_revision is not None and expected_revision != current_revision:
                raise ConfigurationRevisionError("MCP configuration revision is stale")
            previous_revision = current_revision
            try:
                _write_configuration(self.path, validated)
            except OSError as exc:
                if exc.errno in _REFUSED_WRITE_ERRORS:
                    raise ConfigurationReadOnlyError from exc
                raise
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
        """Read the configuration file again and adopt it, then describe it.

        The file is read under the lock that ``replace`` holds, so a reload cannot
        overwrite a replacement that was persisted while it ran. Values above a
        safety maximum are lowered as in ``load``.

        Args:
            principal_id: Who asks for the reload, for the audit log.

        Returns:
            The description of the loaded configuration, as ``describe`` gives it.
        """
        with self._lock:
            loaded = self._validate_and_copy(
                clamp_to_safety_maxima(_read_configuration(self.path), self._specs)
            )
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
        """Return the effective tool policy, with environment overrides applied."""
        with self._lock:
            policy = self._cached().policy
        return McpToolPolicy(**policy.model_dump())

    def resolve(self, tool_name: str, tenant_id: str | None) -> EffectiveToolConfiguration:
        """Merge the global and tenant settings of one tool.

        A tenant setting wins over the global one. Result-limit defaults are capped
        at the policy's ``max_results``. The defaults are deep copies, because
        callers merge them into tool payloads that may be changed later.

        Args:
            tool_name: The name of a tool this store was built with.
            tenant_id: The tenant whose overrides apply, or None for the global
                settings alone.

        Returns:
            The tool's effective settings.
        """
        with self._lock:
            configuration = self._cached()
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

    def tool_spec(self, spec: ToolSpec, tenant_id: str | None = None) -> ToolSpec:
        """Return a tool's spec with its configured defaults and maxima in the schema.

        Args:
            spec: The tool's spec as registered.
            tenant_id: The tenant whose overrides apply, or None for the global
                settings alone.

        Returns:
            A copy of ``spec`` whose input schema carries the effective values.
        """
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
        """List the tools that are enabled before any tenant override."""
        return [name for name in self._specs if self.resolve(name, None).enabled]

    def _validate_and_copy(self, configuration: McpConfiguration) -> McpConfiguration:
        """Reject a configuration that is invalid as written or once overrides apply.

        The stored form must be valid alone, so a later environment change cannot
        revive a bad file. The effective form must be valid too, so an override
        cannot push a tool past its schema.
        """
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


def _is_writable(path: Path) -> bool:
    """Whether a save could create its temporary file in the directory of ``path``."""
    directory = path.parent
    while not directory.exists() and directory != directory.parent:
        directory = directory.parent
    return os.access(directory, os.W_OK | os.X_OK)


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
