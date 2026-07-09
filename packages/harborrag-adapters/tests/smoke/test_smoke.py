"""Smoke tests: every parser/connector module imports and initializes.

These are the cheapest possible guard against a broken import graph (the class
of bug that took down `connectors.mock` and blocked the whole package).
"""
from __future__ import annotations

import importlib

import pytest

PARSER_MODULES = [
    "harborrag_adapters.parsers",
    "harborrag_adapters.parsers.base",
    "harborrag_adapters.parsers.engine",
    "harborrag_adapters.parsers.text",
    "harborrag_adapters.parsers.markdown",
    "harborrag_adapters.parsers.html_engine",
    "harborrag_adapters.parsers.structured",
    "harborrag_adapters.parsers.office",
    "harborrag_adapters.parsers.image",
    "harborrag_adapters.parsers.ebook",
    "harborrag_adapters.parsers.utils",
    "harborrag_adapters.parsers.exceptions",
    "harborrag_adapters.parsers.pdf_engine",
    "harborrag_adapters.parsers.pdf_engine.parser",
    "harborrag_adapters.parsers.pdf_engine.pymupdf",
    "harborrag_adapters.parsers.pdf_engine.docling",
    "harborrag_adapters.parsers.pdf_engine.mineru",
    "harborrag_adapters.parsers.pdf_engine.paddleocr",
    "harborrag_adapters.parsers.pdf_engine.liteparse",
    "harborrag_adapters.parsers.pdf_engine.utils",
]

CONNECTOR_MODULES = [
    "harborrag_adapters.connectors",
    "harborrag_adapters.connectors.base",
    "harborrag_adapters.connectors.registry",
    "harborrag_adapters.connectors.schemas",
    "harborrag_adapters.connectors.exceptions",
    "harborrag_adapters.connectors.http_utils",
    "harborrag_adapters.connectors.attachments",
    "harborrag_adapters.connectors.mock",
    "harborrag_adapters.connectors.confluence.connector",
    "harborrag_adapters.connectors.jira.connector",
    "harborrag_adapters.connectors.github.connector",
    "harborrag_adapters.connectors.sharepoint.connector",
    "harborrag_adapters.connectors.local.connector",
]


@pytest.mark.parametrize("module", PARSER_MODULES + CONNECTOR_MODULES)
def test_module_imports(module: str) -> None:
    assert importlib.import_module(module) is not None


def test_harborparser_initializes_with_default_stack() -> None:
    from harborrag_adapters.parsers import HarborParser

    parser = HarborParser()
    assert parser.parsers, "default parser stack should be non-empty"


def test_connector_registry_has_all_builtin_providers() -> None:
    from harborrag_adapters.connectors import connector_registry

    for provider in ("confluence", "jira", "github", "sharepoint", "local"):
        assert connector_registry.get_class(provider) is not None


def test_mock_connector_round_trip() -> None:
    from harborrag_adapters.connectors.mock import MockConnector

    connector = MockConnector(text="# Hi\n\nBody")
    record = next(connector.discover())
    document = connector.load(record)
    assert document.metadata["title"] == "Mock Document"
    assert document.text().startswith("# Hi")
