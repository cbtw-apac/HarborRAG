from __future__ import annotations

import pytest

from harborrag_core.indexing import VectorSearchResult
from harborrag_engine.retrieval.service.result_loader import EvidenceResultLoader

pytestmark = pytest.mark.unit


@pytest.mark.parametrize(
    ("stored", "expected"),
    [(["text", "table"], ["text", "table"]), ("text", ["text"])],
    ids=["list", "single-string-from-older-points"],
)
def test_evidence_result_reads_chunk_kind_as_a_list(stored: object, expected: list[str]) -> None:
    result = EvidenceResultLoader.result(_candidate(stored))

    assert result.metadata["chunk_kind"] == expected


@pytest.mark.parametrize("stored", [None, [], [""], [1], " "], ids=str)
def test_evidence_result_rejects_a_missing_or_blank_chunk_kind(stored: object) -> None:
    with pytest.raises(ValueError, match="chunk_kind"):
        EvidenceResultLoader.result(_candidate(stored))


def _candidate(chunk_kind: object) -> VectorSearchResult:
    payload: dict[str, object] = {
        "chunk_id": "chunk-1",
        "document_id": "document-1",
        "document_version_id": "version-1",
        "record_kind": "evidence",
        "connector_type": "local",
        "content": "| Step | Result |\n| --- | --- |\n| 1 | Saved |",
    }
    if chunk_kind is not None:
        payload["chunk_kind"] = chunk_kind
    return VectorSearchResult(id="point-1", score=0.9, raw_score=0.9, payload=payload)
