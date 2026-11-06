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

SIX_KEYS = (
    "chunking.max_tokens",
    "ingestion.concurrency",
    "models.embedding",
    "parsing.pdf_parser",
    "retrieval.rerank",
    "retrieval.top_k",
)


def test_registry_holds_exactly_the_six_keys() -> None:
    assert SettingsRegistry.default().keys() == SIX_KEYS


def test_defaults_match_todays_knobs() -> None:
    registry = SettingsRegistry.default()

    defaults = [registry.definition(key).default for key in registry.keys()]

    assert defaults == [1200, 8, None, "liteparse", False, 5]


def test_levels_and_applies() -> None:
    registry = SettingsRegistry.default()

    assert registry.definition("ingestion.concurrency").levels == ("tenant", "connection")
    assert registry.definition("models.embedding").applies == "reindex"


def test_json_schema_entry() -> None:
    schema = SettingsRegistry.default().json_schema()

    assert set(schema) == set(SIX_KEYS)
    assert schema["chunking.max_tokens"] == {
        "type": "integer",
        "minimum": 64,
        "maximum": 4096,
        "default": 1200,
        "levels": ["tenant", "project", "connection"],
        "applies": "next_ingestion",
    }
    assert schema["parsing.pdf_parser"]["enum"] == ["liteparse", "docling"]
    assert schema["models.embedding"]["type"] == "string"
    assert "pattern" in schema["models.embedding"]


def test_with_defaults_sets_embedding() -> None:
    registry = SettingsRegistry.default().with_defaults({"models.embedding": "openai:te3"})

    assert registry.definition("models.embedding").default == "openai:te3"
    assert registry.json_schema()["models.embedding"]["default"] == "openai:te3"
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
