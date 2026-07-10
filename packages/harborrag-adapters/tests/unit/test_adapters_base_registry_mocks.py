from __future__ import annotations

from pathlib import Path

import pytest
from harborrag_adapters.builder import AdapterBuilder
from harborrag_adapters.connectors.base import BaseConnector
from harborrag_adapters.connectors.schemas import ConnectorCapabilities, ConnectorQuery
from harborrag_adapters.parsers.base import BaseParser
from harborrag_adapters.parsers.markdown import MarkdownParser
from harborrag_adapters.registry import AdapterRegistry
from harborrag_core.domain.raw_document import RawDocument
from harborrag_core.domain.source import SourceRecord

pytestmark = pytest.mark.unit

DEFAULT_TEST_TEXT = "# Mock Document\n\nBody"


class ExampleConnector(BaseConnector):
    provider_name = "test"
    capabilities = ConnectorCapabilities(sync=True, full_sync=True)

    def __init__(self, text: str = DEFAULT_TEST_TEXT, *, count: int = 1) -> None:
        self.text = text
        self.count = count

    def discover(self, query: ConnectorQuery | None = None):
        limit = query.limit if query and query.limit is not None else self.count
        for index in range(min(self.count, limit)):
            yield SourceRecord(
                id=f"test://document/{index}",
                source_type="text/markdown",
                locator=f"test://document/{index}",
                metadata={"title": "Test Document", "index": index},
            )

    def load(self, record: SourceRecord):
        return RawDocument(
            id=record.id,
            source=record.locator,
            content=self.text,
            content_type="text/markdown",
            metadata={"title": "Test Document", **record.metadata},
        )


class ExampleLocalTextFileConnector(BaseConnector):
    provider_name = "test_local_text"
    capabilities = ConnectorCapabilities(sync=True, full_sync=True, local_files=True)

    def __init__(self, root: str | Path, *, pattern: str = "*.md") -> None:
        self.root = Path(root)
        self.pattern = pattern

    def discover(self, query: ConnectorQuery | None = None):
        pattern = query.pattern if query and query.pattern else self.pattern
        for path in sorted(self.root.rglob(pattern)):
            if path.is_file():
                yield SourceRecord(
                    id=path.resolve().as_uri(),
                    source_type="text/markdown",
                    locator=str(path.resolve()),
                    metadata={"relative_path": str(path.relative_to(self.root))},
                )

    def load(self, record: SourceRecord):
        path = Path(record.locator)
        return RawDocument(
            id=record.id,
            source=record.locator,
            content=path.read_text(encoding="utf-8"),
            content_type="text/markdown",
            metadata=dict(record.metadata),
        )


class BrokenConnector(BaseConnector):
    provider_name = "broken"

    def discover(self):
        return super().discover()

    def load(self, record):
        return super().load(record)


class BrokenParser(BaseParser):
    parser_name = "broken"

    def parse(self, raw):
        return super().parse(raw)


@pytest.mark.whitebox
def test_implemented_base_methods_raise_not_implemented():
    with pytest.raises(NotImplementedError):
        list(BrokenConnector().discover())
    with pytest.raises(NotImplementedError):
        BrokenConnector().load(SourceRecord("x", "kind", "locator"))
    with pytest.raises(NotImplementedError):
        BrokenParser().parse(RawDocument("x", "src", "text", "text/plain"))


@pytest.mark.whitebox
def test_adapter_registry_and_builder_for_implemented_families():
    registry = AdapterRegistry()
    registry.register_connector("connector", ExampleConnector)
    registry.register_parser("parser", MarkdownParser)
    builder = AdapterBuilder(registry)

    assert builder.build_connector("connector").provider_name == "test"
    assert builder.build_parser("parser").parser_name == "markdown"
    with pytest.raises(ValueError):
        registry.get_connector("missing")
    with pytest.raises(ValueError):
        registry.get_parser("missing")


@pytest.mark.blackbox
def test_connector_and_parser_work_together(tmp_path: Path):
    local = tmp_path / "doc.md"
    local.write_text("# Title\n\nBody", encoding="utf-8")

    connector = ExampleLocalTextFileConnector(tmp_path)
    raw = connector.load(next(connector.discover()))
    parsed = MarkdownParser().parse(raw)

    assert raw.text().startswith("# Title")
    assert [element.type for element in parsed.elements] == ["heading", "paragraph"]
