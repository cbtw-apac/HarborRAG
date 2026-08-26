"""Guards on pre-packing chunk units and the source-specific record rules.

`ChunkUnit`/`ChunkCandidate` are the intermediate shapes a strategy builds
before stable identity is assigned, and the connector-specific validators are
what stop Jira and Confluence chunks from losing the metadata retrieval later
depends on. Both were unexercised.
"""

from __future__ import annotations

import pytest

from harborrag_core.chunking import (
    ChunkHierarchy,
    ChunkKind,
    ChunkRecord,
    ChunkSecurity,
    CitationLocator,
    ConnectorType,
    DocumentKind,
    RecordKind,
)
from harborrag_core.contracts.chunking import SourceSpan, SplitBoundaryKind
from harborrag_engine.ingestion.chunking.records import (
    CanonicalChunkFactory,
    ChunkContextBuilder,
    ChunkValidator,
)
from harborrag_engine.ingestion.chunking.schemas import ChunkCandidate, ChunkUnit

SPAN = SourceSpan(start_offset=0, end_offset=10, element_ids=("element-1",))


def _unit(**changes: object) -> ChunkUnit:
    fields: dict[str, object] = {
        "anchor": "anchor-1",
        "content": "body text",
        "token_count": 2,
        "role": "body",
        "structural_path": ("Guide",),
        "source_span": SPAN,
        "merge_group": "group-1",
    }
    fields.update(changes)
    return ChunkUnit(**fields)  # type: ignore[arg-type]


def _candidate(**changes: object) -> ChunkCandidate:
    fields: dict[str, object] = {
        "anchor": "anchor-1",
        "content": "body text",
        "token_count": 2,
        "role": "body",
        "structural_path": ("Guide",),
        "source_span": SPAN,
        "units": (_unit(),),
        "boundary_kind": SplitBoundaryKind.PARAGRAPH,
    }
    fields.update(changes)
    return ChunkCandidate(**fields)  # type: ignore[arg-type]


def _record(role: str = "body", **metadata: object) -> ChunkRecord:
    chunk_kind = ChunkKind.COMMENT if role == "jira.comment" else ChunkKind.TEXT
    return ChunkRecord(
        strategy_version="strategy-v1",
        chunk_id="chunk:1",
        logical_chunk_id="logical-chunk:1",
        content_hash="hash-1",
        connector_type=ConnectorType.LOCAL,
        document_kind=DocumentKind.LOCAL_FILE,
        record_kind=RecordKind.EVIDENCE,
        chunk_kind=chunk_kind,
        tenant_id="tenant-1",
        connection_id="local-test",
        source_scope_id="docs",
        source_item_id="guide.md",
        source_version="source-v1",
        document_id="document-1",
        document_version_id="version-1",
        ordinal=0,
        content="body text",
        embedding_text="Document: Guide\n\nbody text",
        search_text="Guide\nbody text",
        token_count=2,
        hierarchy=ChunkHierarchy(section_path=("Guide",)),
        citation_locator=CitationLocator(start_offset=0, end_offset=9),
        security=ChunkSecurity(permission_set_id="permission-set:test"),
        metadata=metadata,
    )


def test_canonical_record_factory_maps_remaining_roles_and_parent_titles() -> None:
    assert CanonicalChunkFactory.kind_for_role("event") == ChunkKind.EVENT
    assert CanonicalChunkFactory.kind_for_role("jira.field") == ChunkKind.JIRA_FIELD
    assert CanonicalChunkFactory.kind_for_role("confluence.table") == ChunkKind.TABLE
    assert CanonicalChunkFactory.kind_for_role("prevent") == ChunkKind.TEXT
    assert CanonicalChunkFactory.kind_for_role("decode") == ChunkKind.TEXT
    assert ChunkContextBuilder.parent_title({"ancestor_titles": "not-a-sequence"}) is None
    assert ChunkContextBuilder.parent_title({"ancestor_titles": [None, " Parent "]}) == ("Parent")


# --------------------------------------------------------------------------
# ChunkUnit
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("changes", "message"),
    [
        ({"anchor": "  "}, "anchor and merge_group must be non-empty"),
        ({"merge_group": "  "}, "anchor and merge_group must be non-empty"),
        ({"role": "  "}, "role and content must be non-empty"),
        ({"content": ""}, "role and content must be non-empty"),
        ({"content": "   "}, "role and content must be non-empty"),
        ({"token_count": 0}, "token_count must be positive"),
        ({"structural_path": ("Guide", " ")}, "structural_path parts must be non-empty"),
    ],
)
def test_chunk_unit_rejects_invalid_values(changes: dict[str, object], message: str) -> None:
    with pytest.raises(ValueError, match=message):
        _unit(**changes)


