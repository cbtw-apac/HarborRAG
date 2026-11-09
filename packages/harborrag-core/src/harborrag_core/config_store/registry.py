"""The runtime settings registry: the six keys, their groups, levels and defaults.

Pure; no I/O. Group models are the single source of each key's type, range,
default, levels and ``applies``. Boot-only keys never enter the registry.

Entry point: ``SettingsRegistry.default()``.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field, replace
from typing import Any, Literal

from pydantic import Field, ValidationError

from harborrag_core.base import StrictModel
from harborrag_core.config_store.levels import LevelKind
from harborrag_core.contracts.errors import HarborValidationError

Applies = Literal["live", "next_ingestion", "reindex"]

BOOT_ONLY_PREFIXES: tuple[str, ...] = (
    "database.",
    "redis.",
    "temporal.",
    "qdrant.",
    "graph.",
    "listeners.",
    "auth.",
    "telemetry.",
    "secrets.",
)

GROUP_TITLES: Mapping[str, str] = {
    "retrieval": "Retrieval",
    "parsing": "Parsing",
    "chunking": "Chunking",
    "ingestion": "Ingestion",
    "models": "Models",
}

_SCHEMA_KEYS = ("type", "minimum", "maximum", "enum", "pattern")


def is_boot_only(key: str) -> bool:
    """Return whether ``key`` belongs to a boot-only group set in the environment."""
    return key.startswith(BOOT_ONLY_PREFIXES)


def _extra(levels: list[LevelKind], applies: Applies) -> dict[str, Any]:
    return {"levels": levels, "applies": applies}


class RetrievalGroup(StrictModel):
    """Retrieval settings."""

    top_k: int = Field(
        5,
        ge=1,
        le=100,
        description="Passages the chat retrieval and the MCP vector_search return by default",
        json_schema_extra=_extra(["tenant", "project"], "live"),
    )
    rerank: bool = Field(
        False,
        description="Rerank retrieved passages; refused until a rerank stage exists",
        json_schema_extra=_extra(["tenant", "project"], "live"),
    )


class ParsingGroup(StrictModel):
    """Parsing settings."""

    pdf_parser: Literal["liteparse", "docling"] = Field(
        "liteparse",
        description="Engine that parses PDF documents",
        json_schema_extra=_extra(["tenant", "project", "connection"], "next_ingestion"),
    )


class ChunkingGroup(StrictModel):
    """Chunking settings."""

    max_tokens: int = Field(
        1200,
        ge=64,
        le=4096,
        description="Largest chunk size in tokens; caps every chunking profile",
        json_schema_extra=_extra(["tenant", "project", "connection"], "next_ingestion"),
    )


class IngestionGroup(StrictModel):
    """Ingestion settings."""

    concurrency: int = Field(
        8,
        ge=1,
        le=64,
        description="Documents processed at once by one ingestion run",
        json_schema_extra=_extra(["tenant", "connection"], "next_ingestion"),
    )


class ModelsGroup(StrictModel):
    """Model settings."""

    embedding: str | None = Field(
        None,
        pattern=r"^[A-Za-z0-9_.-]+:\S+$",
        description="Embedding model as provider:model; changing it needs a re-index",
        json_schema_extra=_extra(["tenant", "project"], "reindex"),
    )


SETTINGS_GROUPS: Mapping[str, type[StrictModel]] = {
    "retrieval": RetrievalGroup,
    "parsing": ParsingGroup,
    "chunking": ChunkingGroup,
    "ingestion": IngestionGroup,
    "models": ModelsGroup,
}


@dataclass(frozen=True, slots=True)
class SettingDefinition:
    """One registered setting.

    Args:
        key: Dotted key, ``<group>.<field>``.
        levels: Levels at which an override may exist.
        applies: When a change takes effect.
        default: Value used when no level overrides the key.
        schema: JSON-schema fragment of the value: type, bounds, enum or pattern.
        description: One-line summary of what the key controls.
    """

    key: str
    levels: tuple[LevelKind, ...]
    applies: Applies
    default: object
    schema: Mapping[str, object] = field(compare=False)
    description: str = field(default="", compare=False)

    def validate(self, value: object) -> object:
        """Validate ``value`` strictly against the group field, so ``True`` is not an int.

        Args:
            value: Candidate override value.

        Returns:
            The validated value.

        Raises:
            HarborValidationError: The value is of the wrong type or out of range
                (``field = key``).
        """
        group_name, field_name = self.key.split(".", 1)
        try:
            model = SETTINGS_GROUPS[group_name].model_validate({field_name: value}, strict=True)
        except ValidationError as error:
            reason = "; ".join(str(item["msg"]) for item in error.errors())
            raise HarborValidationError(
                f"{self.key} is invalid: {reason}", details={"field": self.key}
            ) from error
        return getattr(model, field_name)


def _flat_schema(raw: Mapping[str, Any]) -> dict[str, object]:
    branches = raw.get("anyOf")
    if isinstance(branches, list):
        raw = next(branch for branch in branches if branch.get("type") != "null")
    return {name: raw[name] for name in _SCHEMA_KEYS if name in raw}


def _definitions() -> dict[str, SettingDefinition]:
    definitions: dict[str, SettingDefinition] = {}
    for group_name, group in SETTINGS_GROUPS.items():
        properties = group.model_json_schema()["properties"]
        for field_name, info in group.model_fields.items():
            extra = info.json_schema_extra
            assert isinstance(extra, dict)
            key = f"{group_name}.{field_name}"
            levels: tuple[LevelKind, ...] = tuple(extra["levels"])  # type: ignore[arg-type]  # extra is a plain dict of lists
            definitions[key] = SettingDefinition(
                key=key,
                levels=levels,
                applies=extra["applies"],  # type: ignore[arg-type]  # extra values are Applies literals
                default=info.default,
                schema=_flat_schema(properties[field_name]),
                description=info.description or "",
            )
    return definitions


@dataclass(frozen=True, slots=True)
class SettingsRegistry:
    """The set of settings keys the service accepts.

    Args:
        definitions: Definitions by key.
    """

    definitions: Mapping[str, SettingDefinition]

    @classmethod
    def default(cls) -> SettingsRegistry:
        """Build the registry with the shipped defaults."""
        return cls(_definitions())

    def with_defaults(self, defaults: Mapping[str, object]) -> SettingsRegistry:
        """Return a registry whose defaults are replaced by ``defaults`` (for keys it holds)."""
        return SettingsRegistry(
            {
                key: replace(definition, default=defaults[key]) if key in defaults else definition
                for key, definition in self.definitions.items()
            }
        )

    def definition(self, key: str) -> SettingDefinition:
        """Return the definition of ``key``.

        Raises:
            HarborValidationError: The key is unknown or boot-only (``field = key``).
        """
        if is_boot_only(key):
            raise HarborValidationError(
                f"{key} is boot-only: set it in the environment and restart", details={"field": key}
            )
        try:
            return self.definitions[key]
        except KeyError:
            raise HarborValidationError(
                f"{key} is not a known setting", details={"field": key}
            ) from None

    def keys(self) -> tuple[str, ...]:
        """Return every key, sorted."""
        return tuple(sorted(self.definitions))

    def json_schema(self) -> dict[str, Any]:
        """Return the settings grouped for the explorer: ``{"groups": [{name, title, settings}]}``."""
        return {
            "groups": [
                {
                    "name": group,
                    "title": GROUP_TITLES[group],
                    "settings": [
                        _setting_entry(definition)
                        for key, definition in self.definitions.items()
                        if key.startswith(f"{group}.")
                    ],
                }
                for group in SETTINGS_GROUPS
            ]
        }


def _setting_entry(definition: SettingDefinition) -> dict[str, Any]:
    schema = definition.schema
    kind: object
    if "enum" in schema:
        kind = "enum"
    elif "pattern" in schema:
        kind = "model_ref"
    else:
        kind = schema.get("type", "string")
    entry: dict[str, Any] = {
        "key": definition.key,
        "type": kind,
        "default": definition.default,
        "levels": list(definition.levels),
        "applies": definition.applies,
        "description": definition.description,
    }
    if "minimum" in schema:
        entry["min"] = schema["minimum"]
    if "maximum" in schema:
        entry["max"] = schema["maximum"]
    if "enum" in schema:
        entry["enum"] = list(schema["enum"])  # type: ignore[call-overload]  # enum is a list in the fragment
    return entry
