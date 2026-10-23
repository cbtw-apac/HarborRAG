"""Protocol for connector-owned transformation of canonical documents."""

from __future__ import annotations

from collections.abc import Callable
from typing import Protocol, runtime_checkable

from harborrag_core.domain import Document, ParsedDocument, RawDocument


@runtime_checkable
class ConnectorDocumentTransform(Protocol):
    """Transform that enriches a generic document with one connector's source semantics."""

    def transform(
        self,
        raw: RawDocument,
        parsed: ParsedDocument,
        document: Document,
    ) -> Document:
        """Return the connector-specific canonical document.

        Args:
            raw: Raw document the connector loaded.
            parsed: Parsed form of ``raw``.
            document: Generic canonical document to enrich.
        """
        ...


ConnectorDocumentTransformFactory = Callable[[], ConnectorDocumentTransform]
"""Factory that builds an isolated connector document transform."""


__all__ = [
    "ConnectorDocumentTransform",
    "ConnectorDocumentTransformFactory",
]
