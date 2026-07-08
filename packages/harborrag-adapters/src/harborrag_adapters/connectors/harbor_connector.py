from __future__ import annotations

from collections.abc import Iterator

from harborrag_core.domain import RawDocument, SourceRecord

from .schemas import ConnectorCapabilities, ConnectorQuery
from .registry import connector_registry


class HarborConnector:
    def __init__(self, provider: str, **kwargs) -> None:
        self.provider_name = provider
        self.provider = connector_registry.create(provider, **kwargs)

    @property
    def capabilities(self) -> ConnectorCapabilities:
        return self.provider.capabilities

    def discover(self, query: ConnectorQuery | None = None) -> list[SourceRecord]:
        return self.provider.discover(query)

    def load(self, record: SourceRecord) -> RawDocument:
        return self.provider.load(record)

    def load_raw_documents(self, query: ConnectorQuery | None = None) -> Iterator[RawDocument]:
        return self.provider.load_raw_documents(query)

    @classmethod
    def providers(cls) -> list[str]:
        return connector_registry.names()
