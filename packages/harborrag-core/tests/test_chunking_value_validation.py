from __future__ import annotations

import pytest
from chunking_test_fixtures import make_chunk
from pydantic import ValidationError

from harborrag_core.chunking import (
    ChunkContainer,
    ChunkHierarchy,
    ChunkQuality,
    ChunkRelation,
    ChunkSecurity,
    CitationLocator,
    ConnectorType,
    ContainerKind,
    DocumentKind,
    SourceAttribute,
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
    ],
)
def test_structured_value_objects_reject_blank_or_inconsistent_values(
    model: type[object],
    values: dict[str, object],
    message: str,
) -> None:
    with pytest.raises(ValidationError, match=message):
        model(**values)  # type: ignore[operator]


def test_citation_locator_security_and_attribute_edge_validation() -> None:
    invalid_locators = (
        {"start_offset": 1},
        {"start_offset": 2, "end_offset": 1},
        {"uri": " "},
        {"source_element_ids": ("",)},
    )
    for values in invalid_locators:
        with pytest.raises(ValidationError):
            CitationLocator(**values)  # type: ignore[arg-type]

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
        ({"embedding_text": " "}, "embedding_text"),
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


def test_container_title_is_optional() -> None:
    assert (
        ChunkContainer(
            container_id="section:1",
            kind=ContainerKind.SECTION,
            ordinal=0,
        ).title
        is None
    )
