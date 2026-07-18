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


@pytest.mark.whitebox
def test_adapter_registry_model_and_repository_slots():
    from harborrag_adapters.repositories.cache.mock import MockCacheRepository

    class _ExampleModel:
        """Stand-in class for the model registry slot (no real model impl is importable yet)."""

    registry = AdapterRegistry()
    registry.register_model("example_model", _ExampleModel)
    registry.register_repository("cache", MockCacheRepository)

    assert registry.get_model("example_model") is _ExampleModel
    assert registry.get_repository("cache") is MockCacheRepository
    with pytest.raises(ValueError, match="Unknown model"):
        registry.get_model("missing")
    with pytest.raises(ValueError, match="Unknown repository"):
        registry.get_repository("missing")


@pytest.mark.whitebox
def test_adapter_builder_model_repository_and_connector_registry_fallback():
    from harborrag_adapters.connectors.registry import connector_registry
    from harborrag_adapters.repositories.cache.mock import MockCacheRepository

    class _ExampleModel:
        def __init__(self, provider_name: str = "example") -> None:
            self.provider_name = provider_name

    registry = AdapterRegistry()
    registry.register_model("example_model", _ExampleModel)
    registry.register_repository("cache", MockCacheRepository)
    builder = AdapterBuilder(registry)

    assert builder.build_model("example_model").provider_name == "example"
    assert isinstance(builder.build_repository("cache"), MockCacheRepository)

    # Not registered on this AdapterRegistry instance -> falls back to the
    # shared connector_registry singleton instead of raising.
    connector_registry.register("builder-fallback-stub", ExampleConnector)
    try:
        connector = builder.build_connector("builder-fallback-stub")
        assert isinstance(connector, ExampleConnector)
    finally:
        connector_registry.unregister("builder-fallback-stub")


@pytest.mark.whitebox
def test_repository_base_methods_raise_not_implemented():
    from harborrag_adapters.repositories.cache.base import BaseCacheRepository
    from harborrag_adapters.repositories.database.base import BaseDatabaseRepository
    from harborrag_adapters.repositories.object_store.base import BaseObjectRepository
    from harborrag_adapters.repositories.vector.base import BaseVectorRepository

    class BrokenCache(BaseCacheRepository):
        def get(self, key):
            return super().get(key)

        def set(self, key, value, ttl_seconds=None):
            return super().set(key, value, ttl_seconds)

    class BrokenDatabase(BaseDatabaseRepository):
        def execute(self, statement, parameters=None):
            return super().execute(statement, parameters)

    class BrokenObjectStore(BaseObjectRepository):
        def put_bytes(self, key, data, content_type=None):
            return super().put_bytes(key, data, content_type)

        def get_bytes(self, key):
            return super().get_bytes(key)

    class BrokenVector(BaseVectorRepository):
        def upsert(self, items):
            return super().upsert(items)

        def search(self, vector, top_k=10):
            return super().search(vector, top_k)

    with pytest.raises(NotImplementedError):
        BrokenCache().get("key")
    with pytest.raises(NotImplementedError):
        BrokenCache().set("key", "value")
    with pytest.raises(NotImplementedError):
        BrokenDatabase().execute("select 1")
    with pytest.raises(NotImplementedError):
        BrokenObjectStore().put_bytes("key", b"data")
    with pytest.raises(NotImplementedError):
        BrokenObjectStore().get_bytes("key")
    with pytest.raises(NotImplementedError):
        BrokenVector().upsert([])
    with pytest.raises(NotImplementedError):
        BrokenVector().search([0.1])


@pytest.mark.whitebox
def test_mock_cache_repository_round_trips_values():
    from harborrag_adapters.repositories.cache.mock import MockCacheRepository

    cache = MockCacheRepository()
    assert cache.get("missing") is None
    cache.set("key", "value", ttl_seconds=60)
    assert cache.get("key") == "value"


@pytest.mark.whitebox
def test_mock_database_repository_records_statements():
    from harborrag_adapters.repositories.database.mock import MockDatabaseRepository

    database = MockDatabaseRepository()
    result = database.execute("select * from docs where id = ?", ["doc-1"])

    assert result == []
    assert database.statements == [("select * from docs where id = ?", ("doc-1",))]


@pytest.mark.whitebox
def test_mock_object_repository_stores_and_returns_bytes():
    from harborrag_adapters.repositories.object_store.mock import MockObjectRepository

    store = MockObjectRepository()
    location = store.put_bytes("attachments/a.txt", b"hello")

    assert location == "memory://attachments/a.txt"
    assert store.get_bytes("attachments/a.txt") == b"hello"


@pytest.mark.whitebox
def test_mock_vector_repository_upserts_and_searches_by_score():
    from harborrag_adapters.repositories.vector.mock import MockVectorRepository

    repository = MockVectorRepository()
    repository.upsert(
        [
            {"id": "a", "text": "alpha", "vector": [1.0, 0.0], "metadata": {"k": 1}},
            {"id": "b", "text": "beta", "vector": [0.0, 1.0], "metadata": {}},
        ]
    )

    results = repository.search([1.0, 0.0], top_k=1)

    assert len(results) == 1
    assert results[0].id == "a"
    assert results[0].metadata == {"k": 1}


@pytest.mark.blackbox
def test_connector_and_parser_work_together(tmp_path: Path):
    local = tmp_path / "doc.md"
    local.write_text("# Title\n\nBody", encoding="utf-8")

    connector = ExampleLocalTextFileConnector(tmp_path)
    raw = connector.load(next(connector.discover()))
    parsed = MarkdownParser().parse(raw)

    assert raw.text().startswith("# Title")
    assert [element.type for element in parsed.elements] == ["heading", "paragraph"]
