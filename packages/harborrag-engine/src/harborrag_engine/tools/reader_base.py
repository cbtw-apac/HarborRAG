"""Shared dependency boundary for reader tools."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from harborrag_core.contracts.errors import HarborCapabilityError
from harborrag_engine.tools.references import KnowledgeReferenceStore

from .base import BaseTool

if TYPE_CHECKING:
    from harborrag_core.ports.reader import KnowledgeReader, ReaderServices, RetrievalReader


@dataclass(slots=True)
class ReaderTool(BaseTool):
    runtime: ReaderServices | None = None
    references: KnowledgeReferenceStore = field(default_factory=KnowledgeReferenceStore)
    knowledge: KnowledgeReader | None = None
    retrieval: RetrievalReader | None = None

    def require_runtime(self) -> ReaderServices:
        if self.runtime is None:
            raise HarborCapabilityError("reader backend is not configured")
        return self.runtime

    def reference_store(self) -> KnowledgeReferenceStore:
        return self.references

    def require_knowledge(self) -> KnowledgeReader:
        service = self.knowledge or (self.runtime.knowledge if self.runtime is not None else None)
        if service is None:
            raise HarborCapabilityError("knowledge reader backend is not configured")
        return service

    def require_retrieval(self) -> RetrievalReader:
        service = self.retrieval or (self.runtime.retrieval if self.runtime is not None else None)
        if service is None:
            raise HarborCapabilityError("retrieval reader backend is not configured")
        return service


__all__ = ["ReaderTool"]
