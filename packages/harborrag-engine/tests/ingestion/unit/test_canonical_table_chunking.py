from __future__ import annotations

from dataclasses import replace

import pytest

from harborrag_core.chunking import ChunkKind, TableProjectionType
from harborrag_engine.ingestion.chunking import (
    CanonicalTableChunker,
    TableShape,
)

from .table_test_fixtures import (
    CharacterTokenCounter,
    make_artifact,
    make_plan,
    make_request,
)

pytestmark = [pytest.mark.unit, pytest.mark.blackbox]


def test_small_table_produces_route_and_bounded_evidence_chunk_with_exact_context():
    artifact = make_artifact(
        ["Service", "CPU (cores)"],
        [["worker", "2"], ["api", "4"]],
        caption="Worker Configuration",
    )
    plan = make_plan()

    result = CanonicalTableChunker(CharacterTokenCounter()).chunk(
        make_request(artifact),
        plan,
    )

    assert result.classification.shape == TableShape.SMALL
    assert [chunk.metadata["table_chunk_role"] for chunk in result.chunks] == [
        "route",
        "evidence",
    ]
    evidence = result.chunks[1]
    assert evidence.chunk_kind == ChunkKind.TABLE
    assert evidence.content == "Service\tCPU (cores)\nworker\t2\napi\t4"
    assert evidence.embedding_text.startswith("Connector: Confluence")
    assert "Page: Deployment Guide" in evidence.contextual_prefix
    assert "Section: Resource Limits" in evidence.contextual_prefix
    assert "Table: Worker Configuration" in evidence.contextual_prefix
    assert evidence.table_locator is not None
    assert (evidence.table_locator.row_start, evidence.table_locator.row_end) == (1, 2)
    assert evidence.table_locator.selected_columns == ("Service", "CPU (cores)")
    assert "worker" in evidence.search_text
    assert evidence.token_count <= plan.hard_maximum_tokens


def test_long_table_row_groups_cover_rows_once_without_default_overlap():
    artifact = make_artifact(
        ["Service", "Description"],
        [[f"service-{index}", f"description-{index}"] for index in range(7)],
    )
    plan = make_plan(target_rows_per_chunk=2, maximum_rows_per_chunk=2)

    result = CanonicalTableChunker(CharacterTokenCounter()).chunk(
        make_request(artifact),
        plan,
    )
    evidence = [
        chunk for chunk in result.chunks if chunk.metadata["table_chunk_role"] == "evidence"
    ]
    rows = [
        row
        for chunk in evidence
        for row in range(
            chunk.table_locator.row_start,  # type: ignore[union-attr]
            chunk.table_locator.row_end + 1,  # type: ignore[union-attr]
        )
    ]

    assert result.classification.shape == TableShape.LONG
    assert rows == list(range(1, 8))
    assert all("Service\tDescription" in chunk.content for chunk in evidence)
    assert all(chunk.token_count <= plan.hard_maximum_tokens for chunk in evidence)


def test_boundary_overlap_is_exactly_one_row_only_when_configured():
    artifact = make_artifact(
        ["Service", "Value"],
        [[f"service-{index}", str(index)] for index in range(5)],
    )
    plan = make_plan(
        target_rows_per_chunk=2,
        maximum_rows_per_chunk=2,
        boundary_row_overlap=1,
    )

    result = CanonicalTableChunker(CharacterTokenCounter()).chunk(
        make_request(artifact),
        plan,
    )
    ranges = [
        (chunk.table_locator.row_start, chunk.table_locator.row_end)
        for chunk in result.chunks
        if chunk.metadata["table_chunk_role"] == "evidence" and chunk.table_locator is not None
    ]

    assert ranges == [(1, 2), (2, 3), (3, 4), (4, 5)]


def test_wide_table_column_groups_repeat_detected_key_columns():
    artifact = make_artifact(
        ["Service", "Owner", "Region", "CPU", "RAM", "Retry"],
        [["worker", "Ada", "APAC", "2", "4", "3"]],
    )
    plan = make_plan(maximum_columns_per_group=3)

    result = CanonicalTableChunker(CharacterTokenCounter()).chunk(
        make_request(artifact),
        plan,
    )
    evidence = [
        chunk for chunk in result.chunks if chunk.metadata["table_chunk_role"] == "evidence"
    ]

    assert result.classification.shape == TableShape.WIDE
    assert len(evidence) >= 2
    assert all(chunk.table_locator is not None for chunk in evidence)
    assert all(0 in chunk.table_locator.selected_column_indices for chunk in evidence)  # type: ignore[union-attr]
    assert all(0 in chunk.table_locator.key_column_indices for chunk in evidence)  # type: ignore[union-attr]
    selected = {
        index
        for chunk in evidence
        for index in chunk.table_locator.selected_column_indices  # type: ignore[union-attr]
    }
    assert selected == set(range(artifact.column_count))


