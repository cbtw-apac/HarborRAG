from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Iterator

from harborrag_core.domain.raw_document import RawDocument
from harborrag_core.domain.source import SourceRecord

from .schemas import ConnectorCapabilities, ConnectorQuery

class BaseConnector(ABC):
    provider_name: str = "base"
    connector_version: str | None = "1.0.0"
    capabilities: ConnectorCapabilities = ConnectorCapabilities()

    def connect(self) -> None:
        return None

    @abstractmethod
    def discover(self, query: ConnectorQuery | None = None) -> list[SourceRecord]:
        raise NotImplementedError

    @abstractmethod
    def load(self, record: SourceRecord) -> RawDocument:
        raise NotImplementedError

    def load_raw_documents(self, query: ConnectorQuery | None = None) -> Iterator[RawDocument]:
        self.connect()
        for record in self.discover(query):
            yield self.load(record)
