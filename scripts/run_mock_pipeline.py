"""Run the deterministic local composition check end to end.

Wires the runtime's in-memory connector and real text parser, builds a small
retrieval input from the loaded documents, and exercises the deterministic
retrieval pipeline without an external provider.

Usage:
    python scripts/run_mock_pipeline.py [--json] [--query TEXT] [--top-k N]
"""

from __future__ import annotations

import argparse
import json
import sys

from harborrag_core.domain.retrieval import RetrievalQuery, RetrievalResult
from harborrag_engine.retrieval.mock import MockRetrievalPipeline
from harborrag_runtime.composition import CompositionRoot


def run_pipeline(query_text: str, top_k: int) -> dict[str, object]:
    pipeline = CompositionRoot.local().mock_pipeline()
    documents = pipeline.run_once()

    chunks: list[dict[str, object]] = []
    for document in documents:
        chunks.append(
            {
                "id": f"{document.id}#chunk-0",
                "document_id": document.id,
                "text": document.text(),
            }
        )

    retrieval_pipeline = MockRetrievalPipeline(
        results=[
            RetrievalResult(
                id=str(chunk["id"]),
                text=str(chunk["text"]),
                score=0.0,
                metadata={"document_id": chunk["document_id"]},
            )
            for chunk in chunks
        ]
    )
    retrieved = retrieval_pipeline.retrieve(RetrievalQuery(text=query_text, top_k=top_k))

    summary = pipeline.summarize()
    return {
        "documents": [
            {
                "id": document.id,
                "source": document.source,
                "content_type": document.content_type,
                "text": document.text(),
            }
            for document in documents
        ],
        "chunks": chunks,
        "retrieval": [
            {
                "id": result.id,
                "text": result.text,
                "score": result.score,
                "metadata": result.metadata,
            }
            for result in retrieved
        ],
        "summary": {
            "discovered": summary.discovered,
            "loaded": summary.loaded,
            "parsed": summary.parsed,
            "indexed": summary.indexed,
        },
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Run the deterministic mock pipeline.")
    parser.add_argument("--json", action="store_true", help="Print the full result as JSON.")
    parser.add_argument("--query", default="HarborRAG", help="Retrieval query text.")
    parser.add_argument("--top-k", type=int, default=5, help="Number of retrieval results.")
    args = parser.parse_args()

    result = run_pipeline(args.query, args.top_k)

    if args.json:
        print(json.dumps(result, indent=2))
        return 0

    documents, chunks, retrieval = (
        result["documents"],
        result["chunks"],
        result["retrieval"],
    )
    assert isinstance(documents, list) and isinstance(chunks, list) and isinstance(retrieval, list)
    print("Mock pipeline completed:")
    print(f"  documents: {len(documents)}")
    print(f"  chunks:    {len(chunks)}")
    print(f"  retrieved: {len(retrieval)}")
    print(f"  summary:   {result['summary']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
