from __future__ import annotations

from datetime import datetime

import pytest
from chunking_test_fixtures import chunk_values, make_chunk
from pydantic import ValidationError

from harborrag_core.chunking import (
    ChunkHierarchy,
    ChunkKind,
    ChunkQuality,
    ChunkRecord,
    ChunkRelation,
    ChunkSecurity,
    ConnectorType,
    DocumentKind,
    RelationType,
    SourceAttribute,
    SourceLocator,
    TableChunkLocator,
)


def test_valid_evidence_chunk_preserves_distinct_text_representations() -> None:
    chunk = make_chunk()

    assert chunk.content == "Canonical evidence"
    assert chunk.contextual_prefix.startswith("Document:")
    assert chunk.embedding_text.endswith(chunk.content)
    assert "page-123" in chunk.search_text
    assert chunk.hierarchy.section_path == ("Architecture", "Chunking")
    assert chunk.relations == ()


def test_valid_jira_field_chunk_preserves_field_snapshot() -> None:
    chunk = make_chunk(
        connector_type=ConnectorType.JIRA,
        document_kind=DocumentKind.JIRA_ISSUE,
        chunk_kind=ChunkKind.JIRA_FIELD,
        source_attributes=(
            SourceAttribute(
                key="customfield_10042",
                display_name="Customer impact",
                value="High",
            ),
        ),
    )

    assert chunk.source_attributes[0].key == "customfield_10042"
    assert chunk.source_attributes[0].display_name == "Customer impact"


def test_valid_table_chunk_has_versioned_row_locator_and_key_columns() -> None:
    locator = TableChunkLocator(
        table_id="table:stable",
        table_version_id="table-version:7",
        row_start=4,
        row_end=9,
        column_count=4,
        key_column_indices=(0, 2),
        repeated_header_row_count=1,
    )

    chunk = make_chunk(chunk_kind=ChunkKind.TABLE, table_locator=locator)

    assert chunk.table_locator == locator
    assert chunk.table_locator.key_column_indices == (0, 2)


def test_valid_attachment_chunk_has_parent_relation_and_inherited_security() -> None:
    chunk = make_chunk(
        document_kind=DocumentKind.ATTACHMENT,
        relations=(
            ChunkRelation(
                relation_type=RelationType.ATTACHED_TO,
                target_id="document:parent-page",
            ),
        ),
        security=ChunkSecurity(
            permission_set_id="permission-set:parent",
            inherited_from_document_id="document:parent-page",
        ),
    )

    assert chunk.relations[0].relation_type == RelationType.ATTACHED_TO
    assert chunk.security.inherited_from_document_id == "document:parent-page"


def test_chunk_models_are_immutable_strict_and_serialize_enums_as_strings() -> None:
    chunk = make_chunk()

    with pytest.raises(ValidationError):
        chunk.content = "changed"
    with pytest.raises(ValidationError, match="Extra inputs are not permitted"):
        ChunkRecord(**chunk_values(unknown="value"))  # type: ignore[arg-type]

    serialized = chunk.model_dump(mode="json")
    assert serialized["connector_type"] == "confluence"
    assert serialized["document_kind"] == "confluence_page"
    assert serialized["chunk_kind"] == "evidence"
    assert "FrozenMetadata" in repr(chunk.metadata)


def test_chunk_metadata_is_recursively_immutable_and_json_safe() -> None:
    source = {"nested": {"values": [1, 2.5, True, None]}}
    chunk = make_chunk(metadata=source)
    source["nested"]["values"].append("changed")  # type: ignore[index,union-attr]

    assert chunk.model_dump(mode="json")["metadata"] == {"nested": {"values": [1, 2.5, True, None]}}
    with pytest.raises(TypeError):
        chunk.metadata["nested"]["values"] = ()  # type: ignore[index]

    for unsupported in ({1, 2}, object(), float("nan")):
        with pytest.raises(ValidationError, match="JSON-compatible|finite"):
            make_chunk(metadata={"unsupported": unsupported})


