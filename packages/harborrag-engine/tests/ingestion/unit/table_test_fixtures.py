from __future__ import annotations

from harborrag_adapters.chunking.confluence import (
    ConfluencePageInput,
    ConfluencePageNormalizer,
)
from harborrag_core.domain import TableArtifact
from harborrag_engine.ingestion.chunking import (
    ChunkingPlan,
    TableChunkingPolicy,
    TableChunkingRequest,
    TableClassificationThresholds,
)


class CharacterTokenCounter:
    def count(self, text: str) -> int:
        return len(text)


def make_artifact(
    headers: list[str],
    rows: list[list[str]],
    *,
    table_id: str = "table-source",
    caption: str | None = None,
) -> TableArtifact:
    header_row = {
        "type": "tableRow",
        "content": [
            {
                "type": "tableHeader",
                "content": [
                    {
                        "type": "paragraph",
                        "content": [{"type": "text", "text": value}],
                    }
                ],
            }
            for value in headers
        ],
    }
    body_rows = [
        {
            "type": "tableRow",
            "content": [
                {
                    "type": "tableCell",
                    "content": [
                        {
                            "type": "paragraph",
                            "content": [{"type": "text", "text": value}],
                        }
                    ],
                }
                for value in row
            ],
        }
        for row in rows
    ]
    attributes: dict[str, object] = {"localId": table_id}
    if caption:
        attributes["caption"] = caption
    document = ConfluencePageNormalizer().normalize(
        ConfluencePageInput(
            page_id="42",
            page_version="7",
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
                        "content": [{"type": "text", "text": "Resource Limits"}],
                    },
                    {
                        "type": "table",
                        "attrs": attributes,
                        "content": [header_row, *body_rows],
                    },
                ],
            },
        )
    )
    return document.table_artifacts[0]


def thresholds(**changes: object) -> TableClassificationThresholds:
    values = {
        "small_maximum_rows": 3,
        "small_maximum_columns": 3,
        "small_maximum_tokens": 10_000,
        "long_minimum_rows": 4,
        "wide_minimum_columns": 4,
        "large_minimum_rows": 10,
        "large_minimum_cells": 100,
        "large_minimum_tokens": 100_000,
        "matrix_confidence": 0.75,
        "time_series_confidence": 0.70,
    }
    values.update(changes)
    return TableClassificationThresholds(**values)


def make_plan(**policy_changes: object) -> ChunkingPlan:
    policy_values = {
        "thresholds": thresholds(),
        "target_rows_per_chunk": 2,
        "maximum_rows_per_chunk": 3,
        "target_tokens_per_chunk": 180,
        "maximum_columns_per_group": 3,
        "maximum_key_columns": 2,
    }
    policy_values.update(policy_changes)
    return ChunkingPlan(
        profile="table-test",
        strategy_version="table-v1",
        minimum_tokens=10,
        target_tokens=300,
        soft_maximum_tokens=400,
        hard_maximum_tokens=500,
        table_policy=TableChunkingPolicy(**policy_values),
    )


def make_request(artifact: TableArtifact) -> TableChunkingRequest:
    return TableChunkingRequest(
        artifact=artifact,
        tenant_id="tenant-1",
        connection_id="confluence-main",
        source_scope="ENG",
        page_title="Deployment Guide",
        space="Platform Engineering",
        permissions={"groups": ("engineering",)},
    )
