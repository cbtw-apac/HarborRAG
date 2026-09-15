"""Shared dependency boundary for reader tools."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from harborrag_core.contracts.errors import HarborCapabilityError
from harborrag_runtime.tools.references import KnowledgeReferenceStore

from .base import BaseTool

if TYPE_CHECKING:
    from harborrag_runtime.sdk import HarborRAG


@dataclass(slots=True)
class ReaderTool(BaseTool):
    runtime: HarborRAG | None = None
    references: KnowledgeReferenceStore = field(default_factory=KnowledgeReferenceStore)

    def require_runtime(self) -> HarborRAG:
        if self.runtime is None:
            raise HarborCapabilityError("reader backend is not configured")
        return self.runtime

    def reference_store(self) -> KnowledgeReferenceStore:
        return self.references


__all__ = ["ReaderTool"]
