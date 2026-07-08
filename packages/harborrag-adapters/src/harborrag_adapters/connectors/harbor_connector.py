from __future__ import annotations

from collections.abc import Iterator

from harborrag_core.domain.raw_document import RawDocument
from harborrag_core.domain.source import SourceRecord

from .registry import connector_registry
from .schemas import ConnectorCapabilities, ConnectorQuery


class HarborConnector:
    """Factory facade that hides provider-class lookup from callers."""

    def __init__(self, provider: str, **kwargs) -> None:
        """Create a concrete connector from the provider registry."""
        self.provider_name = provider
        self.provider = connector_registry.create(provider, **kwargs)

    @property
    def capabilities(self) -> ConnectorCapabilities:
        """Expose the selected provider's advertised capabilities."""
        return self.provider.capabilities

    def discover(self, query: ConnectorQuery | None = None) -> Iterator[SourceRecord]:
        """Proxy discovery to the selected provider."""
        return self.provider.discover(query)

    def load(self, record: SourceRecord) -> RawDocument:
        """Proxy one-record loading to the selected provider."""
        return self.provider.load(record)

    def load_raw_documents(
        self,
        query: ConnectorQuery | None = None,
    ) -> Iterator[RawDocument]:
        """Proxy the provider's discover-then-load convenience stream."""
        return self.provider.load_raw_documents(query)

    @classmethod
    def providers(cls) -> list[str]:
        """List registered provider names and aliases."""
        return connector_registry.names()
