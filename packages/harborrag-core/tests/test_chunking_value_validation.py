from __future__ import annotations

import pytest
from chunking_test_fixtures import make_chunk
from pydantic import ValidationError

from harborrag_core.chunking import (
    ChunkContainer,
    ChunkContext,
    ChunkHierarchy,
    ChunkKind,
    ChunkQuality,
    ChunkRecord,
    ChunkRelation,
    ChunkSecurity,
    ChunkValidationError,
    ConnectorType,
    ContainerKind,
    DocumentKind,
    SourceAttribute,
    SourceLocator,
    TableChunkLocator,
)


@pytest.mark.parametrize(
    ("model", "values", "message"),
    [
        (
            ChunkContainer,
            {"container_id": "c1", "kind": "section", "ordinal": 0, "title": " "},
            "title",
        ),
        (
            ChunkContainer,
            {"container_id": " ", "kind": "section", "ordinal": 0},
            "container_id",
        ),
        (ChunkHierarchy, {"section_path": (" ",)}, "section path"),
        (ChunkHierarchy, {"document_title": " "}, "document_title"),
        (ChunkHierarchy, {"parent_title": " "}, "parent_title"),
        (ChunkHierarchy, {"ancestry": ("a", "a")}, "duplicates"),
        (ChunkHierarchy, {"section_id": "a", "ancestry": ("a",)}, "section_id"),
        (
            ChunkHierarchy,
            {"parent_section_id": "b", "ancestry": ("a",)},
            "last ancestry",
        ),
        (
            ChunkHierarchy,
            {"previous_chunk_id": "same", "next_chunk_id": "same"},
            "must differ",
        ),
        (
            ChunkHierarchy,
            {
                "containers": (
                    {"container_id": "same", "kind": "section", "ordinal": 0},
                    {"container_id": "same", "kind": "panel", "ordinal": 1},
                )
            },
            "container IDs must be unique",
        ),
        (
            ChunkHierarchy,
            {
                "containers": (
                    {"container_id": "first", "kind": "section", "ordinal": 0},
                    {"container_id": "second", "kind": "panel", "ordinal": 0},
                )
            },
            "container ordinals must be unique",
        ),
        (
            ChunkHierarchy,
            {
                "containers": (
                    {"container_id": "first", "kind": "section", "ordinal": 2},
                    {"container_id": "second", "kind": "panel", "ordinal": 1},
                )
            },
            "ordered by ordinal",
        ),
        (ChunkRelation, {"relation_type": "links_to", "target_id": " "}, "target"),
        (
            ChunkRelation,
            {"relation_type": "links_to", "target_id": "target", "target_version_id": " "},
            "target",
        ),
        (ChunkQuality, {"issues": (" ",)}, "issues"),
        (ChunkQuality, {"issues": ("same", "same")}, "duplicates"),
        (ChunkContext, {"title": " "}, "title"),
        (ChunkContext, {"parent_title": " "}, "parent_title"),
        (ChunkContext, {"structural_path": (" ",)}, "structural_path"),
    ],
)
def test_structured_value_objects_reject_blank_or_inconsistent_values(
    model: type[object],
    values: dict[str, object],
    message: str,
) -> None:
    with pytest.raises(ValidationError, match=message):
        model(**values)  # type: ignore[operator]


def test_source_locator_security_and_attribute_edge_validation() -> None:
    invalid_locators = (
        {"start_offset": 1},
        {"start_offset": 2, "end_offset": 1},
        {"uri": " "},
        {"source_element_ids": ("",)},
    )
    for values in invalid_locators:
        with pytest.raises(ValidationError):
            SourceLocator(**values)  # type: ignore[arg-type]

    with pytest.raises(ValidationError, match="visibility"):
        ChunkSecurity(permission_set_id="permissions", visibility=" ")
    with pytest.raises(ValidationError, match="key"):
        SourceAttribute(key=" ", value="value")
    with pytest.raises(ValidationError, match="display_name"):
        SourceAttribute(key="field", value="value", display_name=" ")
    with pytest.raises(ValidationError, match="within column_count"):
        TableChunkLocator(
            table_id="table:1",
            table_version_id="table-version:1",
            row_start=0,
            row_end=1,
            column_count=2,
            key_column_indices=(2,),
        )


@pytest.mark.parametrize(
    ("changes", "message"),
    [
        ({"embedding_text": "wrong"}, "embedding_text"),
        (
            {
                "connector_type": ConnectorType.LOCAL,
                "document_kind": DocumentKind.CONFLUENCE_PAGE,
            },
            "confluence_page",
        ),
        (
            {
                "connector_type": ConnectorType.LOCAL,
                "document_kind": DocumentKind.JIRA_ISSUE,
            },
            "jira_issue",
        ),
        (
            {
                "connector_type": ConnectorType.JIRA,
                "document_kind": DocumentKind.LOCAL_FILE,
            },
            "local_file",
        ),
    ],
)
def test_chunk_rejects_cross_field_contract_mismatches(
    changes: dict[str, object],
    message: str,
) -> None:
    with pytest.raises(ValidationError, match=message):
        make_chunk(**changes)


