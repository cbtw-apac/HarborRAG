from __future__ import annotations

from dataclasses import replace

import pytest

from harborrag_core.schemas.ids import ChunkId
from harborrag_engine.ingestion.chunking import CanonicalTableChunker
from harborrag_engine.ingestion.chunking.table.errors import (
    InvalidTableLocatorError,
    TableChunkingError,
)
from harborrag_engine.ingestion.chunking.table.models import TableShape
from harborrag_engine.ingestion.chunking.table.validator import TableChunkValidator

from .table_test_fixtures import (
    CharacterTokenCounter,
    make_artifact,
    make_plan,
    make_request,
)

pytestmark = [pytest.mark.unit, pytest.mark.whitebox]


def _valid_table_result():
    artifact = make_artifact(
        ["Service", "CPU"],
        [["worker", "2"], ["api", "4"]],
    )
    plan = make_plan()
    result = CanonicalTableChunker(CharacterTokenCounter()).chunk(
        make_request(artifact),
        plan,
    )
    return artifact, plan, result


def test_validator_rejects_empty_and_duplicate_chunk_sets():
    artifact, plan, result = _valid_table_result()
    validator = TableChunkValidator(CharacterTokenCounter())

    with pytest.raises(TableChunkingError, match="no chunks"):
        validator.validate(artifact, result.classification, (), plan)
    with pytest.raises(TableChunkingError, match="duplicate"):
        validator.validate(
            artifact,
            result.classification,
            (result.chunks[0], result.chunks[0]),
            plan,
        )


@pytest.mark.parametrize(
    ("record_change", "locator_change", "error_type", "message"),
    [
        ({"table_locator": None}, {}, InvalidTableLocatorError, "missing"),
        ({}, {"table_id": "another"}, InvalidTableLocatorError, "different table"),
        (
            {},
            {"table_version_id": "another-version"},
            InvalidTableLocatorError,
            "different table version",
        ),
        ({}, {"row_end": 99}, InvalidTableLocatorError, "row range"),
        (
            {},
            {"selected_column_indices": (0, 99)},
            InvalidTableLocatorError,
            "selected column",
        ),
        (
            {},
            {"selected_column_indices": (1,), "key_column_indices": (0,)},
            InvalidTableLocatorError,
            "key columns",
        ),
        ({"content": ""}, {}, TableChunkingError, "content is empty"),
        ({"token_count": 0}, {}, TableChunkingError, "inconsistent"),
    ],
)
def test_validator_rejects_invalid_record_contracts(
    record_change,
    locator_change,
    error_type,
    message,
):
    artifact, plan, result = _valid_table_result()
    record = result.chunks[1]
    if locator_change:
        record_change["table_locator"] = record.table_locator.model_copy(  # type: ignore[union-attr]
            update=locator_change
        )
    corrupted = record.model_copy(update=record_change)

    with pytest.raises(error_type, match=message):
        TableChunkValidator(CharacterTokenCounter())._validate_record(
            artifact,
            result.classification,
            corrupted,
            plan,
        )


def test_validator_rejects_token_limit_and_lost_hierarchy_provenance():
    artifact, plan, result = _valid_table_result()
    record = result.chunks[1]
    validator = TableChunkValidator(CharacterTokenCounter())
    oversized_text = "x" * (plan.hard_maximum_tokens + 1)
    oversized = record.model_copy(
        update={
            "embedding_text": oversized_text,
            "token_count": len(oversized_text),
        }
    )
    wrong_section = record.model_copy(
        update={"hierarchy": record.hierarchy.model_copy(update={"section_path": ("Wrong",)})}
    )
    wrong_tab = record.model_copy(
        update={
            "table_locator": record.table_locator.model_copy(  # type: ignore[union-attr]
                update={"tab_path": ("Wrong",)}
            )
        }
    )

    with pytest.raises(TableChunkingError, match="hard token"):
        validator._validate_record(
            artifact,
            result.classification,
            oversized,
            plan,
        )
    with pytest.raises(TableChunkingError, match="section provenance"):
        validator._validate_record(
            artifact,
            result.classification,
            wrong_section,
            plan,
        )
    with pytest.raises(TableChunkingError, match="tab provenance"):
        validator._validate_record(
            artifact,
            result.classification,
            wrong_tab,
            plan,
        )


def test_validator_reports_incomplete_headers_and_lower_self_containment():
    artifact, plan, result = _valid_table_result()
    record = result.chunks[1]
    content = "not-the-selected-headers\nworker\t2"
    embedding = f"{record.contextual_prefix}\n\n{content}"
    changed = record.model_copy(
        update={
            "content": content,
            "embedding_text": embedding,
            "token_count": len(embedding),
        }
    )
    classification = replace(result.classification, key_column_indices=())

    report = TableChunkValidator(CharacterTokenCounter())._validate_record(
        artifact,
        classification,
        changed,
        plan,
    )

    assert report.header_completeness_score == 0
    assert report.self_containment_score == 0.75
    assert report.warnings == ("selected headers are not repeated",)


def test_validator_requires_route_schema_and_complete_nonduplicated_coverage():
    artifact, plan, result = _valid_table_result()
    validator = TableChunkValidator(CharacterTokenCounter())
    route, evidence = result.chunks

    with pytest.raises(TableChunkingError, match="route"):
        validator._validate_route_and_schema(result.classification, (evidence,))
    with pytest.raises(TableChunkingError, match="schema"):
        validator._validate_route_and_schema(
            replace(result.classification, shape=TableShape.WIDE),
            (route,),
        )
    with pytest.raises(TableChunkingError, match="cover every"):
        validator._validate_coverage(
            artifact,
            result.classification,
            (route,),
            plan,
        )

    duplicate = evidence.model_copy(
        update={
            "chunk_id": ChunkId("different-chunk"),
            "logical_chunk_id": ChunkId("different-logical-chunk"),
        }
    )
    with pytest.raises(TableChunkingError, match="duplication"):
        validator._validate_coverage(
            artifact,
            result.classification,
            (route, evidence, duplicate),
            plan,
        )


def test_large_table_without_evidence_is_an_explicit_valid_coverage_policy():
    artifact = make_artifact(
        ["Service", "Description"],
        [[f"service-{index}", f"description-{index}"] for index in range(9)],
    )
    plan = make_plan()
    result = CanonicalTableChunker(CharacterTokenCounter()).chunk(
        make_request(artifact),
        plan,
    )

    TableChunkValidator(CharacterTokenCounter())._validate_coverage(
        artifact,
        result.classification,
        result.chunks,
        plan,
    )
