"""The runtime setting catalog: every overridable key, its type, and its scope rules.

This is the single source of truth `GET /v1/admin/settings/schema` renders from and
`PATCH /v1/admin/settings` validates against, so the two can never drift from each
other or from the YAML/env defaults they are meant to replace.

Deviation from the original spec: the "type" enum there only lists
string/integer/number/boolean/enum/model_ref, which cannot express a list-valued
setting such as ``capture.file_types`` or ``parsing.ocr.languages``. This registry
adds an "array" type with an ``items_type`` describing the element type; flag this
to the API's spec owner so the published schema stays in sync with this addition.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal

ScopeLevel = Literal["tenant", "project", "connection"]
ApplyMode = Literal["live", "next_ingestion", "reindex"]
SettingType = Literal["string", "integer", "number", "boolean", "enum", "model_ref", "array"]

ALL_LEVELS: frozenset[ScopeLevel] = frozenset({"tenant", "project", "connection"})
TENANT_PROJECT_LEVELS: frozenset[ScopeLevel] = frozenset({"tenant", "project"})
TENANT_CONNECTION_LEVELS: frozenset[ScopeLevel] = frozenset({"tenant", "connection"})


@dataclass(frozen=True, slots=True)
class SettingSpec:
    """One overridable key: its type, bounds, allowed scope, and apply timing."""

    key: str
    type: SettingType
    default: Any
    levels: frozenset[ScopeLevel]
    applies: ApplyMode
    description: str
    group: str
    group_title: str
    minimum: float | None = None
    maximum: float | None = None
    enum: tuple[Any, ...] | None = None
    items_type: SettingType | None = None

    def __post_init__(self) -> None:
        if not self.key.strip():
            raise ValueError("setting key must be non-empty")
        if not self.levels:
            raise ValueError(f"{self.key}: levels must be non-empty")
        if self.levels - ALL_LEVELS:
            raise ValueError(f"{self.key}: levels must be a subset of {sorted(ALL_LEVELS)}")
        if self.type == "array" and self.items_type is None:
            raise ValueError(f"{self.key}: array type requires items_type")
        if self.type == "enum" and not self.enum:
            raise ValueError(f"{self.key}: enum type requires a non-empty enum")

    def validate_value(self, value: Any) -> None:
        """Reject a candidate override value that violates this key's declared shape.

        Raises:
            ValueError: ``value`` does not match the type, range, or enum for this key.
        """
        if self.type == "integer":
            if isinstance(value, bool) or not isinstance(value, int):
                raise ValueError(f"{self.key} must be an integer")
            self._check_range(value)
        elif self.type == "number":
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                raise ValueError(f"{self.key} must be a number")
            self._check_range(value)
        elif self.type == "boolean":
            if not isinstance(value, bool):
                raise ValueError(f"{self.key} must be a boolean")
        elif self.type == "string":
            if not isinstance(value, str):
                raise ValueError(f"{self.key} must be a string")
        elif self.type == "enum":
            if value not in (self.enum or ()):
                raise ValueError(f"{self.key} must be one of {self.enum}")
        elif self.type == "model_ref":
            if not isinstance(value, str) or ":" not in value:
                raise ValueError(f"{self.key} must be a 'provider:model' reference")
        elif self.type == "array":
            if not isinstance(value, list):
                raise ValueError(f"{self.key} must be an array")
            if not all(isinstance(item, str) for item in value):
                raise ValueError(f"{self.key} items must be strings")

    def _check_range(self, value: float) -> None:
        if self.minimum is not None and value < self.minimum:
            raise ValueError(f"{self.key} must be >= {self.minimum}")
        if self.maximum is not None and value > self.maximum:
            raise ValueError(f"{self.key} must be <= {self.maximum}")


@dataclass(frozen=True, slots=True)
class SettingGroup:
    """One stage's settings, in catalog order, as `GET /v1/admin/settings/schema` groups them."""

    name: str
    title: str
    settings: tuple[SettingSpec, ...]


def _spec(
    key: str,
    type: SettingType,
    default: Any,
    levels: frozenset[ScopeLevel],
    applies: ApplyMode,
    description: str,
    group: str,
    group_title: str,
    *,
    minimum: float | None = None,
    maximum: float | None = None,
    enum: tuple[Any, ...] | None = None,
    items_type: SettingType | None = None,
) -> SettingSpec:
    return SettingSpec(
        key=key,
        type=type,
        default=default,
        levels=levels,
        applies=applies,
        description=description,
        group=group,
        group_title=group_title,
        minimum=minimum,
        maximum=maximum,
        enum=enum,
        items_type=items_type,
    )


_RETRIEVAL = "retrieval", "Retrieval"
_PARSING = "parsing", "Parsing"
_CHUNKING = "chunking", "Chunking"
_MODELS = "models", "Models"
_INGESTION = "ingestion", "Ingestion"

