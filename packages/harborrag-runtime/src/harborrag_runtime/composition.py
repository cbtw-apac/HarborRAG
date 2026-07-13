from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass, field

from harborrag_adapters.connectors.base import BaseConnector
from harborrag_adapters.connectors.schemas import ConnectorQuery
from harborrag_adapters.parsers.text import TextParser
from harborrag_core.domain.raw_document import RawDocument
from harborrag_core.domain.source import SourceRecord
from harborrag_engine.builder import EngineBuilder

from harborrag_runtime.services.base import BaseRuntimeService
from harborrag_runtime.services.mock import MockRuntimeService


@dataclass(frozen=True, slots=True)
class _MockIngestionSummary:
    """Local stand-in for ``harborrag_engine.ingestion.base.IngestionRunSummary``.

    Not imported directly: that package's ``__init__.py`` unconditionally
    imports its ``mock`` submodule, which fails today (depends on core
    modules that don't exist yet: ``harborrag_core.ports.models``,
    ``domain.metadata``, ``domain.provenance``). Same field shape so
    ``dataclasses.asdict`` output is unaffected either way.
    """

    discovered: int
    loaded: int
    parsed: int
    indexed: int


class _MockIngestionConnector(BaseConnector):
    """In-memory connector yielding one canned record for the health-check ingest path."""

    provider_name = "mock_runtime"

    def discover(self, query: ConnectorQuery | None = None) -> Iterator[SourceRecord]:
        yield SourceRecord(
            id="mock://composition/1",
            source_type="text/plain",
            locator="mock://composition/1",
        )

    def load(self, record: SourceRecord) -> RawDocument:
        return RawDocument(
            id=record.id,
            source=record.locator,
            content="HarborRAG mock ingestion content",
            content_type="text/plain",
        )


@dataclass(slots=True)
class _MockIngestionShim:
    """Reduced connector -> parser smoke check for the health-check ingest path.

    This is NOT the documented ``BaseIngestionPipeline`` contract: it skips
    normalization into ``HarborDocument`` because that step depends on core
    domain modules (``DocumentMetadata``/``DocumentProvenance``) that don't
    exist yet. It exists only so the runtime/app ingest smoke check has
    something real to run instead of raising ``AttributeError``.
    """

    connector: BaseConnector
    parser: TextParser
    _summary: _MockIngestionSummary = field(
        default_factory=lambda: _MockIngestionSummary(0, 0, 0, 0)
    )

    def run_once(self) -> list[RawDocument]:
        documents: list[RawDocument] = []
        for record in self.connector.discover():
            raw = self.connector.load(record)
            self.parser.parse(raw)
            documents.append(raw)
        self._summary = _MockIngestionSummary(
            len(documents), len(documents), len(documents), 0
        )
        return documents

    def summarize(self) -> _MockIngestionSummary:
        return self._summary


@dataclass(slots=True)
class CompositionRoot:
    engine_builder: EngineBuilder
    runtime_service: BaseRuntimeService = field(default_factory=MockRuntimeService)

    @classmethod
    def local(cls) -> CompositionRoot:
        return cls(engine_builder=EngineBuilder())

    def diagnostics(self) -> dict[str, object]:
        return {
            "runtime": self.runtime_service.diagnostics(),
            "engine": self.engine_builder.diagnostics(),
        }

    def mock_pipeline(self) -> _MockIngestionShim:
        """Build the connector/parser pair used by the ingest smoke check."""
        return _MockIngestionShim(
            connector=_MockIngestionConnector(), parser=TextParser()
        )

    def run_mock_ingestion(self) -> dict[str, object]:
        """Proxy to the runtime service so app/CLI callers don't reach into it."""
        return self.runtime_service.run_mock_ingestion()
