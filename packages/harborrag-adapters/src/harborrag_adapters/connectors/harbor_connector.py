"""Provider-name facade the runtime uses to build connectors."""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any

from harborrag_core.domain.raw_document import RawDocument
from harborrag_core.domain.source import SourceRecord

from .descriptors import ConnectorDocumentDescriptor
from .registry import connector_registry
from .schemas import ConnectorCapabilities, ConnectorPage, ConnectorQuery, ConnectorSkip


class HarborConnector:
    """Facade that creates a provider connector by name and delegates to it."""

    def __init__(self, provider: str, **kwargs: Any) -> None:
        """Create the provider connector from the registry.

        Args:
            provider: Registered provider name or alias.
            **kwargs: Constructor arguments forwarded to the provider class.
        """
        self.provider_name = provider
        self.provider = connector_registry.create(provider, **kwargs)

    @property
    def capabilities(self) -> ConnectorCapabilities:
        """Return the provider's advertised capabilities."""
        return self.provider.capabilities

    @property
    def skipped(self) -> tuple[ConnectorSkip, ...]:
        """Return the provider's reported skips from its last traversal."""
        return self.provider.skipped

    def discover(self, query: ConnectorQuery | None = None) -> Iterator[SourceRecord]:
        """Delegate discovery to the provider."""
        return self.provider.discover(query)

    def discover_page(
        self,
        query: ConnectorQuery | None,
        *,
        cursor: str | None,
        page_size: int,
    ) -> ConnectorPage:
        """Delegate cursor-based paged discovery to the provider."""
        return self.provider.discover_page(
            query,
            cursor=cursor,
            page_size=page_size,
        )

    def connect(self) -> None:
        """Connect the provider."""
        self.provider.connect()

    def close(self) -> None:
        """Close the provider."""
        self.provider.close()

    def load(self, record: SourceRecord) -> RawDocument:
        """Delegate loading of one record to the provider."""
        return self.provider.load(record)

    def describe(
        self,
        record: SourceRecord,
    ) -> ConnectorDocumentDescriptor:
        """Delegate building the admission descriptor to the provider."""
        return self.provider.describe(record)

    def load_raw_documents(
        self,
        query: ConnectorQuery | None = None,
    ) -> Iterator[RawDocument]:
        """Delegate the discover-then-load stream to the provider."""
        return self.provider.load_raw_documents(query)

    @classmethod
    def providers(cls) -> list[str]:
        """Return registered provider names and aliases."""
        return connector_registry.names()
