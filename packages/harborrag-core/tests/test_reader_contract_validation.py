"""Reader request contracts reject out-of-bounds input before any backend sees it."""

from __future__ import annotations

from collections.abc import Callable

import pytest

from harborrag_core.contracts.reader import (
    DOCUMENT_CONTEXT_CHUNK_FIELDS,
    DOCUMENT_CONTEXT_LIMIT,
    DOCUMENT_LIST_LIMIT,
    ENTITY_FIND_LIMIT,
    EVIDENCE_BATCH_LIMIT,
    EVIDENCE_ITEM_FIELDS,
    SOURCE_LIST_LIMIT,
    DocumentContextRequest,
    DocumentListRequest,
    EntityFindRequest,
    EntityResolveRequest,
    EvidenceFetchRequest,
    EvidenceReadRequest,
    EvidenceReadSelector,
    RelationSearchRequest,
    RetrievalLane,
    RetrievalMode,
    RetrievalRequest,
    SemanticPathRequest,
    SourceListRequest,
)
from harborrag_core.security import AccessContext

ACCESS = AccessContext(principal_id="reader-1", tenant_id="tenant-a")


def _selectors(*chunk_ids: str) -> tuple[EvidenceReadSelector, ...]:
    return tuple(EvidenceReadSelector(item) for item in chunk_ids)


INVALID_REQUESTS: list[tuple[Callable[[], object], str]] = [
    # Evidence selectors and batches.
    (lambda: EvidenceReadSelector("  "), "evidence chunk ID must be non-empty"),
    (
        lambda: EvidenceReadSelector("chunk-1", expected_document_id=" "),
        "expected document ID must be non-empty when provided",
    ),
    (
        lambda: EvidenceReadSelector("chunk-1", expected_document_version_id=""),
        "expected document version ID must be non-empty when provided",
    ),
    (
        lambda: EvidenceReadRequest(ACCESS, ()),
        f"evidence read requires between 1 and {EVIDENCE_BATCH_LIMIT} items",
    ),
    (
        lambda: EvidenceReadRequest(
            ACCESS, _selectors(*(f"chunk-{index}" for index in range(EVIDENCE_BATCH_LIMIT + 1)))
        ),
        f"evidence read requires between 1 and {EVIDENCE_BATCH_LIMIT} items",
    ),
    (
        lambda: EvidenceReadRequest(ACCESS, _selectors("chunk-1", "chunk-1")),
        "evidence read chunk IDs must be unique",
    ),
    # Document context windows.
    (lambda: DocumentContextRequest(ACCESS, " "), "context document ID must be non-empty"),
    (
        lambda: DocumentContextRequest(ACCESS, "doc-1", expected_document_version_id=" "),
        "expected document version ID must be non-empty when provided",
    ),
    (
        lambda: DocumentContextRequest(ACCESS, "doc-1", anchor_chunk_id=""),
        "anchor chunk ID must be non-empty when provided",
    ),
    (
        lambda: DocumentContextRequest(ACCESS, "doc-1", anchor_section_path=("Intro", " ")),
        "anchor section path entries must be non-empty",
    ),
    (
        lambda: DocumentContextRequest(
            ACCESS, "doc-1", anchor_chunk_id="chunk-1", anchor_section_path=("Intro",)
        ),
        "use either anchor_chunk_id or anchor_section_path",
    ),
    (
        lambda: DocumentContextRequest(ACCESS, "doc-1", offset=-1),
        "context offset or limit is outside its bounded range",
    ),
    (
        lambda: DocumentContextRequest(ACCESS, "doc-1", limit=0),
        "context offset or limit is outside its bounded range",
    ),
    (
        lambda: DocumentContextRequest(ACCESS, "doc-1", limit=DOCUMENT_CONTEXT_LIMIT + 1),
        "context offset or limit is outside its bounded range",
    ),
    # Source and document inventories.
    (
        lambda: SourceListRequest(ACCESS, limit=0),
        f"source list limit must be between 1 and {SOURCE_LIST_LIMIT}",
    ),
    (
        lambda: SourceListRequest(
            ACCESS, source_scope_ids=tuple(f"s-{i}" for i in range(SOURCE_LIST_LIMIT + 1))
        ),
        "source list filters exceed their bounds",
    ),
    (
        lambda: SourceListRequest(ACCESS, connector_types=tuple(f"c-{i}" for i in range(11))),
        "source list filters exceed their bounds",
    ),
    (
        lambda: DocumentListRequest(ACCESS, limit=DOCUMENT_LIST_LIMIT + 1),
        f"document list limit must be between 1 and {DOCUMENT_LIST_LIMIT}",
    ),
    (
        lambda: DocumentListRequest(ACCESS, after_document_id="  "),
        "document cursor must be non-empty",
    ),
    # Retrieval and entities.
    (lambda: RetrievalRequest(ACCESS, "   "), "retrieval query must be non-empty"),
    (
        lambda: RetrievalRequest(ACCESS, "payments", top_k=101),
        "retrieval top_k must be between 1 and 100",
    ),
    (lambda: EntityFindRequest(ACCESS, " "), "entity query must be non-empty"),
    (
        lambda: EntityFindRequest(ACCESS, "engineers", limit=ENTITY_FIND_LIMIT + 1),
        f"entity limit must be between 1 and {ENTITY_FIND_LIMIT}",
    ),
    (
        lambda: EntityFindRequest(ACCESS, "engineers", facets={f"f{i}": "x" for i in range(13)}),
        "entity find request exceeds bounded filters",
    ),
    (
        lambda: EntityFindRequest(
            ACCESS, "engineers", source_scope_ids=tuple(f"s-{i}" for i in range(101))
        ),
        "entity find request exceeds bounded filters",
    ),
    (
        lambda: EvidenceFetchRequest(ACCESS, ()),
        "evidence fetch requires between 1 and 20 chunk IDs",
    ),
    (
        lambda: EvidenceFetchRequest(ACCESS, ("chunk-1", " ")),
        "evidence chunk IDs must be non-empty",
    ),
    (
        lambda: EvidenceFetchRequest(ACCESS, ("chunk-1", "chunk-1")),
        "evidence chunk IDs must be unique",
    ),
    (
        lambda: EntityResolveRequest(ACCESS, " "),
        "entity name must contain between 1 and 256 characters",
    ),
    (
        lambda: EntityResolveRequest(ACCESS, "x" * 257),
        "entity name must contain between 1 and 256 characters",
    ),
    (
        lambda: EntityResolveRequest(ACCESS, "Payments", limit=21),
        "entity resolution limit must be between 1 and 20",
    ),
    # Relation search.
    (
        lambda: RelationSearchRequest(ACCESS, " "),
        "relation search entity ID must be non-empty",
    ),
    (
        lambda: RelationSearchRequest(ACCESS, "entity-1", direction="sideways"),
        "relation direction must be outgoing, incoming, or either",
    ),
    (
        lambda: RelationSearchRequest(ACCESS, "entity-1", limit=31),
        "relation search limit must be between 1 and 30",
    ),
    (
        lambda: RelationSearchRequest(ACCESS, "entity-1", predicates=("owns", " ")),
        "relation predicates must contain at most five non-empty values",
    ),
    (
        lambda: RelationSearchRequest(ACCESS, "entity-1", predicates=("p",) * 6),
        "relation predicates must contain at most five non-empty values",
    ),
    # Semantic paths.
    (
        lambda: SemanticPathRequest(ACCESS, "entity-1", " "),
        "semantic path endpoints must be non-empty",
    ),
    (
        lambda: SemanticPathRequest(ACCESS, "entity-1", "entity-1"),
        "semantic path endpoints must be distinct",
    ),
    (
        lambda: SemanticPathRequest(ACCESS, "entity-1", "entity-2", traversal="outgoing"),
        "semantic path traversal must be directed or either",
    ),
    (
        lambda: SemanticPathRequest(ACCESS, "entity-1", "entity-2", max_hops=4),
        "semantic path max_hops must be between 1 and 3",
    ),
    (
        lambda: SemanticPathRequest(ACCESS, "entity-1", "entity-2", limit=6),
        "semantic path limit must be between 1 and 5",
    ),
    (
        lambda: SemanticPathRequest(ACCESS, "entity-1", "entity-2", predicates=("",)),
        "path predicates must contain at most five non-empty values",
    ),
]


