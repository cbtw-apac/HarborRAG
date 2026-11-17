from __future__ import annotations

from pathlib import Path

import pytest

from harborrag_core.config_store.documents import SettingsDocument
from harborrag_runtime.config.errors import ConfigurationError
from harborrag_runtime.config.settings_document import (
    SettingsDocumentError,
    dump_settings_document,
    load_settings_document,
)

pytestmark = pytest.mark.unit


def _load(tmp_path: Path, text: str) -> SettingsDocument:
    path = tmp_path / "settings.yaml"
    path.write_text(text, encoding="utf-8")
    return load_settings_document(path)


def test_load_dotted_and_nested_keys(tmp_path: Path) -> None:
    dotted = _load(tmp_path, "chunking.max_tokens: 512\n")
    nested = _load(tmp_path, "chunking:\n  max_tokens: 512\n")

    assert dotted == nested
    assert dotted.levels == {"tenant": {"chunking.max_tokens": 512}}


def test_projects_and_connections_sections(tmp_path: Path) -> None:
    document = _load(
        tmp_path,
        "version: 1\nretrieval.top_k: 7\n"
        "projects:\n  cpm:\n    chunking: {max_tokens: 384}\n"
        "connections:\n  cpm/cpm-tickets:\n    ingestion.concurrency: 2\n",
    )

    assert document.levels == {
        "tenant": {"retrieval.top_k": 7},
        "project:cpm": {"chunking.max_tokens": 384},
        "connection:cpm/cpm-tickets": {"ingestion.concurrency": 2},
    }


@pytest.mark.parametrize(
    ("text", "key"),
    [
        ("connections:\n  cpm:\n    chunking.max_tokens: 1\n", "cpm"),
        ("version: 2\n", "version"),
        ("projects: [a]\n", "projects"),
        ("projects:\n  cpm: 3\n", "cpm"),
        ("chunking.max_tokens: 1\nchunking:\n  max_tokens: 2\n", "chunking.max_tokens"),
        ("- a\n", None),
    ],
)
def test_structure_errors_name_path(tmp_path: Path, text: str, key: str | None) -> None:
    with pytest.raises(SettingsDocumentError) as error:
        _load(tmp_path, text)

    assert str(tmp_path / "settings.yaml") in str(error.value)
    assert error.value.key == key
    if key is not None:
        assert key in str(error.value)


def test_duplicate_key_rejected(tmp_path: Path) -> None:
    with pytest.raises(ConfigurationError, match="duplicate key"):
        _load(tmp_path, "retrieval.top_k: 1\nretrieval.top_k: 2\n")


def test_dump_load_round_trip(tmp_path: Path) -> None:
    document = SettingsDocument(
        {
            "connection:cpm/cpm-tickets": {"ingestion.concurrency": 2},
            "tenant": {"retrieval.top_k": 7, "chunking.max_tokens": 512},
            "project:cpm": {"chunking.max_tokens": 384},
        }
    )

    text = dump_settings_document(document)

    assert text.startswith("chunking.max_tokens: 512\nretrieval.top_k: 7\n")
    assert _load(tmp_path, text) == document
