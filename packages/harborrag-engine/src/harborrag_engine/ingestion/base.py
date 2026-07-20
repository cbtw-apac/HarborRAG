from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass

from harborrag_core.domain.document import Document
from harborrag_core.domain.raw_document import RawDocument


@dataclass(frozen=True, slots=True)
class IngestionRunSummary:
    discovered: int
    loaded: int
    parsed: int
    indexed: int


class BaseDocumentNormalizer(ABC):
    """Normalize connector/parser outputs into a canonical Document.

    TODO: Implement a production normalizer that preserves source metadata, permissions,
    attachments, parser quality, graph hints, tenant namespace, and object-store artifact links.
    """

    @abstractmethod
    def normalize(self, raw: RawDocument, parsed_text: str) -> Document:
        raise NotImplementedError


class BaseChunker(ABC):
    """Split documents into retrieval chunks.

    TODO: Implement section-aware, table-preserving, code-aware, and late-chunking strategies.
    Add contract tests showing that table rows and headings are not destroyed.
    """

    @abstractmethod
    def chunk(self, document: Document) -> list[str]:
        raise NotImplementedError


class BaseIngestionPipeline(ABC):
    """Orchestrate connector -> parser -> normalizer -> repositories."""

    @abstractmethod
    def run_once(self) -> list[Document]:
        raise NotImplementedError

    @abstractmethod
    def summarize(self) -> IngestionRunSummary:
        raise NotImplementedError
