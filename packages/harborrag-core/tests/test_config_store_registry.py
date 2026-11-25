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

CAPTURE_FILE_TYPES = [
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
]

FOURTEEN_KEYS = (
    "capture.file_types",
    "capture.max_document_mb",
    "capture.max_pdf_pages",
    "chunking.max_tokens",
    "ingestion.activity_timeout",
    "ingestion.batch_size",
    "ingestion.concurrency",
    "ingestion.max_attempts",
    "models.embedding",
    "parsing.ocr.enabled",
    "parsing.ocr.languages",
    "parsing.pdf_parser",
    "retrieval.rerank",
    "retrieval.top_k",
)


def test_registry_holds_exactly_the_fourteen_keys() -> None:
    assert SettingsRegistry.default().keys() == FOURTEEN_KEYS


def test_defaults_match_todays_knobs() -> None:
    registry = SettingsRegistry.default()

    defaults = [registry.definition(key).default for key in registry.keys()]

    assert defaults == [
        CAPTURE_FILE_TYPES,
        20,
        200,
        1200,
        "PT10M",
        50,
        8,
        3,
        None,
        True,
        ["en"],
        "liteparse",
        False,
        5,
    ]


def test_levels_and_applies() -> None:
    registry = SettingsRegistry.default()

    assert registry.definition("ingestion.concurrency").levels == ("tenant", "connection")
    assert registry.definition("models.embedding").applies == "reindex"


def test_json_schema_groups_hold_every_key_once() -> None:
    groups = SettingsRegistry.default().json_schema()["groups"]

    assert [(group["name"], group["title"]) for group in groups] == [
        ("retrieval", "Retrieval"),
        ("capture", "Capture"),
        ("parsing", "Parsing"),
        ("chunking", "Chunking"),
        ("ingestion", "Ingestion"),
        ("models", "Models"),
    ]
    entries = {entry["key"]: entry for group in groups for entry in group["settings"]}
    assert sorted(entry["key"] for group in groups for entry in group["settings"]) == sorted(
        FOURTEEN_KEYS
    )
    assert entries["capture.max_document_mb"] == {
        "key": "capture.max_document_mb",
        "type": "integer",
        "default": 20,
        "min": 1,
        "max": 100,
        "levels": ["tenant", "project", "connection"],
        "applies": "next_ingestion",
        "description": entries["capture.max_document_mb"]["description"],
    }
    assert entries["capture.file_types"]["type"] == "array"
    assert entries["capture.file_types"]["default"] == CAPTURE_FILE_TYPES
    assert "min" not in entries["capture.file_types"]
    assert entries["capture.max_pdf_pages"] == {
        "key": "capture.max_pdf_pages",
        "type": "integer",
        "default": 200,
        "min": 1,
        "max": 2000,
        "levels": ["tenant", "project", "connection"],
        "applies": "next_ingestion",
        "description": entries["capture.max_pdf_pages"]["description"],
    }
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
    assert entries["parsing.ocr.enabled"] == {
        "key": "parsing.ocr.enabled",
        "type": "boolean",
        "default": True,
        "levels": ["tenant", "project", "connection"],
        "applies": "next_ingestion",
        "description": entries["parsing.ocr.enabled"]["description"],
    }
    assert entries["parsing.ocr.languages"]["type"] == "array"
    assert entries["parsing.ocr.languages"]["default"] == ["en"]
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
    assert SettingsLevel.parse("project:demo") == SettingsLevel("project", project_id="demo")
    parsed = SettingsLevel.parse("connection:demo/demo-tickets")
    assert parsed == SettingsLevel("connection", project_id="demo", connection_id="demo-tickets")
    assert str(parsed) == "connection:demo/demo-tickets"
    assert str(SettingsLevel.parse("project:demo")) == "project:demo"
    assert str(SettingsLevel("tenant")) == "tenant"


@pytest.mark.parametrize(
    "text", ["folder:x", "connection:demo", "connection:demo/Bad!", "project:", ""]
)
def test_level_parse_rejects_malformed(text: str) -> None:
    with pytest.raises(HarborValidationError) as raised:
        SettingsLevel.parse(text)

    assert raised.value.details == {"field": "level"}


def test_level_chain_runs_from_tenant_to_self() -> None:
    level = SettingsLevel.parse("connection:demo/demo-tickets")

    assert [str(item) for item in level.chain()] == [
        "tenant",
        "project:demo",
        "connection:demo/demo-tickets",
    ]
    assert SettingsLevel("tenant").chain() == (SettingsLevel("tenant"),)