def test_chunk_unit_freezes_its_metadata() -> None:
    unit = _unit(metadata={"key": "value"})

    assert unit.metadata["key"] == "value"
    with pytest.raises(TypeError):
        unit.metadata["key"] = "other"  # type: ignore[index]


def test_chunk_unit_defaults_are_conservative() -> None:
    unit = _unit()

    assert unit.boundary_kind is SplitBoundaryKind.PARAGRAPH
    assert unit.hard_boundary_before is False
    assert unit.hard_boundary_after is False
    assert unit.forced_split is False


# --------------------------------------------------------------------------
# ChunkCandidate
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("changes", "message"),
    [
        ({"anchor": "  "}, "anchor and content must be non-empty"),
        ({"content": "   "}, "anchor and content must be non-empty"),
        ({"role": " "}, "role and units must be non-empty"),
        ({"units": ()}, "role and units must be non-empty"),
        ({"token_count": 0}, "counts must be positive/non-negative"),
        ({"local_part_index": -1}, "counts must be positive/non-negative"),
    ],
)
def test_chunk_candidate_rejects_invalid_values(
    changes: dict[str, object],
    message: str,
) -> None:
    with pytest.raises(ValueError, match=message):
        _candidate(**changes)


def test_chunk_candidate_freezes_its_metadata() -> None:
    candidate = _candidate(metadata={"key": "value"})

    with pytest.raises(TypeError):
        candidate.metadata["key"] = "other"  # type: ignore[index]


# --------------------------------------------------------------------------
# Source-specific record rules
# --------------------------------------------------------------------------


def _source_errors(record: ChunkRecord, strategy: str) -> list[str]:
    errors: list[str] = []
    from harborrag_engine.ingestion.chunking.sources.validation import (
        validate_confluence_chunk,
        validate_jira_chunk,
    )

    validators = {
        "confluence": validate_confluence_chunk,
        "jira": validate_jira_chunk,
    }
    validator = validators.get(strategy)
    if validator is not None:
        validator(record, errors, "chunk[0]")
    return errors


def test_jira_chunks_require_an_issue_key() -> None:
    assert any("requires issue_key" in e for e in _source_errors(_record(), "jira"))
    assert any("requires issue_key" in e for e in _source_errors(_record(issue_key="  "), "jira"))
    assert _source_errors(_record(issue_key="PROJ-1"), "jira") == []


def test_jira_comment_chunks_additionally_require_a_comment_id() -> None:
    comment = _record("jira.comment", issue_key="PROJ-1")

    assert any("requires comment_id" in e for e in _source_errors(comment, "jira"))

    with_id = _record("jira.comment", issue_key="PROJ-1", comment_id=42)
    assert _source_errors(with_id, "jira") == []


def test_confluence_chunks_require_a_page_id() -> None:
    assert any("requires page_id" in e for e in _source_errors(_record(), "confluence"))
    assert _source_errors(_record(page_id=1234), "confluence") == []
    assert _source_errors(_record(page_id="1234"), "confluence") == []


def test_an_unknown_strategy_adds_no_source_specific_requirements() -> None:
    assert _source_errors(_record(), "document") == []


# --------------------------------------------------------------------------
# Monotonic source locations
# --------------------------------------------------------------------------


def _location_errors(spans: list[CitationLocator]) -> list[str]:
    errors: list[str] = []
    previous: dict[str, tuple[int, int, int]] = {}
    for index, span in enumerate(spans):
        record = _record().model_copy(update={"citation_locator": span})
        ChunkValidator._validate_locations(record, previous, errors, f"chunk[{index}]")
    return errors


def test_forward_only_source_locations_are_accepted() -> None:
    spans = [
        CitationLocator(start_offset=0, end_offset=10, source_element_ids=("e1",)),
        CitationLocator(start_offset=10, end_offset=20, source_element_ids=("e1",)),
    ]

    assert _location_errors(spans) == []


def test_a_backward_source_location_is_reported() -> None:
    spans = [
        CitationLocator(start_offset=10, end_offset=20, source_element_ids=("e1",)),
        CitationLocator(start_offset=0, end_offset=5, source_element_ids=("e1",)),
    ]

    assert any("source location moved backward" in e for e in _location_errors(spans))


def test_a_record_with_an_empty_citation_locator_is_accepted() -> None:
    errors: list[str] = []
    record = _record().model_copy(update={"citation_locator": CitationLocator()})

    ChunkValidator._validate_locations(record, {}, errors, "chunk[0]")

    assert errors == []
