"""Document-family normalization policy."""

from __future__ import annotations

from harborrag_adapters.parsers.common.family import FamilyResultNormalizer


class DocumentNormalizer(FamilyResultNormalizer):
    """Convert a document engine result into the stable parser result."""

    def __init__(self) -> None:
        super().__init__("document")
