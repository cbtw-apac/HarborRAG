#!/usr/bin/env python3
"""Run one minimal provider-independent engine ingestion smoke check."""

from __future__ import annotations

import json
import sys

from harborrag_core.chunking import RecordKind
from harborrag_core.contracts.chunking import TextRefinementRequest, TextSplit
from harborrag_core.domain import Document, DocumentElement, DocumentProvenance
from harborrag_engine.ingestion import (
    ChunkingConfig,
    ChunkingRequest,
    GraphProjectionBuilder,
    build_chunking_service,
)


class WordCounter:
    def count(self, text: str) -> int:
        return len(text.split())


class UnexpectedRefiner:
    def split(self, request: TextRefinementRequest) -> tuple[TextSplit, ...]:
        raise AssertionError(f"smoke document unexpectedly required refinement: {request}")


def run() -> dict[str, object]:
    document = Document(
        id="engine-smoke-document",
        title="Engine smoke",
        content_type="page",
        provenance=DocumentProvenance(source="local_file", record_id="smoke.md"),
        content=[
            DocumentElement("heading", "heading", "Operations", {"level": 1}),
            DocumentElement(
                "paragraph",
                "paragraph",
                "HarborRAG keeps ingestion package boundaries explicit.",
            ),
        ],
    )
    chunking = build_chunking_service(
        config=ChunkingConfig(create_route_chunks=True),
        token_counter=WordCounter(),
        refiner=UnexpectedRefiner(),
    ).chunk(
        ChunkingRequest(
            tenant_id="smoke",
            document_version_id="engine-smoke-version",
            connector_type="local",
            document=document,
        )
    )
    graph = GraphProjectionBuilder().build_structural(
        document=document,
        chunks=chunking.chunks,
        graph_projection_version="smoke-v1",
    )

    record_kinds = {chunk.record_kind for chunk in chunking.chunks}
    checks = {
        "chunk_manifest_valid": chunking.manifest.validation.valid,
        "route_chunk_created": RecordKind.ROUTE in record_kinds,
        "evidence_chunk_created": RecordKind.EVIDENCE in record_kinds,
        "graph_nodes_created": bool(graph.nodes),
        "graph_manifest_created": bool(graph.manifest.payload_sha256),
    }
    failed = [name for name, passed in checks.items() if not passed]
    if failed:
        raise RuntimeError(f"failed smoke checks: {', '.join(failed)}")
    return {
        "status": "PASS",
        "chunks": len(chunking.chunks),
        "graph_nodes": len(graph.nodes),
        "graph_relations": len(graph.relations),
    }


def main() -> int:
    try:
        result = run()
    except Exception as error:
        print(f"Engine ingestion smoke failed: {type(error).__name__}: {error}", file=sys.stderr)
        return 1
    print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
