from __future__ import annotations

from abc import ABC, abstractmethod
from typing import TYPE_CHECKING

from harborrag_core.domain.normalized_document import Document
from harborrag_core.domain.parser import ParsedDocument
from harborrag_core.domain.raw_document import RawDocument

if TYPE_CHECKING:
    from harborrag_engine.ingestion.chunking.config import ChunkingPlan
    from harborrag_engine.ingestion.chunking.schemas import (
        ChunkingRequest,
        ChunkingResult,
    )


class BaseDocumentNormalizer(ABC):
    """Normalize connector/parser outputs into a canonical Document."""

    @abstractmethod
    def normalize(self, raw: RawDocument, parsed: ParsedDocument) -> Document:
        raise NotImplementedError


class HarborChunker(ABC):
    """Create canonical chunks from a normalized-document request and plan."""

    @abstractmethod
    def chunk(
        self,
        request: ChunkingRequest,
        plan: ChunkingPlan,
    ) -> ChunkingResult:
        raise NotImplementedError


class BaseChunker(HarborChunker):
    """Compatibility base for callers that still use configured engine profiles."""

    @abstractmethod
    def chunk(
        self,
        request: ChunkingRequest,
        plan: ChunkingPlan | None = None,
    ) -> ChunkingResult:
        raise NotImplementedError
