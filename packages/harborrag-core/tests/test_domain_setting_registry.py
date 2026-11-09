from __future__ import annotations

import pytest

from harborrag_core.domain.setting_registry import (
    SETTING_REGISTRY,
    ScopeLevel,
    get_setting_spec,
    is_allowed_at_level,
    settings_schema_groups,
)


def test_registry_has_fourteen_keys() -> None:
    assert len(SETTING_REGISTRY) == 14


def test_get_setting_spec_rejects_unknown_key() -> None:
    with pytest.raises(KeyError):
        get_setting_spec("not.a.real.key")


@pytest.mark.parametrize(
    ("key", "level", "expected"),
    [
        ("chunking.max_tokens", "connection", True),
        ("models.embedding", "connection", False),
        ("models.embedding", "project", True),
        ("ingestion.concurrency", "project", False),
        ("ingestion.concurrency", "tenant", True),
    ],
)
def test_is_allowed_at_level(key: str, level: ScopeLevel, expected: bool) -> None:
    assert is_allowed_at_level(key, level) == expected


def test_chunking_max_tokens_rejects_out_of_range() -> None:
    spec = get_setting_spec("chunking.max_tokens")
    spec.validate_value(512)
    with pytest.raises(ValueError, match="must be <="):
        spec.validate_value(5000)
    with pytest.raises(ValueError, match="must be >="):
        spec.validate_value(10)


def test_parsing_pdf_parser_rejects_unknown_engine() -> None:
    spec = get_setting_spec("parsing.pdf_parser")
    spec.validate_value("liteparse")
    with pytest.raises(ValueError, match="must be one of"):
        spec.validate_value("mineru")


def test_capture_file_types_requires_string_array() -> None:
    spec = get_setting_spec("capture.file_types")
    spec.validate_value([".pdf", ".md"])
    with pytest.raises(ValueError, match="must be an array"):
        spec.validate_value(".pdf")
    with pytest.raises(ValueError, match="items must be strings"):
        spec.validate_value([1, 2])


def test_models_embedding_requires_provider_colon_model() -> None:
    spec = get_setting_spec("models.embedding")
    spec.validate_value("openai:text-embedding-3-large")
    with pytest.raises(ValueError, match="provider:model"):
        spec.validate_value("text-embedding-3-large")


def test_settings_schema_groups_preserve_catalog_order() -> None:
    groups = settings_schema_groups()
    assert [group.name for group in groups] == [
        "capture",
        "parsing",
        "chunking",
        "embedding",
        "ingestion",
    ]
    capture = groups[0]
    assert capture.title == "Capture"
    assert [spec.key for spec in capture.settings] == [
        "capture.max_document_mb",
        "capture.file_types",
        "capture.max_pdf_pages",
    ]


def test_boolean_rejects_int_even_though_bool_is_an_int_subclass() -> None:
    spec = get_setting_spec("parsing.ocr.enabled")
    spec.validate_value(True)
    with pytest.raises(ValueError, match="must be a boolean"):
        spec.validate_value(1)


def test_integer_rejects_bool_even_though_bool_is_an_int_subclass() -> None:
    spec = get_setting_spec("chunking.max_tokens")
    with pytest.raises(ValueError, match="must be an integer"):
        spec.validate_value(True)
