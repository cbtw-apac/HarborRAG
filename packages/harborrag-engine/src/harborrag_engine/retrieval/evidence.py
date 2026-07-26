from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from html import escape

from harborrag_core.domain.retrieval import RetrievalResult


@dataclass(frozen=True, slots=True)
class EvidenceBuilder:
    """Render bounded retrieved text as explicitly untrusted prompt data."""

    maximum_characters: int = 16_000

    def __post_init__(self) -> None:
        if self.maximum_characters < 1:
            raise ValueError("evidence maximum_characters must be positive")

    def build(self, results: Sequence[RetrievalResult]) -> str:
        remaining = self.maximum_characters
        sections: list[str] = ['<retrieved_evidence trust="untrusted">']
        for index, item in enumerate(results, start=1):
            if remaining <= 0:
                break
            text = item.text[:remaining]
            remaining -= len(text)
            identifier = escape(item.id, quote=True)
            sections.append(
                f'<document citation="{index}" id="{identifier}">\n{escape(text)}\n</document>'
            )
        sections.append("</retrieved_evidence>")
        return "\n".join(sections)
