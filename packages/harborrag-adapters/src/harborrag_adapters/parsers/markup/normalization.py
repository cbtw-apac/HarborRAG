"""Markup-family text normalization policy."""

from harborrag_adapters._shared.text.normalization import (
    compact_text,
    html_to_text,
    html_to_text_with_engine,
)
from harborrag_adapters.parsers.common.family import FamilyResultNormalizer


class MarkupNormalizer(FamilyResultNormalizer):
    """Convert markup engine output into the stable parser result."""

    def __init__(self) -> None:
        super().__init__("markup")


__all__ = [
    "MarkupNormalizer",
    "compact_text",
    "html_to_text",
    "html_to_text_with_engine",
]
