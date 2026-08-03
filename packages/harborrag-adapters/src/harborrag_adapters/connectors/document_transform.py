"""Connector-owned transformation of generic canonical documents."""

from __future__ import annotations

from collections.abc import Callable
from typing import Protocol, runtime_checkable

from harborrag_core.domain import Document, ParsedDocument, RawDocument


@runtime_checkable
class ConnectorDocumentTransform(Protocol):
    """Enrich a generic document using one connector's source semantics."""

    def transform(
        self,
        raw: RawDocument,
        parsed: ParsedDocument,
        document: Document,
    ) -> Document:
        """Return the connector-specific canonical representation."""
        ...


ConnectorDocumentTransformFactory = Callable[[], ConnectorDocumentTransform]
"""Construct an isolated connector document transform."""


__all__ = [
    "ConnectorDocumentTransform",
    "ConnectorDocumentTransformFactory",
]
