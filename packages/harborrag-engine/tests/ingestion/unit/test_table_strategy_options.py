from __future__ import annotations

import pytest

from harborrag_core.chunking import TableProjectionType
from harborrag_engine.ingestion.chunking import (
    CanonicalTableChunker,
    MatrixProjectionMode,
    TableShape,
)

from .table_test_fixtures import (
    CharacterTokenCounter,
    make_artifact,
    make_plan,
    make_request,
)

pytestmark = [pytest.mark.unit, pytest.mark.blackbox]


def test_configured_wide_table_key_is_repeated_in_every_column_group():
    artifact = make_artifact(
        ["Service", "Owner", "Region", "CPU", "RAM", "Retry"],
        [["worker", "Ada", "APAC", "2", "4", "3"]],
    )

    result = CanonicalTableChunker(CharacterTokenCounter()).chunk(
        make_request(artifact),
        make_plan(
            explicit_key_columns=("Owner",),
            maximum_columns_per_group=3,
        ),
    )
    evidence = [
        chunk for chunk in result.chunks if chunk.metadata["table_chunk_role"] == "evidence"
    ]

    assert all(chunk.table_locator is not None for chunk in evidence)
    assert all(1 in chunk.table_locator.key_column_indices for chunk in evidence)  # type: ignore[union-attr]
    assert all(1 in chunk.table_locator.selected_column_indices for chunk in evidence)  # type: ignore[union-attr]


@pytest.mark.parametrize(
    ("mode", "expected"),
    [
        (MatrixProjectionMode.ROWS, {TableProjectionType.MATRIX_ROW}),
        (MatrixProjectionMode.COLUMNS, {TableProjectionType.MATRIX_COLUMN}),
    ],
)
def test_matrix_projection_mode_can_select_one_semantic_orientation(mode, expected):
    artifact = make_artifact(
        ["Region", "Jan", "Feb"],
        [["APAC", "10", "12"], ["EMEA", "8", "9"]],
    )

    result = CanonicalTableChunker(CharacterTokenCounter()).chunk(
        make_request(artifact),
        make_plan(matrix_projection_mode=mode),
    )
    projections = {
        chunk.table_locator.projection_type
        for chunk in result.chunks
        if chunk.metadata["table_chunk_role"] == "evidence" and chunk.table_locator is not None
    }

    assert projections == expected


def test_large_table_can_emit_selective_descriptive_evidence_when_enabled():
    artifact = make_artifact(
        ["Service", "Description", "Count"],
        [
            [
                f"service-{index}",
                f"Detailed operational description for service number {index}",
                str(index),
            ]
            for index in range(9)
        ],
    )

    result = CanonicalTableChunker(CharacterTokenCounter()).chunk(
        make_request(artifact),
        make_plan(
            large_table_evidence_enabled=True,
            maximum_evidence_chunks_per_table=3,
        ),
    )
    evidence = [
        chunk for chunk in result.chunks if chunk.metadata["table_chunk_role"] == "evidence"
    ]

    assert result.classification.shape == TableShape.LARGE
    assert evidence
    assert all(chunk.table_locator is not None for chunk in evidence)
    assert all(1 in chunk.table_locator.selected_column_indices for chunk in evidence)  # type: ignore[union-attr]


def test_dense_token_cap_omits_remaining_evidence_but_retains_route():
    artifact = make_artifact(["Service", "CPU"], [["worker", "2"]])

    result = CanonicalTableChunker(CharacterTokenCounter()).chunk(
        make_request(artifact),
        make_plan(maximum_dense_table_tokens=1),
    )

    assert [chunk.metadata["table_chunk_role"] for chunk in result.chunks] == ["route"]
    assert result.warnings[-1] == (
        "table dense evidence safeguard reached; remaining evidence omitted"
    )


def test_header_only_table_retains_route_and_records_no_evidence_warning():
    artifact = make_artifact(["Service", "CPU"], [])

    result = CanonicalTableChunker(CharacterTokenCounter()).chunk(
        make_request(artifact),
        make_plan(),
    )

    assert [chunk.metadata["table_chunk_role"] for chunk in result.chunks] == ["route"]
    assert result.warnings == ("table contains no non-header rows for evidence chunking",)


def test_table_context_can_use_tab_provenance_without_a_section():
    artifact = make_artifact(["Service", "CPU"], [["worker", "2"]]).model_copy(
        update={"tab_path": ("Production",), "section_path": ()},
    )

    result = CanonicalTableChunker(CharacterTokenCounter()).chunk(
        make_request(artifact),
        make_plan(),
    )

    assert all("Tab: Production" in chunk.embedding_text for chunk in result.chunks)
    assert all("Section:" not in chunk.embedding_text for chunk in result.chunks)