_SPECS: tuple[SettingSpec, ...] = (
    _spec(
        "retrieval.top_k",
        "integer",
        5,
        TENANT_PROJECT_LEVELS,
        "live",
        "Passages the chat retrieval and the MCP vector_search return by default",
        *_RETRIEVAL,
        minimum=1,
        maximum=100,
    ),
    _spec(
        "retrieval.rerank",
        "boolean",
        False,
        TENANT_PROJECT_LEVELS,
        "live",
        "Rerank chat retrieval passages with the model catalog's rerank model",
        *_RETRIEVAL,
    ),
    _spec(
        "capture.max_document_mb",
        "integer",
        20,
        ALL_LEVELS,
        "next_ingestion",
        "Largest source document accepted, in megabytes",
        *_RETRIEVAL,
        minimum=1,
        maximum=100,
    ),
    _spec(
        "capture.file_types",
        "array",
        None,
        ALL_LEVELS,
        "next_ingestion",
        "Allowed file extensions",
        *_RETRIEVAL,
        items_type="string",
    ),
    _spec(
        "capture.max_pdf_pages",
        "integer",
        200,
        ALL_LEVELS,
        "next_ingestion",
        "Largest PDF page count accepted",
        *_RETRIEVAL,
        minimum=1,
        maximum=2000,
    ),
    _spec(
        "parsing.pdf_parser",
        "enum",
        "liteparse",
        ALL_LEVELS,
        "next_ingestion",
        "PDF parsing engine",
        *_PARSING,
        enum=("liteparse", "docling"),
    ),
    _spec(
        "parsing.ocr.enabled",
        "boolean",
        True,
        ALL_LEVELS,
        "next_ingestion",
        "Run OCR over scanned pages and images",
        *_PARSING,
    ),
    _spec(
        "parsing.ocr.languages",
        "array",
        ["en"],
        ALL_LEVELS,
        "next_ingestion",
        "OCR language codes",
        *_PARSING,
        items_type="string",
    ),
    _spec(
        # Matches config/chunking.yaml's "canonical" profile maximum_tokens; keep
        # these in sync, or resolve_effective's "default" tier lies about what a
        # fresh install actually chunks at.
        "chunking.max_tokens",
        "integer",
        1100,
        ALL_LEVELS,
        "next_ingestion",
        "Largest chunk size in tokens",
        *_CHUNKING,
        minimum=64,
        maximum=4096,
    ),
    _spec(
        # Matches config/chunking.yaml's "canonical" profile overlap_tokens.
        "chunking.overlap_tokens",
        "integer",
        0,
        ALL_LEVELS,
        "next_ingestion",
        "Token overlap between adjacent chunks; must stay below max_tokens",
        *_CHUNKING,
        minimum=0,
        maximum=512,
    ),
    _spec(
        "ingestion.concurrency",
        "integer",
        4,
        TENANT_CONNECTION_LEVELS,
        "next_ingestion",
        "Document activities run at once",
        *_INGESTION,
        minimum=1,
        maximum=64,
    ),
    _spec(
        "ingestion.batch_size",
        "integer",
        50,
        TENANT_CONNECTION_LEVELS,
        "next_ingestion",
        "Documents per batch workflow",
        *_INGESTION,
        minimum=1,
        maximum=500,
    ),
    _spec(
        "ingestion.activity_timeout",
        "string",
        "PT10M",
        TENANT_CONNECTION_LEVELS,
        "next_ingestion",
        "ISO 8601 duration bounding one activity attempt",
        *_INGESTION,
    ),
    _spec(
        "ingestion.max_attempts",
        "integer",
        3,
        TENANT_CONNECTION_LEVELS,
        "next_ingestion",
        "Retries per activity",
        *_INGESTION,
        minimum=1,
        maximum=10,
    ),
    _spec(
        "models.embedding",
        "model_ref",
        None,
        TENANT_PROJECT_LEVELS,
        "reindex",
        "Embedding model as 'provider:model'",
        *_MODELS,
    ),
    _spec(
        "embedding.batch_size",
        "integer",
        32,
        TENANT_PROJECT_LEVELS,
        "next_ingestion",
        "Chunks per embedding request",
        *_MODELS,
        minimum=1,
        maximum=256,
    ),
)

SETTING_REGISTRY: dict[str, SettingSpec] = {spec.key: spec for spec in _SPECS}


def get_setting_spec(key: str) -> SettingSpec:
    """Look up a key's spec.

    Raises:
        KeyError: ``key`` is not a registered setting.
    """
    try:
        return SETTING_REGISTRY[key]
    except KeyError:
        raise KeyError(f"unknown setting key: {key!r}") from None


def settings_schema_groups() -> tuple[SettingGroup, ...]:
    """Group the registry by stage, in catalog declaration order, for the schema endpoint."""
    order: list[tuple[str, str]] = []
    seen: set[str] = set()
    grouped: dict[str, list[SettingSpec]] = {}
    for spec in _SPECS:
        grouped.setdefault(spec.group, []).append(spec)
        if spec.group not in seen:
            seen.add(spec.group)
            order.append((spec.group, spec.group_title))
    return tuple(
        SettingGroup(name=name, title=title, settings=tuple(grouped[name])) for name, title in order
    )


def is_allowed_at_level(key: str, level: ScopeLevel) -> bool:
    """Whether ``key`` may be overridden at ``level``."""
    return level in get_setting_spec(key).levels
