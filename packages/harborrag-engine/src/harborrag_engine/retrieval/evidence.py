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
        opening = '<retrieved_evidence trust="untrusted">'
        closing = "</retrieved_evidence>"
        minimum = len(opening) + 1 + len(closing)
        if minimum > self.maximum_characters:
            return ""
        sections: list[str] = [opening]
        rendered_length = minimum
        for index, item in enumerate(results, start=1):
            identifier = escape(item.id[:128], quote=True)
            available = self.maximum_characters - rendered_length - 1
            empty_section = f'<document citation="{index}" id="{identifier}">\n\n</document>'
            if len(empty_section) > available:
                break
            low = 0
            high = len(item.text)
            while low < high:
                midpoint = (low + high + 1) // 2
                candidate = (
                    f'<document citation="{index}" id="{identifier}">\n'
                    f"{escape(item.text[:midpoint])}\n</document>"
                )
                if len(candidate) <= available:
                    low = midpoint
                else:
                    high = midpoint - 1
            section = (
                f'<document citation="{index}" id="{identifier}">\n'
                f"{escape(item.text[:low])}\n</document>"
            )
            sections.append(section)
            rendered_length += len(section) + 1
        sections.append(closing)
        return "\n".join(sections)
