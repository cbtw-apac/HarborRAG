"""Connector declarations and a registry that refuses broken connectors at startup.

A connector class declares itself with ``@connector(...)``: its name, typed
configuration, and checkpoint model. The registry maps names to
``"module:Class"`` strings and imports a connector only when it is first
asked for, so a worker never pays for vendor code it does not run.
``validate_all`` imports and checks every registered connector, so a broken
one fails the deployment at boot rather than in the middle of a sync.
"""

from __future__ import annotations

import importlib
import inspect
import typing
from collections.abc import Callable
from dataclasses import dataclass
from threading import Lock
from typing import Any, Literal

from pydantic import BaseModel

from harborrag_core.sources import Capabilities, ConfigError, ConnectorConfig, field_classes

_ASYNC_ITERATOR_METHODS = ("fetch", "fetch_keys", "list_keys", "open_blob")
_REQUIRED_CREATE_ARGS = frozenset({"source_id", "auth", "http", "config", "logger"})


@dataclass(frozen=True, slots=True)
class ConnectorSpec:
    """What the runtime needs to know to build and drive one connector."""

    name: str
    cls: type[Any]
    config: type[ConnectorConfig]
    checkpoint: type[BaseModel]
    aliases: tuple[str, ...] = ()

    @property
    def capabilities(self) -> Capabilities:
        capabilities: Capabilities = self.cls.capabilities
        return capabilities


def connector[T: type[Any]](
    name: str,
    *,
    config: type[ConnectorConfig],
    checkpoint: type[BaseModel],
    aliases: tuple[str, ...] = (),
) -> Callable[[T], T]:
    """Declare a class as the ``name`` source connector."""

    def declare(cls: T) -> T:
        cls.__connector_spec__ = ConnectorSpec(name, cls, config, checkpoint, aliases)
        return cls

    return declare


def _check_create(cls: type[Any]) -> list[str]:
    create = inspect.getattr_static(cls, "create", None)
    if not isinstance(create, classmethod):
        return ["create must be a classmethod"]
    if not inspect.iscoroutinefunction(create.__func__):
        return ["create must be async"]
    parameters = inspect.signature(create.__func__).parameters
    missing = _REQUIRED_CREATE_ARGS - set(parameters)
    return [f"create is missing keyword arguments {sorted(missing)}"] if missing else []


def _check_methods(cls: type[Any]) -> list[str]:
    problems: list[str] = []
    for method in _ASYNC_ITERATOR_METHODS:
        function = getattr(cls, method, None)
        if function is None:
            problems.append(f"{method} is missing")
        elif not inspect.isasyncgenfunction(function):
            problems.append(f"{method} must be an async generator")
    for method in ("preflight",):
        if not inspect.iscoroutinefunction(getattr(cls, method, None)):
            problems.append(f"{method} must be async")
    if not callable(getattr(cls, "parse_checkpoint", None)):
        problems.append("parse_checkpoint is missing")
    return problems


def _check_models(spec: ConnectorSpec) -> list[str]:
    problems: list[str] = []
    if not isinstance(getattr(spec.cls, "capabilities", None), Capabilities):
        problems.append("capabilities must be a Capabilities instance")
    try:
        field_classes(spec.config)
    except ConfigError as exc:
        problems.append(str(exc))
    version = spec.checkpoint.model_fields.get("version")
    if version is None or typing.get_origin(version.annotation) is not Literal:
        problems.append("the checkpoint model needs a `version: Literal[...]` field")
    return problems


def validate_connector(cls: type[Any]) -> list[str]:
    """Every reason ``cls`` cannot be used as a source connector; empty when it can."""

    spec = getattr(cls, "__connector_spec__", None)
    if not isinstance(spec, ConnectorSpec) or spec.cls is not cls:
        return [f"{cls.__name__} is not declared with @connector"]
    problems = _check_create(cls) + _check_methods(cls) + _check_models(spec)
    return [f"{spec.name}: {problem}" for problem in problems]


class SourceConnectorRegistry:
    """Name to connector spec, imported lazily and validated on first use."""

    def __init__(self) -> None:
        self._targets: dict[str, str] = {}
        self._aliases: dict[str, str] = {}
        self._loaded: dict[str, ConnectorSpec] = {}
        self._lock = Lock()

    def register_lazy(self, name: str, target: str, *, aliases: tuple[str, ...] = ()) -> None:
        """Register ``"package.module:ClassName"`` under ``name``."""

        if ":" not in target:
            raise ValueError(f"target must look like 'module:Class', got {target!r}")
        with self._lock:
            for label in (name, *aliases):
                if label in self._targets or label in self._aliases:
                    raise ValueError(f"source connector {label!r} is already registered")
            self._targets[name] = target
            self._aliases.update(dict.fromkeys(aliases, name))

    def names(self) -> tuple[str, ...]:
        return tuple(sorted(self._targets))

    def resolve(self, name: str) -> str:
        """The canonical name for ``name`` or one of its aliases."""

        canonical = self._aliases.get(name, name)
        if canonical not in self._targets:
            raise ConfigError(f"unknown source connector {name!r}")
        return canonical

    def get(self, name: str) -> ConnectorSpec:
        canonical = self.resolve(name)
        with self._lock:
            spec = self._loaded.get(canonical)
            if spec is None:
                spec = self._load(canonical)
                self._loaded[canonical] = spec
        return spec

    def validate_all(self) -> list[str]:
        """Import and check every connector; return all problems found."""

        problems: list[str] = []
        for name in self.names():
            try:
                self.get(name)
            except (ConfigError, ImportError, AttributeError) as exc:
                problems.append(str(exc))
        return problems

    def _load(self, name: str) -> ConnectorSpec:
        module_name, _, attribute = self._targets[name].partition(":")
        cls = getattr(importlib.import_module(module_name), attribute)
        problems = validate_connector(cls)
        if problems:
            raise ConfigError("; ".join(problems))
        spec: ConnectorSpec = cls.__connector_spec__
        if spec.name != name:
            raise ConfigError(
                f"{self._targets[name]} declares itself as {spec.name!r}, not {name!r}"
            )
        return spec


source_registry = SourceConnectorRegistry()
"""Connectors register here as they land (Jira in P3, Confluence and local files in P4, ...)."""