def test_matrix_and_time_series_use_only_their_semantic_projections():
    matrix = make_artifact(
        ["Region", "Jan", "Feb"],
        [["APAC", "10", "12"], ["EMEA", "8", "9"]],
    )
    time_series = make_artifact(
        ["Timestamp", "Service", "Latency (ms)"],
        [
            ["2025-01-01T00:00:00Z", "api", "20"],
            ["2025-01-02T00:00:00Z", "api", "25"],
        ],
    )
    chunker = CanonicalTableChunker(CharacterTokenCounter())

    matrix_result = chunker.chunk(make_request(matrix), make_plan())
    time_result = chunker.chunk(make_request(time_series), make_plan())
    matrix_projections = {
        chunk.table_locator.projection_type
        for chunk in matrix_result.chunks
        if chunk.metadata["table_chunk_role"] == "evidence" and chunk.table_locator is not None
    }
    time_projections = {
        chunk.table_locator.projection_type
        for chunk in time_result.chunks
        if chunk.metadata["table_chunk_role"] == "evidence" and chunk.table_locator is not None
    }

    assert matrix_projections == {
        TableProjectionType.MATRIX_ROW,
        TableProjectionType.MATRIX_COLUMN,
    }
    assert time_projections == {TableProjectionType.TIME_WINDOW}
    assert all(
        value in "\n".join(chunk.content for chunk in time_result.chunks)
        for value in ("2025-01-01T00:00:00Z", "20", "25")
    )


def test_large_table_retains_route_and_schema_without_unbounded_dense_chunks():
    artifact = make_artifact(
        ["Service", "Description"],
        [[f"service-{index}", f"description {index}"] for index in range(9)],
    )

    result = CanonicalTableChunker(CharacterTokenCounter()).chunk(
        make_request(artifact),
        make_plan(),
    )

    assert result.classification.shape == TableShape.LARGE
    assert [chunk.metadata["table_chunk_role"] for chunk in result.chunks] == [
        "route",
        "schema",
    ]
    assert result.warnings[-1] == (
        "large table evidence disabled; route and schema chunks retained"
    )


def test_dense_chunk_cap_records_warning_without_failing_the_table():
    artifact = make_artifact(
        ["Service", "Description"],
        [[f"service-{index}", f"description-{index}"] for index in range(7)],
    )
    plan = make_plan(
        target_rows_per_chunk=1,
        maximum_rows_per_chunk=1,
        maximum_evidence_chunks_per_table=1,
    )

    result = CanonicalTableChunker(CharacterTokenCounter()).chunk(
        make_request(artifact),
        plan,
    )

    assert sum(chunk.metadata["table_chunk_role"] == "evidence" for chunk in result.chunks) == 1
    assert "table dense evidence safeguard reached" in result.warnings[-1]


def test_very_long_unicode_cell_is_fragmented_with_repeated_header_and_exact_locator():
    artifact = make_artifact(
        ["Service", "Description"],
        [["worker", "部署 " * 200]],
    )
    base = make_plan()
    plan = replace(
        base,
        hard_maximum_tokens=180,
        soft_maximum_tokens=170,
        target_tokens=160,
    )

    result = CanonicalTableChunker(CharacterTokenCounter()).chunk(
        make_request(artifact),
        plan,
    )
    evidence = [
        chunk for chunk in result.chunks if chunk.metadata["table_chunk_role"] == "evidence"
    ]

    assert len(evidence) > 1
    assert all(chunk.content.startswith("Service\tDescription\n") for chunk in evidence)
    assert all(chunk.token_count <= plan.hard_maximum_tokens for chunk in evidence)
    assert all(chunk.table_locator is not None for chunk in evidence)
    assert [chunk.table_locator.fragment_index for chunk in evidence] == list(  # type: ignore[union-attr]
        range(len(evidence))
    )
    assert all(
        (chunk.table_locator.row_start, chunk.table_locator.row_end) == (1, 1)  # type: ignore[union-attr]
        for chunk in evidence
    )


def test_table_chunk_identity_is_deterministic_and_strategy_sensitive():
    artifact = make_artifact(["Service", "CPU"], [["worker", "2"]])
    request = make_request(artifact)
    plan = make_plan()
    chunker = CanonicalTableChunker(CharacterTokenCounter())

    first = chunker.chunk(request, plan)
    repeated = chunker.chunk(request, plan)
    changed = chunker.chunk(request, replace(plan, strategy_version="table-v2"))

    assert [chunk.chunk_id for chunk in first.chunks] == [
        chunk.chunk_id for chunk in repeated.chunks
    ]
    assert [chunk.logical_chunk_id for chunk in first.chunks] == [
        chunk.logical_chunk_id for chunk in changed.chunks
    ]
    assert [chunk.chunk_id for chunk in first.chunks] != [
        chunk.chunk_id for chunk in changed.chunks
    ]
    assert all(report.provenance_score == 1 for report in first.quality.values())