@pytest.mark.parametrize(
    ("source_kind", "role", "connector", "document_kind", "chunk_kind"),
    [
        (
            "confluence",
            "code",
            ConnectorType.CONFLUENCE,
            DocumentKind.CONFLUENCE_PAGE,
            ChunkKind.CODE,
        ),
        (
            "jira",
            "jira.comment",
            ConnectorType.JIRA,
            DocumentKind.JIRA_ISSUE,
            ChunkKind.COMMENT,
        ),
        (
            "local",
            "event",
            ConnectorType.LOCAL,
            DocumentKind.LOCAL_FILE,
            ChunkKind.EVENT,
        ),
        (
            "jira",
            "jira.field",
            ConnectorType.JIRA,
            DocumentKind.JIRA_ISSUE,
            ChunkKind.JIRA_FIELD,
        ),
        (
            "confluence",
            "attachment",
            ConnectorType.CONFLUENCE,
            DocumentKind.ATTACHMENT,
            ChunkKind.EVIDENCE,
        ),
        (
            "confluence",
            "confluence.table",
            ConnectorType.CONFLUENCE,
            DocumentKind.CONFLUENCE_PAGE,
            ChunkKind.TABLE,
        ),
        (
            "local",
            "prevent",
            ConnectorType.LOCAL,
            DocumentKind.LOCAL_FILE,
            ChunkKind.EVIDENCE,
        ),
    ],
)
def test_explicit_legacy_migration_maps_existing_roles(
    source_kind: str,
    role: str,
    connector: ConnectorType,
    document_kind: DocumentKind,
    chunk_kind: ChunkKind,
) -> None:
    chunk = ChunkRecord.from_legacy(
        logical_chunk_id="logical",
        chunk_revision_id="revision",
        tenant_id="tenant",
        document_id="document",
        document_version_id="document-version",
        artifact_id="artifact",
        artifact_revision_id="artifact-version",
        ordinal=0,
        role=role,
        content="content",
        content_hash="hash",
        metadata={"source_kind": source_kind, "chunker_version": "7"},
    )

    assert chunk.connector_type == connector
    assert chunk.document_kind == document_kind
    assert chunk.chunk_kind == chunk_kind
    assert chunk.strategy_version == "7"


@pytest.mark.parametrize(
    ("metadata", "expected_start", "expected_end"),
    [
        (
            {
                "table_id": "table:stable",
                "table_version_id": "table-version:stable",
                "row_start": "invalid",
                "row_end": "invalid",
            },
            0,
            1,
        ),
        ({"row_start": 10}, 10, 11),
        ({"row_start": 10, "row_end": 9}, 10, 10),
        ({"row_start": True, "row_end": False}, 0, 1),
    ],
)
def test_legacy_table_migration_builds_a_structured_locator(
    metadata: dict[str, object],
    expected_start: int,
    expected_end: int,
) -> None:
    chunk = ChunkRecord.from_legacy(
        logical_chunk_id="logical",
        chunk_revision_id="revision",
        tenant_id="tenant",
        document_id="document",
        document_version_id="document-version",
        artifact_id="artifact",
        artifact_revision_id="artifact-version",
        ordinal=0,
        role="table",
        content="A\tB\n1\t2",
        content_hash="hash",
        metadata=metadata,
    )

    assert chunk.table_locator is not None
    assert chunk.table_locator.row_start == expected_start
    assert chunk.table_locator.row_end == expected_end


def test_payload_migration_accepts_current_and_legacy_storage_shapes() -> None:
    current = make_chunk(metadata={"source": "canonical"})
    assert ChunkRecord.from_legacy_payload(current.model_dump()) == current

    legacy = ChunkRecord.from_legacy_payload(
        {
            "logical_chunk_id": "logical",
            "chunk_revision_id": "revision",
            "tenant_id": "tenant",
            "document_id": "document",
            "document_version_id": "document-version",
            "artifact_id": "artifact",
            "artifact_revision_id": "artifact-version",
            "ordinal": 0,
            "role": "jira.comment",
            "content": "content",
            "content_hash": "hash",
            "source_span": {"start_offset": 0, "end_offset": 7},
            "context": {"title": "Issue"},
            "metadata": {"source_kind": "jira"},
        }
    )

    assert legacy.chunk_kind == ChunkKind.COMMENT
    assert legacy.source_locator.start_offset == 0
    assert legacy.hierarchy.document_title == "Issue"
    with pytest.raises(ChunkValidationError, match="metadata must be a mapping"):
        ChunkRecord.from_legacy_payload({"metadata": "invalid"})


def test_canonical_role_compatibility_falls_back_to_chunk_kind() -> None:
    chunk = make_chunk(metadata={})

    assert chunk.role == ChunkKind.EVIDENCE.value
    assert (
        ChunkContainer(
            container_id="section:1",
            kind=ContainerKind.SECTION,
            ordinal=0,
        ).title
        is None
    )
