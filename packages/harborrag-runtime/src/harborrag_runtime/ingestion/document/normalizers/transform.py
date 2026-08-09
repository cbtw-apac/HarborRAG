"""Runtime bridge between generic normalization and connector transforms."""

from __future__ import annotations

from harborrag_adapters.connectors.document_transform import ConnectorDocumentTransform
from harborrag_core.domain import Document, ParsedDocument, RawDocument
from harborrag_engine.ingestion import BaseDocumentNormalizer


class ConnectorTransformDocumentNormalizer(BaseDocumentNormalizer):
    """Apply a connector-owned transform after generic engine normalization."""

    def __init__(
        self,
        *,
        fallback: BaseDocumentNormalizer,
        transform: ConnectorDocumentTransform,
    ) -> None:
        if not isinstance(transform, ConnectorDocumentTransform):
            raise TypeError("connector transform must implement ConnectorDocumentTransform")
        self._fallback = fallback
        self._transform = transform

    def normalize(
        self,
        raw: RawDocument,
        parsed: ParsedDocument,
    ) -> Document:
        document = self._fallback.normalize(raw, parsed)
        return self._transform.transform(raw, parsed, document)
