from __future__ import annotations

from abc import ABC, abstractmethod
from typing import TYPE_CHECKING

from harborrag_core.domain.normalized_document import Document
from harborrag_core.domain.parser import ParsedDocument
from harborrag_core.domain.raw_document import RawDocument

if TYPE_CHECKING:
    from harborrag_engine.ingestion.chunking.schemas import (
        ChunkingRequest,
        ChunkingResult,
    )


class BaseDocumentNormalizer(ABC):
    """Normalize connector/parser outputs into a canonical Document."""

    @abstractmethod
    def normalize(self, raw: RawDocument, parsed: ParsedDocument) -> Document:
        raise NotImplementedError


class BaseChunker(ABC):
    """Split a normalized document into canonical records and a manifest."""

    @abstractmethod
    def chunk(self, request: ChunkingRequest) -> ChunkingResult:
        raise NotImplementedError
