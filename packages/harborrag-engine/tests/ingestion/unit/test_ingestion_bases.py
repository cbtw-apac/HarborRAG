from __future__ import annotations

import pytest
from harborrag_engine.ingestion.base import (
    BaseChunker,
    BaseDocumentNormalizer,
    BaseIngestionPipeline,
    IngestionRunSummary,
)


class BrokenNormalizer(BaseDocumentNormalizer):
    def normalize(self, raw, parsed_text):
        return super().normalize(raw, parsed_text)


class BrokenChunker(BaseChunker):
    def chunk(self, document):
        return super().chunk(document)


class BrokenIngestionPipeline(BaseIngestionPipeline):
    def run_once(self):
        return super().run_once()

    def summarize(self):
        return super().summarize()


def test_ingestion_base_methods_raise() -> None:
    with pytest.raises(NotImplementedError):
        BrokenNormalizer().normalize(None, "text")
    with pytest.raises(NotImplementedError):
        BrokenChunker().chunk(None)
    with pytest.raises(NotImplementedError):
        BrokenIngestionPipeline().run_once()
    with pytest.raises(NotImplementedError):
        BrokenIngestionPipeline().summarize()


def test_ingestion_run_summary_is_a_plain_dataclass() -> None:
    summary = IngestionRunSummary(discovered=2, loaded=2, parsed=1, indexed=1)

    assert (summary.discovered, summary.loaded, summary.parsed, summary.indexed) == (
        2,
        2,
        1,
        1,
    )
