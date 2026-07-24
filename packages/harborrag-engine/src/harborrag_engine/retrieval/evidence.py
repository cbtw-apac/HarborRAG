from __future__ import annotations

from collections.abc import Sequence

from harborrag_core.domain.retrieval import RetrievalResult
from harborrag_engine.retrieval.base import BaseEvidenceBuilder


class EvidenceBuilder(BaseEvidenceBuilder):
    """Render retrieval results into a compact, citation-numbered context."""

    def build(self, results: Sequence[RetrievalResult]) -> str:
        return "\n\n".join(f"[{idx}] {item.text}" for idx, item in enumerate(results, start=1))
