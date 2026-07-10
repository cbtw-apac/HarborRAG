from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from types import MappingProxyType
from typing import Any

from harborrag_adapters.parsers.base import BaseParser
from harborrag_adapters.parsers.engine import HarborParser
from harborrag_adapters.parsers.pdf_engine import PdfBackend

from harborrag_runtime.config.errors import ParserConfigurationError
from harborrag_runtime.config.parsers.providers import (
    parser_factory,
    pdf_backend_spec,
)


@dataclass(frozen=True, slots=True)
class PdfBackendDefinition:
    """One PDF backend in an explicitly ordered backend chain."""

    backend: str
    settings: Mapping[str, Any] = field(default_factory=dict, repr=False)
    secret_environment: Mapping[str, str] = field(default_factory=dict, repr=False)
    process_environment: Mapping[str, str] = field(default_factory=dict, repr=False)

    def __post_init__(self) -> None:
        """Copy backend configuration into read-only mappings."""
        object.__setattr__(self, "settings", MappingProxyType(dict(self.settings)))
        object.__setattr__(
            self,
            "secret_environment",
            MappingProxyType(dict(self.secret_environment)),
        )
        object.__setattr__(
            self,
            "process_environment",
            MappingProxyType(dict(self.process_environment)),
        )

    def build(
        self,
        *,
        parser_name: str,
        environment: Mapping[str, str] | None = None,
    ) -> PdfBackend:
        """Build the configured PDF backend with contextual error reporting."""
        spec = pdf_backend_spec(self.backend)
        if spec is None:  # Defensive for manually-created definitions.
            raise ParserConfigurationError(
                f"Parser {parser_name!r} uses unsupported PDF backend {self.backend!r}"
            )
        values = dict(self.settings)
        source_environment = os.environ if environment is None else environment
        for setting, variable_name in self.secret_environment.items():
            values[setting] = _required_environment_value(
                source_environment,
                variable_name,
                parser_name=parser_name,
                backend=self.backend,
            )
        if self.process_environment:
            values["env"] = {
                target: _required_environment_value(
                    source_environment,
                    variable_name,
                    parser_name=parser_name,
                    backend=self.backend,
                )
                for target, variable_name in self.process_environment.items()
            }

        try:
            return spec.build(values)
        except (TypeError, ValueError) as exc:
            raise ParserConfigurationError(
                f"Parser {parser_name!r} PDF backend {self.backend!r} is invalid: {exc}"
            ) from exc


@dataclass(frozen=True, slots=True)
class ParserDefinition:
    """One named parser construction recipe loaded from YAML."""

    name: str
    parser: str
    enabled: bool = True
    settings: Mapping[str, Any] = field(default_factory=dict, repr=False)
    pdf_backends: tuple[PdfBackendDefinition, ...] = ()

    def __post_init__(self) -> None:
        """Copy parser settings into a read-only mapping."""
        object.__setattr__(self, "settings", MappingProxyType(dict(self.settings)))

    def build(
        self,
        *,
        environment: Mapping[str, str] | None = None,
        overrides: Mapping[str, Any] | None = None,
    ) -> BaseParser[Any, Any]:
        """Build the parser after merging YAML settings and code overrides."""
        factory = parser_factory(self.parser)
        if factory is None:  # Defensive for manually-created definitions.
            raise ParserConfigurationError(
                f"Parser {self.name!r} uses unsupported parser {self.parser!r}"
            )

        values = dict(self.settings)
        if self.pdf_backends:
            values["backends"] = [
                backend.build(parser_name=self.name, environment=environment)
                for backend in self.pdf_backends
            ]
        if overrides:
            values.update(overrides)

        try:
            return factory(**values)
        except (TypeError, ValueError) as exc:
            raise ParserConfigurationError(
                f"Parser {self.name!r} ({self.parser}) is invalid: {exc}"
            ) from exc


@dataclass(frozen=True, slots=True)
class ParserCatalog:
    """A versioned, immutable collection of named parser definitions."""

    parsers: Mapping[str, ParserDefinition]
    source_path: Path
    version: int

    def __post_init__(self) -> None:
        """Copy the parser map into a read-only view."""
        object.__setattr__(self, "parsers", MappingProxyType(dict(self.parsers)))

    def get(self, name: str) -> ParserDefinition:
        """Return one parser definition by application-level name."""
        try:
            return self.parsers[name]
        except KeyError as exc:
            raise ParserConfigurationError(
                f"Unknown configured parser: {name!r}"
            ) from exc

    def names(self, *, enabled_only: bool = False) -> list[str]:
        """Return configured names alphabetically, optionally filtering disabled."""
        return sorted(
            name
            for name, definition in self.parsers.items()
            if definition.enabled or not enabled_only
        )

    def build(
        self,
        name: str,
        *,
        environment: Mapping[str, str] | None = None,
        overrides: Mapping[str, Any] | None = None,
    ) -> BaseParser[Any, Any]:
        """Build one configured parser by application-level name."""
        return self.get(name).build(environment=environment, overrides=overrides)

    def build_enabled(
        self,
        *,
        environment: Mapping[str, str] | None = None,
    ) -> dict[str, BaseParser[Any, Any]]:
        """Build all enabled parser definitions in their YAML order."""
        return {
            name: definition.build(environment=environment)
            for name, definition in self.parsers.items()
            if definition.enabled
        }

    def build_harbor_parser(
        self,
        *,
        environment: Mapping[str, str] | None = None,
    ) -> HarborParser:
        """Replace matching default parsers with enabled configured parsers.

        Exactly one enabled definition may exist for a stable parser type. All
        parser types not configured remain on ``HarborParser``'s default stack.
        """
        configured: dict[str, tuple[str, BaseParser[Any, Any]]] = {}
        for config_name, parser in self.build_enabled(environment=environment).items():
            existing = configured.get(parser.name)
            if existing is not None:
                raise ParserConfigurationError(
                    f"Enabled parser definitions {existing[0]!r} and "
                    f"{config_name!r} both configure parser {parser.name!r}"
                )
            configured[parser.name] = (config_name, parser)

        parser_stack: list[BaseParser[Any, Any]] = []
        for default in HarborParser.default_parsers():
            replacement = configured.pop(default.name, None)
            parser_stack.append(default if replacement is None else replacement[1])
        parser_stack.extend(parser for _, parser in configured.values())
        return HarborParser(parser_stack)


def _required_environment_value(
    environment: Mapping[str, str],
    variable_name: str,
    *,
    parser_name: str,
    backend: str,
) -> str:
    """Return one referenced environment value or raise a contextual error."""
    value = environment.get(variable_name)
    if value is None or value == "":
        raise ParserConfigurationError(
            f"Parser {parser_name!r} PDF backend {backend!r} requires "
            f"environment variable {variable_name!r}"
        )
    return value
