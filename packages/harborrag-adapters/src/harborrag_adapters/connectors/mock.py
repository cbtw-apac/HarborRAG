from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

from harborrag_core.domain.raw_document import RawDocument
from harborrag_core.domain.source import SourceRecord

from .base import BaseConnector
from .schemas import ConnectorCapabilities, ConnectorQuery


DEFAULT_MOCK_TEXT = "# Mock Document\n\nBody"


class MockConnector(BaseConnector):
    """In-memory connector used by tests, examples, and local composition.

    It emits a single deterministic document so the ingestion pipeline can be
    exercised end-to-end without any network or filesystem dependency.
    """

    provider_name = "mock"
    capabilities = ConnectorCapabilities(sync=True, full_sync=True)

    def __init__(self, text: str = DEFAULT_MOCK_TEXT, *, count: int = 1) -> None:
        self.text = text
        self.count = count

    def discover(self, query: ConnectorQuery | None = None) -> Iterator[SourceRecord]:
        limit = query.limit if query and query.limit is not None else self.count
        for index in range(min(self.count, limit)):
            yield SourceRecord(
                id=f"mock://document/{index}",
                source_type="text/markdown",
                locator=f"mock://document/{index}",
                metadata={"title": "Mock Document", "index": index},
            )

    def load(self, record: SourceRecord) -> RawDocument:
        return RawDocument(
            id=record.id,
            source=record.locator,
            content=self.text,
            content_type="text/markdown",
            metadata={"title": "Mock Document", **record.metadata},
        )


class MockLocalTextFileConnector(BaseConnector):
    """Connector that reads UTF-8 text files from a local directory.

    Unlike the production ``LocalFileConnector`` it performs no scope/symlink
    hardening; it exists purely to give deterministic fixtures a real on-disk
    source during tests and demos.
    """

    provider_name = "mock_local_text"
    capabilities = ConnectorCapabilities(sync=True, full_sync=True, local_files=True)

    def __init__(self, root: str | Path, *, pattern: str = "*.md") -> None:
        self.root = Path(root)
        self.pattern = pattern

    def discover(self, query: ConnectorQuery | None = None) -> Iterator[SourceRecord]:
        pattern = query.pattern if query and query.pattern else self.pattern
        for path in sorted(self.root.rglob(pattern)):
            if path.is_file():
                yield SourceRecord(
                    id=path.resolve().as_uri(),
                    source_type="text/markdown",
                    locator=str(path.resolve()),
                    metadata={"relative_path": str(path.relative_to(self.root))},
                )

    def load(self, record: SourceRecord) -> RawDocument:
        path = Path(record.locator)
        return RawDocument(
            id=record.id,
            source=record.locator,
            content=path.read_text(encoding="utf-8"),
            content_type="text/markdown",
            metadata=dict(record.metadata),
        )
