from __future__ import annotations

import pytest

from harborrag_adapters.chunking.confluence import (
    ConfluencePageInput,
    ConfluencePageNormalizer,
)
from harborrag_engine.ingestion.chunking import (
    CanonicalTableChunker,
    ChunkingPlan,
    TableChunkingRequest,
)

pytestmark = [pytest.mark.integration, pytest.mark.blackbox]


class _WordCounter:
    def count(self, text: str) -> int:
        return len(text.split())


def test_adf_page_table_flows_to_canonical_chunk_records():
    page = ConfluencePageInput(
        page_id="42",
        page_version="3",
        space_id="space-1",
        space_key="ENG",
        title="Deployment Guide",
        source_url="https://example.atlassian.net/wiki/spaces/ENG/pages/42",
        adf={
            "type": "doc",
            "version": 1,
            "content": [
                {
                    "type": "heading",
                    "attrs": {"level": 1},
                    "content": [{"type": "text", "text": "Limits"}],
                },
                {
                    "type": "table",
                    "attrs": {"localId": "limits"},
                    "content": [
                        {
                            "type": "tableRow",
                            "content": [
                                {
                                    "type": "tableHeader",
                                    "content": [
                                        {
                                            "type": "paragraph",
                                            "content": [{"type": "text", "text": "Service"}],
                                        }
                                    ],
                                },
                                {
                                    "type": "tableHeader",
                                    "content": [
                                        {
                                            "type": "paragraph",
                                            "content": [{"type": "text", "text": "CPU"}],
                                        }
                                    ],
                                },
                            ],
                        },
                        {
                            "type": "tableRow",
                            "content": [
                                {
                                    "type": "tableCell",
                                    "content": [
                                        {
                                            "type": "paragraph",
                                            "content": [{"type": "text", "text": "worker"}],
                                        }
                                    ],
                                },
                                {
                                    "type": "tableCell",
                                    "content": [
                                        {
                                            "type": "paragraph",
                                            "content": [{"type": "text", "text": "2"}],
                                        }
                                    ],
                                },
                            ],
                        },
                    ],
                },
            ],
        },
    )
    document = ConfluencePageNormalizer().normalize(page)
    artifact = document.table_artifacts[0]

    result = CanonicalTableChunker(_WordCounter()).chunk(
        TableChunkingRequest(
            artifact=artifact,
            tenant_id="tenant",
            connection_id="confluence",
            source_scope="ENG",
            page_title=document.title,
            space="Engineering",
        ),
        ChunkingPlan(
            minimum_tokens=1, target_tokens=50, soft_maximum_tokens=75, hard_maximum_tokens=100
        ),
    )

    assert {chunk.table_locator.table_id for chunk in result.chunks} == {artifact.table_id}
    assert all(chunk.document_id == document.id for chunk in result.chunks)
    assert all(chunk.hierarchy.section_path == ("Limits",) for chunk in result.chunks)
