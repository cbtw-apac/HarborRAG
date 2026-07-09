"""Gray-box integration tests for the adapters <-> harborrag-core contract.

These exercise the seams where adapter code depends on core domain types:
``ParseInput.coerce`` shape handling, the RawDocument -> ParseInput -> parser
round trip, the AdapterBuilder connector-registry fallback, and the connector
schema helpers.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from harborrag_adapters.builder import AdapterBuilder
from harborrag_adapters.connectors import (
    ConfluenceConnector,
    ConfluenceSpaceConfig,
    GitHubConnector,
    GitHubRepositoryConfig,
)
from harborrag_adapters.connectors.exceptions import ConnectorNotFoundError
from harborrag_adapters.connectors.mock import MockConnector
from harborrag_adapters.connectors.schemas import (
    ConnectorCapabilities,
    ConnectorQuery,
    ConnectorSyncState,
)
from harborrag_adapters.parsers import HarborParser
from harborrag_adapters.registry import AdapterRegistry
from harborrag_core.domain.element import DocumentElement
from harborrag_core.domain.parser import ParsedDocument, ParseInput
from harborrag_core.domain.raw_document import RawDocument


# ---------------------------------------------------------------------------
# RawDocument -> ParseInput.coerce -> HarborParser round trip
# ---------------------------------------------------------------------------

def test_raw_document_parses_through_coerce():
    raw = RawDocument(
        id="mock://document/0",
        source="docs/guide.md",
        content="# Guide\n\nBody paragraph.",
        content_type="text/markdown",
        metadata={"title": "Guide"},
    )

    parse_input = ParseInput.coerce(raw)
    # Filename is derived from the source locator so suffix routing works.
    assert parse_input.filename == "guide.md"
    assert parse_input.content_type == "text/markdown"
    assert parse_input.content == raw.content

    document = HarborParser().parse(parse_input)

    # ParsedDocument must conform to the core schema.
    assert isinstance(document, ParsedDocument)
    assert isinstance(document.content, str) and document.content
    assert document.parser_name == "markdown"
    assert isinstance(document.parser_name, str)
    assert isinstance(document.elements, list)
    assert all(isinstance(el, DocumentElement) for el in document.elements)


def test_raw_document_filename_falls_back_to_source_id():
    raw = RawDocument(
        id="anything",
        source="",  # empty source -> coerce falls through to source_id
        content="plain body",
        content_type="text/plain",
    )
    # RawDocument has no source_id attribute, so filename stays None but
    # content_type still drives routing.
    parse_input = ParseInput.coerce(raw)
    assert parse_input.filename is None
    assert parse_input.content_type == "text/plain"

    document = HarborParser().parse(parse_input)
    assert document.parser_name == "text"


# ---------------------------------------------------------------------------
# ParseInput.coerce input-shape contract
# ---------------------------------------------------------------------------

def test_coerce_bytes_becomes_content():
    result = ParseInput.coerce(b"raw bytes")
    assert result.content == b"raw bytes"
    assert result.path is None


def test_coerce_path_becomes_path(tmp_path: Path):
    p = tmp_path / "file.txt"
    result = ParseInput.coerce(p)
    assert result.path == p
    assert result.content is None
    # filename is derived from the path.
    assert result.filename == "file.txt"


def test_coerce_str_becomes_content_not_path(tmp_path: Path):
    # A string that happens to name a real file must still be treated as
    # content, never auto-promoted to a filesystem read.
    real = tmp_path / "real.txt"
    real.write_text("on disk", encoding="utf-8")

    result = ParseInput.coerce(str(real))
    assert result.content == str(real)
    assert result.path is None


def test_coerce_object_with_text_metadata_filename():
    class DocLike:
        filename = "thing.md"
        metadata = {"k": "v"}
        content_type = "text/markdown"

        def text(self) -> str:
            return "# from text()"

    result = ParseInput.coerce(DocLike())
    assert result.content == "# from text()"
    assert result.filename == "thing.md"
    assert result.metadata == {"k": "v"}
    assert result.content_type == "text/markdown"


def test_coerce_is_idempotent():
    original = ParseInput(content="x", filename="a.txt")
    assert ParseInput.coerce(original) is original


# ---------------------------------------------------------------------------
# AdapterBuilder connector-registry fallback
# ---------------------------------------------------------------------------

def test_builder_resolves_explicitly_registered_connector():
    registry = AdapterRegistry()
    registry.register_connector("mock", MockConnector)
    builder = AdapterBuilder(registry)

    connector = builder.build_connector("mock")
    assert isinstance(connector, MockConnector)


def test_builder_falls_back_to_connector_registry_confluence():
    builder = AdapterBuilder(AdapterRegistry())

    connector = builder.build_connector(
        "confluence",
        config=ConfluenceSpaceConfig(
            space_key="ENG",
            base_url="https://example.atlassian.net/wiki",
            token="token",
            email="me@example.com",
            requests_per_minute=6000,
        ),
    )
    assert isinstance(connector, ConfluenceConnector)


def test_builder_falls_back_to_connector_registry_github():
    builder = AdapterBuilder(AdapterRegistry())

    connector = builder.build_connector(
        "github",
        config=GitHubRepositoryConfig(
            repository_url="https://github.com/acme/harbor-rag.git",
            requests_per_minute=6000,
        ),
    )
    assert isinstance(connector, GitHubConnector)


def test_builder_unknown_connector_raises():
    builder = AdapterBuilder(AdapterRegistry())
    with pytest.raises(ConnectorNotFoundError):
        builder.build_connector("does-not-exist")


# ---------------------------------------------------------------------------
# Connector schema behaviour
# ---------------------------------------------------------------------------

def test_connector_capabilities_defaults():
    caps = ConnectorCapabilities()
    assert caps.sync is True
    assert caps.incremental_sync is False
    # frozen dataclass -> immutable feature flags.
    with pytest.raises(Exception):
        caps.sync = False  # type: ignore[misc]


def test_connector_query_defaults():
    query = ConnectorQuery()
    assert query.recursive is True
    assert query.limit is None
    assert query.filters == {}


def test_connector_sync_state_update_checksum():
    state = ConnectorSyncState(connector_id="c1", source_system="local")
    state.update_checksum("doc-1", "abc123")
    state.update_checksum("doc-2", "def456")
    state.update_checksum("doc-1", "updated")

    assert state.checksum_map == {"doc-1": "updated", "doc-2": "def456"}
