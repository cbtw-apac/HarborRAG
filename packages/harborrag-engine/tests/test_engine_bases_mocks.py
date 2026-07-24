from __future__ import annotations

import pytest

from harborrag_core.domain.document import Document
from harborrag_core.domain.element import DocumentElement
from harborrag_core.domain.provenance import DocumentProvenance
from harborrag_core.domain.retrieval import RetrievalQuery, RetrievalResult
from harborrag_engine.builder import EngineBuilder
from harborrag_engine.config import EngineConfig
from harborrag_engine.indexing.base import BaseIndexer
from harborrag_engine.indexing.mock import MockIndexer
from harborrag_engine.ingestion.base import (
    BaseChunker,
    BaseDocumentNormalizer,
    BaseIngestionPipeline,
    IngestionRunSummary,
)
from harborrag_engine.policy import EnginePolicy
from harborrag_engine.retrieval.base import BaseEvidenceBuilder, BaseRetrievalPipeline
from harborrag_engine.retrieval.fusion import reciprocal_rank_fusion
from harborrag_engine.retrieval.mock import MockEvidenceBuilder
from harborrag_engine.retrieval.reranking import keep_top
from harborrag_engine.retrieval.rewriting import identity_rewrite


class BrokenRetrieval(BaseRetrievalPipeline):
    def retrieve(self, query):
        return super().retrieve(query)


class BrokenEvidence(BaseEvidenceBuilder):
    def build(self, results):
        return super().build(results)


class BrokenIndexer(BaseIndexer):
    def index(self, documents):
        return super().index(documents)


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


def test_implemented_engine_base_methods_raise():
    with pytest.raises(NotImplementedError):
        BrokenRetrieval().retrieve(RetrievalQuery("q"))
    with pytest.raises(NotImplementedError):
        BrokenEvidence().build([])
    with pytest.raises(NotImplementedError):
        BrokenIndexer().index([])
    with pytest.raises(NotImplementedError):
        BrokenNormalizer().normalize(None, "text")
    with pytest.raises(NotImplementedError):
        BrokenChunker().chunk(None)
    with pytest.raises(NotImplementedError):
        BrokenIngestionPipeline().run_once()
    with pytest.raises(NotImplementedError):
        BrokenIngestionPipeline().summarize()


def test_ingestion_run_summary_is_a_plain_dataclass():
    summary = IngestionRunSummary(discovered=2, loaded=2, parsed=1, indexed=1)
    assert (summary.discovered, summary.loaded, summary.parsed, summary.indexed) == (
        2,
        2,
        1,
        1,
    )


def test_mock_evidence_builder_numbers_results_in_order():
    builder = MockEvidenceBuilder()
    results = [
        RetrievalResult("a", "first", 0.9),
        RetrievalResult("b", "second", 0.5),
    ]

    evidence = builder.build(results)

    assert evidence == "[1] first\n\n[2] second"


def test_mock_indexer_records_indexed_document_ids():
    indexer = MockIndexer(indexed=[])
    document = Document(
        id="doc-1",
        title="Doc 1",
        content=[DocumentElement(id="doc-1:0", type="paragraph", content="body")],
        content_type="page",
        provenance=DocumentProvenance(source="local_file"),
    )

    count = indexer.index([document])

    assert count == 1
    assert indexer.indexed == ["doc-1"]


def test_engine_builder_and_policy():
    diagnostics = EngineBuilder(
        EngineConfig(tenant="t", environment="test"),
        EnginePolicy(max_concurrency=2),
    ).diagnostics()

    assert diagnostics["tenant"] == "t"
    assert diagnostics["environment"] == "test"
    with pytest.raises(ValueError):
        EnginePolicy(max_concurrency=0)


def test_retrieval_helpers():
    results = [
        RetrievalResult("a", "harbor rag", 0.1),
        RetrievalResult("b", "other", 0.9),
    ]

    fused = reciprocal_rank_fusion([[results[0], results[1]], [results[1]]])

    assert fused[0].id == "b"
    assert identity_rewrite("hello") == ["hello"]
    assert keep_top(results, 1)[0].id == "b"