@pytest.mark.parametrize(("build", "message"), INVALID_REQUESTS)
def test_reader_requests_reject_out_of_bounds_input(
    build: Callable[[], object], message: str
) -> None:
    with pytest.raises(ValueError) as error:
        build()

    assert str(error.value) == message


def test_document_context_request_accepts_a_section_anchor_window() -> None:
    request = DocumentContextRequest(
        ACCESS,
        "doc-1",
        expected_document_version_id="version-1",
        anchor_section_path=("Overview", "Setup"),
        offset=3,
        limit=DOCUMENT_CONTEXT_LIMIT,
        include_outline=True,
    )

    assert request.anchor_section_path == ("Overview", "Setup")
    assert request.anchor_chunk_id is None
    assert request.limit == DOCUMENT_CONTEXT_LIMIT


def test_retrieval_request_coerces_the_mode_and_keeps_its_defaults() -> None:
    request = RetrievalRequest(ACCESS, "payments", mode=RetrievalMode.FLAT.value)  # type: ignore[arg-type]

    assert request.mode is RetrievalMode.FLAT
    assert request.lane is RetrievalLane.HYBRID
    assert request.top_k == 10
    assert request.graph_seed_node_keys == ()


def test_valid_graph_requests_keep_their_bounds() -> None:
    relation = RelationSearchRequest(ACCESS, "entity-1", predicates=("owns",), direction="incoming")
    path = SemanticPathRequest(
        ACCESS, "entity-1", "entity-2", predicates=("owns",), traversal="directed", max_hops=3
    )
    fetch = EvidenceFetchRequest(ACCESS, ("chunk-1", "chunk-2"))
    resolve = EntityResolveRequest(ACCESS, "Payments", limit=20)
    evidence = EvidenceReadRequest(ACCESS, _selectors("chunk-1", "chunk-2"))
    sources = SourceListRequest(ACCESS, connector_types=("local",), limit=SOURCE_LIST_LIMIT)
    documents = DocumentListRequest(ACCESS, after_document_id="doc-1", limit=1)
    entities = EntityFindRequest(ACCESS, "engineers", facets={"level": ["senior"]})

    assert (relation.direction, relation.limit) == ("incoming", 8)
    assert (path.traversal, path.max_hops, path.limit) == ("directed", 3, 3)
    assert fetch.chunk_ids == ("chunk-1", "chunk-2")
    assert resolve.limit == 20
    assert [item.chunk_id for item in evidence.items] == ["chunk-1", "chunk-2"]
    assert sources.connector_types == ("local",)
    assert documents.after_document_id == "doc-1"
    assert entities.facets == {"level": ["senior"]}


def test_transport_field_lists_follow_the_dataclass_order() -> None:
    assert EVIDENCE_ITEM_FIELDS[:3] == ("chunk_id", "availability", "text")
    assert EVIDENCE_ITEM_FIELDS[-1] == "citation_locator"
    assert DOCUMENT_CONTEXT_CHUNK_FIELDS == (
        "chunk_id",
        "ordinal",
        "text",
        "chunk_kind",
        "section_path",
        "citation_locator",
    )
