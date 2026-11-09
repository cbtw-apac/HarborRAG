"""Markup-family text normalization policy."""

from harborrag_adapters.parsers.common.family import FamilyResultNormalizer
from harborrag_adapters.parsers.common.normalization import (
    compact_text as compact_text,
)
from harborrag_adapters.parsers.common.normalization import (
    html_to_text as html_to_text,
)
from harborrag_adapters.parsers.common.normalization import (
    html_to_text_with_engine as html_to_text_with_engine,
)


class MarkupNormalizer(FamilyResultNormalizer):
    """Convert markup engine output into the stable parser result."""

    def __init__(self) -> None:
        super().__init__("markup")
