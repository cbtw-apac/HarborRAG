from __future__ import annotations

import pytest

from harborrag_engine.ingestion.base import (
    BaseChunker,
    BaseDocumentNormalizer,
)


class BrokenNormalizer(BaseDocumentNormalizer):
    def normalize(self, raw, parsed_text):
        return super().normalize(raw, parsed_text)


class BrokenChunker(BaseChunker):
    def chunk(self, document):
        return super().chunk(document)


def test_ingestion_base_methods_raise() -> None:
    with pytest.raises(NotImplementedError):
        BrokenNormalizer().normalize(None, "text")
    with pytest.raises(NotImplementedError):
        BrokenChunker().chunk(None)