def test_optional_chunk_fields_may_be_absent() -> None:
    chunk = make_chunk(
        language=None,
        created_at=None,
        source_locator=SourceLocator(),
        hierarchy=ChunkHierarchy(),
        quality=ChunkQuality(),
    )

    assert chunk.language is None
    assert chunk.created_at is None
    assert chunk.table_locator is None


@pytest.mark.parametrize(
    ("changes", "message"),
    [
        ({"content": "   ", "embedding_text": "   "}, "content must be non-empty"),
        ({"token_count": -1}, "greater than or equal to 0"),
        ({"document_id": ""}, "at least 1 character"),
        ({"document_version_id": ""}, "at least 1 character"),
        (
            {
                "quality": ChunkQuality(score=0.5),
                "created_at": datetime(2026, 1, 1),  # noqa: DTZ001
            },
            "timezone",
        ),
    ],
)
def test_chunk_rejects_invalid_required_state(
    changes: dict[str, object],
    message: str,
) -> None:
    with pytest.raises(ValidationError, match=message):
        make_chunk(**changes)


def test_security_relation_and_hierarchy_validation_reject_sensitive_shape_errors() -> None:
    with pytest.raises(ValidationError, match="permission_set_id"):
        ChunkSecurity(permission_set_id=" ")

    relation = ChunkRelation(
        relation_type=RelationType.LINKS_TO,
        target_id="document:target",
    )
    with pytest.raises(ValidationError, match="relations must not contain duplicates"):
        make_chunk(relations=(relation, relation))

    with pytest.raises(ValidationError, match="self-reference"):
        make_chunk(
            hierarchy=ChunkHierarchy(parent_chunk_id="chunk:exact"),
        )
    with pytest.raises(ValidationError, match="self-reference"):
        make_chunk(
            hierarchy=ChunkHierarchy(parent_chunk_id="logical-chunk:stable"),
        )
    with pytest.raises(ValidationError, match="relations must not self-reference"):
        make_chunk(
            relations=(
                ChunkRelation(
                    relation_type=RelationType.LINKS_TO,
                    target_id="chunk:exact",
                ),
            )
        )


def test_quality_table_locator_and_source_attribute_validation_are_strict() -> None:
    with pytest.raises(ValidationError, match="less than or equal to 1"):
        ChunkQuality(score=1.01)
    with pytest.raises(ValidationError, match="row_end"):
        TableChunkLocator(
            table_id="table:1",
            table_version_id="table-version:1",
            row_start=3,
            row_end=2,
            column_count=2,
        )
    with pytest.raises(ValidationError, match="duplicates"):
        TableChunkLocator(
            table_id="table:1",
            table_version_id="table-version:1",
            row_start=0,
            row_end=1,
            column_count=2,
            key_column_indices=(0, 0),
        )
    with pytest.raises(ValidationError, match="must not exceed the table row range"):
        TableChunkLocator(
            table_id="table:1",
            table_version_id="table-version:1",
            row_start=4,
            row_end=5,
            column_count=2,
            repeated_header_row_count=3,
        )
    with pytest.raises(ValidationError, match="finite"):
        SourceAttribute(key="score", value=float("inf"))
    with pytest.raises(ValidationError, match="keys must be unique"):
        make_chunk(
            source_attributes=(
                SourceAttribute(key="field", value="one"),
                SourceAttribute(key="field", value="two"),
            )
        )


def test_table_locator_is_required_only_for_table_chunks() -> None:
    with pytest.raises(ValidationError, match="require table_locator"):
        make_chunk(chunk_kind=ChunkKind.TABLE)
    with pytest.raises(ValidationError, match="only valid for table chunks"):
        make_chunk(
            table_locator=TableChunkLocator(
                table_id="table:1",
                table_version_id="table-version:1",
                row_start=0,
                row_end=1,
                column_count=2,
            )
        )
