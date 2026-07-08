from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Iterator

from harborrag_core.domain.raw_document import RawDocument
from harborrag_core.domain.source import SourceRecord

from .schemas import ConnectorCapabilities, ConnectorQuery


class BaseConnector(ABC):
    """Common sync connector contract.

    Connectors are responsible for source-specific discovery and loading only.
    They return core domain objects so runtime can handle orchestration,
    concurrency, scheduling, checkpointing, and parsing independently.
    """

    provider_name: str = "base"
    connector_version: str | None = "1.0.0"
    capabilities: ConnectorCapabilities = ConnectorCapabilities()

    def connect(self) -> None:
        """Perform an optional eager connection check.

        Most providers authenticate lazily during the first API request. Override
        this only when a connector needs an explicit session setup or health
        check before discovery starts.
        """
        return None

    @abstractmethod
    def discover(self, query: ConnectorQuery | None = None) -> Iterator[SourceRecord]:
        """Yield lightweight records that identify loadable source items."""
        raise NotImplementedError

    @abstractmethod
    def load(self, record: SourceRecord) -> RawDocument:
        """Fetch one raw document for a previously discovered source record."""
        raise NotImplementedError

    def load_raw_documents(
        self,
        query: ConnectorQuery | None = None,
    ) -> Iterator[RawDocument]:
        """Convenience stream that discovers records and loads them in order."""
        self.connect()
        for record in self.discover(query):
            yield self.load(record)
