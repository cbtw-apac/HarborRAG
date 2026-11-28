"""The runtime settings registry: its keys, their groups, levels and defaults.

Pure; no I/O. Group models are the single source of each key's type, range,
default, levels and ``applies``. Boot-only keys never enter the registry.

Entry point: ``SettingsRegistry.default()``.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field, replace
from typing import Any, Literal

from pydantic import Field, ValidationError, field_validator

from harborrag_core.base import StrictModel
from harborrag_core.config_store.durations import parse_iso8601_duration_seconds
from harborrag_core.config_store.levels import LevelKind
from harborrag_core.contracts.errors import HarborValidationError

MIN_ACTIVITY_TIMEOUT_SECONDS = 30
MAX_ACTIVITY_TIMEOUT_SECONDS = 3_600

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
    "capture": "Capture",
    "parsing": "Parsing",
    "chunking": "Chunking",
    "embedding": "Embedding",
    "ingestion": "Ingestion",
    "models": "Models",
}

_SCHEMA_KEYS = ("type", "minimum", "maximum", "enum", "pattern")

_CAPTURE_FILE_TYPES: tuple[str, ...] = (
    ".md",
    ".markdown",
    ".txt",
    ".pdf",
    ".docx",
    ".doc",
    ".epub",
    ".odt",
    ".pptx",
    ".csv",
    ".xlsx",
    ".png",
    ".jpg",
    ".jpeg",
    ".html",
)


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
        default=False,
        description="Rerank chat retrieval passages with the model catalog's rerank model",
        json_schema_extra=_extra(["tenant", "project"], "live"),
    )


class CaptureGroup(StrictModel):
    """Capture settings."""

    max_document_mb: int = Field(
        20,
        ge=1,
        le=100,
        description="Largest document size in megabytes a capture will fetch",
        json_schema_extra=_extra(["tenant", "project", "connection"], "next_ingestion"),
    )
    file_types: list[str] = Field(
        list(_CAPTURE_FILE_TYPES),
        min_length=1,
        description="File extensions a capture will fetch, from the shared capture types",
        json_schema_extra=_extra(["tenant", "project", "connection"], "next_ingestion"),
    )
    max_pdf_pages: int = Field(
        200,
        ge=1,
        le=2000,
        description="Largest page count a PDF may have before it is rejected",
        json_schema_extra=_extra(["tenant", "project", "connection"], "next_ingestion"),
    )

    @field_validator("file_types")
    @classmethod
    def _shared_file_types(cls, value: list[str]) -> list[str]:
        """Refuse an extension outside the shared types, with or without its dot."""
        unknown = sorted({_dotted_extension(item) for item in value} - set(_CAPTURE_FILE_TYPES))
        if unknown:
            raise ValueError(f"unsupported file types: {', '.join(unknown)}")
        return value


def _dotted_extension(value: str) -> str:
    """Return ``value`` lowercased with exactly one leading dot, so ``PDF`` becomes ``.pdf``."""
    stripped = value.strip().lower()
    return stripped if stripped.startswith(".") else f".{stripped}"


class OcrSettings(StrictModel):
    """OCR settings nested under ``parsing.ocr``."""

    enabled: bool = Field(
        default=True,
        description="Run OCR on scanned PDFs and image documents",
        json_schema_extra=_extra(["tenant", "project", "connection"], "next_ingestion"),
    )
    languages: list[str] = Field(
        ["en"],
        description="Languages the OCR engine recognizes",
        json_schema_extra=_extra(["tenant", "project", "connection"], "next_ingestion"),
    )


class ParsingGroup(StrictModel):
    """Parsing settings."""

    pdf_parser: Literal["liteparse", "docling"] = Field(
        "liteparse",
        description="Engine that parses PDF documents",
        json_schema_extra=_extra(["tenant", "project", "connection"], "next_ingestion"),
    )
    ocr: OcrSettings = Field(default_factory=lambda: OcrSettings(enabled=True, languages=["en"]))


class ChunkingGroup(StrictModel):
    """Chunking settings."""

    max_tokens: int = Field(
        1200,
        ge=64,
        le=4096,
        description="Largest chunk size in tokens; caps every chunking profile",
        json_schema_extra=_extra(["tenant", "project", "connection"], "next_ingestion"),
    )
    overlap_tokens: int | None = Field(
        None,
        ge=0,
        le=512,
        description=(
            "Tokens repeated between consecutive chunks, clamped below a profile's target; "
            "unset keeps each profile's own overlap"
        ),
        json_schema_extra=_extra(["tenant", "project", "connection"], "next_ingestion"),
    )


class EmbeddingGroup(StrictModel):
    """Embedding settings."""

    batch_size: int = Field(
        32,
        ge=1,
        le=256,
        description="Chunks embedded per encoding API call",
        json_schema_extra=_extra(["tenant", "project"], "next_ingestion"),
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
    activity_timeout: str | None = Field(
        None,
        description=(
            "ISO 8601 duration from PT30S to PT1H bounding one attempt of each document "
            "activity; unset keeps each activity's own budget"
        ),
        json_schema_extra=_extra(["tenant", "connection"], "next_ingestion"),
    )
    max_attempts: int | None = Field(
        None,
        ge=1,
        le=10,
        description=(
            "Attempts per document activity, the first one included; unset keeps the "
            "document retry policy in config/temporal.yaml"
        ),
        json_schema_extra=_extra(["tenant", "connection"], "next_ingestion"),
    )
    batch_size: int = Field(
        200,
        ge=1,
        le=500,
        description="Documents grouped into one batch workflow",
        json_schema_extra=_extra(["tenant", "connection"], "next_ingestion"),
    )

    @field_validator("activity_timeout")
    @classmethod
    def _validate_activity_timeout(cls, value: str | None) -> str | None:
        """Refuse a duration that is malformed or outside PT30S to PT1H."""
        if value is None:
            return None
        seconds = parse_iso8601_duration_seconds(value)
        if not MIN_ACTIVITY_TIMEOUT_SECONDS <= seconds <= MAX_ACTIVITY_TIMEOUT_SECONDS:
            raise ValueError(
                "activity_timeout must be between PT30S and PT1H "
                f"({MIN_ACTIVITY_TIMEOUT_SECONDS}-{MAX_ACTIVITY_TIMEOUT_SECONDS} seconds)"
            )
        return value


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
    "capture": CaptureGroup,
    "parsing": ParsingGroup,
    "chunking": ChunkingGroup,
    "embedding": EmbeddingGroup,
    "ingestion": IngestionGroup,
    "models": ModelsGroup,
}


@dataclass(frozen=True, slots=True)
class SettingDefinition:
    """One registered setting.

    Attributes:
        key: Dotted key, ``<group>.<field>`` or ``<group>.<nested>.<field>``.
        levels: Levels at which an override may exist.
        applies: When a change takes effect.
        default: Value used when no level overrides the key.
        owner: The model the leaf field belongs to: the group itself, or a
            nested settings model one level under it.
        leaf_field: The field name on ``owner`` that holds the value.
        schema: JSON-schema fragment of the value: type, bounds, enum or pattern.
        description: One-line summary of what the key controls.
    """

    key: str
    levels: tuple[LevelKind, ...]
    applies: Applies
    default: object
    owner: type[StrictModel] = field(compare=False)
    leaf_field: str = field(compare=False)
    schema: Mapping[str, object] = field(compare=False)
    description: str = field(default="", compare=False)

    def validate(self, value: object) -> object:
        """Validate ``value`` strictly against the owning model, so ``True`` is not an int.

        Args:
            value: Candidate override value.

        Returns:
            The validated value.

        Raises:
            HarborValidationError: The value is of the wrong type or out of range
                (``field = key``).
        """
        try:
            model = self.owner.model_validate({self.leaf_field: value}, strict=True)
        except ValidationError as error:
            reason = "; ".join(str(item["msg"]) for item in error.errors())
            raise HarborValidationError(
                f"{self.key} is invalid: {reason}", details={"field": self.key}
            ) from error
        return getattr(model, self.leaf_field)


def _flat_schema(raw: Mapping[str, Any]) -> dict[str, object]:
    branches = raw.get("anyOf")
    if isinstance(branches, list):
        raw = next(branch for branch in branches if branch.get("type") != "null")
    return {name: raw[name] for name in _SCHEMA_KEYS if name in raw}


def _definitions() -> dict[str, SettingDefinition]:
    definitions: dict[str, SettingDefinition] = {}
    for group_name, group in SETTINGS_GROUPS.items():
        _collect(definitions, group_name, group)
    return definitions


def _collect(
    definitions: dict[str, SettingDefinition], prefix: str, owner: type[StrictModel]
) -> None:
    """Add every leaf field of ``owner`` to ``definitions``, recursing into nested models."""
    properties = owner.model_json_schema()["properties"]
    for field_name, info in owner.model_fields.items():
        annotation = info.annotation
        if isinstance(annotation, type) and issubclass(annotation, StrictModel):
            _collect(definitions, f"{prefix}.{field_name}", annotation)
            continue
        extra = info.json_schema_extra
        assert isinstance(extra, dict)
        key = f"{prefix}.{field_name}"
        levels: tuple[LevelKind, ...] = tuple(extra["levels"])  # type: ignore[arg-type]  # extra is a plain dict of lists
        definitions[key] = SettingDefinition(
            key=key,
            levels=levels,
            applies=extra["applies"],  # type: ignore[arg-type]  # extra values are Applies literals
            default=info.default,
            owner=owner,
            leaf_field=field_name,
            schema=_flat_schema(properties[field_name]),
            description=info.description or "",
        )


@dataclass(frozen=True, slots=True)
class SettingsRegistry:
    """The set of settings keys the service accepts.

    Attributes:
        definitions: Definitions by key.
        rerank_available: The model catalog has a rerank model, so
            ``retrieval.rerank`` may be switched on.
    """

    definitions: Mapping[str, SettingDefinition]
    rerank_available: bool = False

    @classmethod
    def default(cls) -> SettingsRegistry:
        """Build the registry with the shipped defaults."""
        return cls(_definitions())

    def with_defaults(self, defaults: Mapping[str, object]) -> SettingsRegistry:
        """Return a registry whose defaults are replaced by ``defaults`` (for keys it holds).

        ``rerank_available`` carries over unchanged.
        """
        return replace(
            self,
            definitions={
                key: replace(definition, default=defaults[key]) if key in defaults else definition
                for key, definition in self.definitions.items()
            },
        )

    def definition(self, key: str) -> SettingDefinition:
        """Return the definition of ``key``.

        Args:
            key: Dotted setting key, ``<group>.<field>``.

        Returns:
            The registered definition.

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
