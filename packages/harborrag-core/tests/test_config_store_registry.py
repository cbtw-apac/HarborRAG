from __future__ import annotations

import pytest

from harborrag_core.config_store.levels import SettingsLevel
from harborrag_core.config_store.registry import (
    BOOT_ONLY_PREFIXES,
    SettingsRegistry,
    is_boot_only,
)
from harborrag_core.contracts.errors import HarborValidationError

pytestmark = pytest.mark.unit

EIGHT_KEYS = (
    "chunking.max_tokens",
    "ingestion.activity_timeout",
    "ingestion.concurrency",
    "ingestion.max_attempts",
    "models.embedding",
    "parsing.pdf_parser",
    "retrieval.rerank",
    "retrieval.top_k",
)


def test_registry_holds_exactly_the_eight_keys() -> None:
    assert SettingsRegistry.default().keys() == EIGHT_KEYS


def test_defaults_match_todays_knobs() -> None:
    registry = SettingsRegistry.default()

    defaults = [registry.definition(key).default for key in registry.keys()]

    assert defaults == [1200, "PT10M", 8, 3, None, "liteparse", False, 5]


def test_levels_and_applies() -> None:
    registry = SettingsRegistry.default()

    assert registry.definition("ingestion.concurrency").levels == ("tenant", "connection")
    assert registry.definition("models.embedding").applies == "reindex"


def test_json_schema_groups_hold_every_key_once() -> None:
    groups = SettingsRegistry.default().json_schema()["groups"]

    assert [(group["name"], group["title"]) for group in groups] == [
        ("retrieval", "Retrieval"),
        ("parsing", "Parsing"),
        ("chunking", "Chunking"),
        ("ingestion", "Ingestion"),
        ("models", "Models"),
    ]
    entries = {entry["key"]: entry for group in groups for entry in group["settings"]}
    assert sorted(entry["key"] for group in groups for entry in group["settings"]) == sorted(
        EIGHT_KEYS
    )
    assert entries["chunking.max_tokens"] == {
        "key": "chunking.max_tokens",
        "type": "integer",
        "default": 1200,
        "min": 64,
        "max": 4096,
        "levels": ["tenant", "project", "connection"],
        "applies": "next_ingestion",
        "description": entries["chunking.max_tokens"]["description"],
    }
    assert entries["parsing.pdf_parser"]["type"] == "enum"
    assert entries["parsing.pdf_parser"]["enum"] == ["liteparse", "docling"]
    assert entries["retrieval.rerank"]["type"] == "boolean"
    assert "min" not in entries["retrieval.rerank"]
    assert "max" not in entries["retrieval.rerank"]
    assert "min" not in entries["parsing.pdf_parser"]
    assert entries["models.embedding"]["type"] == "model_ref"
    assert all(entry["description"] for entry in entries.values())


def _entry(registry: SettingsRegistry, key: str) -> dict[str, object]:
    groups = registry.json_schema()["groups"]
    return next(entry for group in groups for entry in group["settings"] if entry["key"] == key)


def test_with_defaults_sets_embedding() -> None:
    registry = SettingsRegistry.default().with_defaults({"models.embedding": "openai:te3"})

    assert registry.definition("models.embedding").default == "openai:te3"
    assert _entry(registry, "models.embedding")["default"] == "openai:te3"
    assert SettingsRegistry.default().definition("models.embedding").default is None


@pytest.mark.parametrize("prefix", BOOT_ONLY_PREFIXES)
def test_boot_only_prefixes_are_refused(prefix: str) -> None:
    key = f"{prefix}url"

    assert is_boot_only(key)
    with pytest.raises(HarborValidationError, match="boot-only") as raised:
        SettingsRegistry.default().definition(key)
    assert raised.value.details == {"field": key}


def test_unknown_key_is_refused() -> None:
    with pytest.raises(HarborValidationError) as raised:
        SettingsRegistry.default().definition("chunking.nope")

    assert raised.value.details == {"field": "chunking.nope"}


@pytest.mark.parametrize(
    ("key", "value"),
    [
        ("chunking.max_tokens", True),
        ("chunking.max_tokens", 63),
        ("retrieval.top_k", "5"),
        ("parsing.pdf_parser", "tesseract"),
        ("models.embedding", "no-colon"),
    ],
)
def test_validate_rejects_bad_values(key: str, value: object) -> None:
    with pytest.raises(HarborValidationError) as raised:
        SettingsRegistry.default().definition(key).validate(value)

    assert raised.value.details == {"field": key}


def test_validate_returns_the_value() -> None:
    assert SettingsRegistry.default().definition("chunking.max_tokens").validate(300) == 300


def test_level_parse() -> None:
    assert SettingsLevel.parse(None) == SettingsLevel("tenant")
    assert SettingsLevel.parse("tenant") == SettingsLevel("tenant")
    assert SettingsLevel.parse("project:cpm") == SettingsLevel("project", project_id="cpm")
    parsed = SettingsLevel.parse("connection:cpm/cpm-tickets")
    assert parsed == SettingsLevel("connection", project_id="cpm", connection_id="cpm-tickets")
    assert str(parsed) == "connection:cpm/cpm-tickets"
    assert str(SettingsLevel.parse("project:cpm")) == "project:cpm"
    assert str(SettingsLevel("tenant")) == "tenant"


@pytest.mark.parametrize(
    "text", ["folder:x", "connection:cpm", "connection:cpm/Bad!", "project:", ""]
)
def test_level_parse_rejects_malformed(text: str) -> None:
    with pytest.raises(HarborValidationError) as raised:
        SettingsLevel.parse(text)

    assert raised.value.details == {"field": "level"}


def test_level_chain_runs_from_tenant_to_self() -> None:
    level = SettingsLevel.parse("connection:cpm/cpm-tickets")

    assert [str(item) for item in level.chain()] == [
        "tenant",
        "project:cpm",
        "connection:cpm/cpm-tickets",
    ]
    assert SettingsLevel("tenant").chain() == (SettingsLevel("tenant"),)
