"""Guards on pre-packing chunk units and the source-specific record rules.

`ChunkUnit`/`ChunkCandidate` are the intermediate shapes a strategy builds
before stable identity is assigned, and the connector-specific validators are
what stop a Jira/Confluence/JSON chunk from losing the metadata retrieval later
depends on. Both were unexercised.
"""

from __future__ import annotations

import pytest

from harborrag_core.chunking import ChunkKind
from harborrag_core.contracts.chunking import SourceSpan, SplitBoundaryKind
from harborrag_core.schemas.documents import ChunkContext, ChunkRecord, ChunkSourceSpan
from harborrag_engine.ingestion.chunking.record_factory import CanonicalChunkFactory
from harborrag_engine.ingestion.chunking.schemas import ChunkCandidate, ChunkUnit
from harborrag_engine.ingestion.chunking.validation import ChunkValidator

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
    return ChunkRecord.from_legacy(
        tenant_id="tenant-1",
        document_id="document-1",
        document_version_id="version-1",
        artifact_id="artifact-1",
        artifact_revision_id="revision-1",
        logical_chunk_id="logical-1",
        chunk_revision_id="chunk-revision-1",
        ordinal=0,
        role=role,
        content="body text",
        content_hash="hash-1",
        token_count=2,
        context=ChunkContext(structural_path=("Guide",)),
        source_span=ChunkSourceSpan(start_offset=0, end_offset=9),
        metadata=metadata,
    )


def test_canonical_record_factory_maps_remaining_roles_and_parent_titles() -> None:
    assert CanonicalChunkFactory.kind_for_role("event") == ChunkKind.EVENT
    assert CanonicalChunkFactory.kind_for_role("jira.field") == ChunkKind.JIRA_FIELD
    assert CanonicalChunkFactory._parent_title({"ancestor_titles": "not-a-sequence"}) is None
    assert CanonicalChunkFactory._parent_title({"ancestor_titles": [None, " Parent "]}) == (
        "Parent"
    )


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
    ChunkValidator._validate_source_specific(record, strategy, errors, "chunk[0]")
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


def test_json_chunks_require_a_json_path() -> None:
    assert any("requires json_path" in e for e in _source_errors(_record(), "json"))
    assert _source_errors(_record(json_path="$.items[0]"), "json") == []


def test_an_unknown_strategy_adds_no_source_specific_requirements() -> None:
    assert _source_errors(_record(), "document") == []


# --------------------------------------------------------------------------
# Monotonic source locations
# --------------------------------------------------------------------------


def _location_errors(spans: list[ChunkSourceSpan]) -> list[str]:
    errors: list[str] = []
    previous: dict[str, tuple[int, int, int]] = {}
    for index, span in enumerate(spans):
        record = _record().model_copy(update={"source_locator": span})
        ChunkValidator._validate_locations(record, previous, errors, f"chunk[{index}]")
    return errors


def test_forward_only_source_locations_are_accepted() -> None:
    spans = [
        ChunkSourceSpan(start_offset=0, end_offset=10, source_element_ids=("e1",)),
        ChunkSourceSpan(start_offset=10, end_offset=20, source_element_ids=("e1",)),
    ]

    assert _location_errors(spans) == []


def test_a_backward_source_location_is_reported() -> None:
    spans = [
        ChunkSourceSpan(start_offset=10, end_offset=20, source_element_ids=("e1",)),
        ChunkSourceSpan(start_offset=0, end_offset=5, source_element_ids=("e1",)),
    ]

    assert any("source location moved backward" in e for e in _location_errors(spans))


def test_a_record_with_an_empty_source_locator_is_accepted() -> None:
    errors: list[str] = []
    record = _record().model_copy(update={"source_locator": ChunkSourceSpan()})

    ChunkValidator._validate_locations(record, {}, errors, "chunk[0]")

    assert errors == []
