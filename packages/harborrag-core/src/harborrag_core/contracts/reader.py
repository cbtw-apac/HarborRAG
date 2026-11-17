"""Transport-independent reader requests and results.

Frozen values that the MCP tools, the SDK and the application services pass to the
reader. Requests check their own bounds in ``__post_init__``; nothing here performs
I/O, and every request carries the caller's ``AccessContext``.

Start from: RetrievalRequest, EvidenceReadRequest, DocumentContextRequest.
"""

from __future__ import annotations

from dataclasses import dataclass, field, fields
from enum import StrEnum
from typing import Literal

from harborrag_core.domain.retrieval import RetrievalResult
from harborrag_core.ingestion import GraphEdgeRecord, GraphNodeRecord, ReadableSource
from harborrag_core.retrieval import (
    GraphNodeResolutionQuery,
    GraphPath,
    GraphPathQuery,
    GraphSubgraphQuery,
    GraphTriplet,
    GraphTripletQuery,
)
from harborrag_core.security import AccessContext
from harborrag_core.topology.records import CanonicalAssertion, CanonicalMention
from harborrag_core.topology.search import EvidenceBundle, RetrievalMode


class RetrievalLane(StrEnum):
    """Select the retrieval representation used to rank candidates."""

    DENSE = "dense"
    SPARSE = "sparse"
    HYBRID = "hybrid"


EVIDENCE_BATCH_LIMIT = 10
DOCUMENT_CONTEXT_LIMIT = 10
SOURCE_LIST_LIMIT = 20
SOURCE_CONNECTOR_FILTER_LIMIT = 10
DOCUMENT_LIST_LIMIT = 20
EVIDENCE_AVAILABILITIES = ("available", "unavailable", "output_limit")
DOCUMENT_CONTEXT_OUTCOMES = ("ok", "unavailable", "version_changed", "output_limit")
type EvidenceAvailability = Literal["available", "unavailable", "output_limit"]
type DocumentContextOutcome = Literal["ok", "unavailable", "version_changed", "output_limit"]


@dataclass(frozen=True, slots=True)
class EvidenceReadSelector:
    """One chunk to read, optionally pinned to the document version the caller saw.

    Args:
        chunk_id: Chunk to read; must not be blank.
        expected_document_id: Document the chunk must belong to, when given.
        expected_document_version_id: Version the chunk must belong to, when given.
    """

    chunk_id: str
    expected_document_id: str | None = None
    expected_document_version_id: str | None = None

    def __post_init__(self) -> None:
        """Reject a blank chunk ID or a blank expected document or version ID."""
        if not self.chunk_id.strip():
            raise ValueError("evidence chunk ID must be non-empty")
        if self.expected_document_id is not None and not self.expected_document_id.strip():
            raise ValueError("expected document ID must be non-empty when provided")
        if (
            self.expected_document_version_id is not None
            and not self.expected_document_version_id.strip()
        ):
            raise ValueError("expected document version ID must be non-empty when provided")


@dataclass(frozen=True, slots=True)
class EvidenceReadRequest:
    """Read up to ``EVIDENCE_BATCH_LIMIT`` chunks by ID.

    Args:
        access: Caller identity; only chunks the caller may read are returned.
        items: Chunks to read, each at most once.
    """

    access: AccessContext
    items: tuple[EvidenceReadSelector, ...]

    def __post_init__(self) -> None:
        """Reject an empty or oversized batch and repeated chunk IDs."""
        if not 1 <= len(self.items) <= EVIDENCE_BATCH_LIMIT:
            raise ValueError(f"evidence read requires between 1 and {EVIDENCE_BATCH_LIMIT} items")
        chunk_ids = tuple(item.chunk_id for item in self.items)
        if len(set(chunk_ids)) != len(chunk_ids):
            raise ValueError("evidence read chunk IDs must be unique")


@dataclass(frozen=True, slots=True)
class EvidenceReadItem:
    """One chunk read, or the reason it could not be.

    Args:
        chunk_id: Chunk that was asked for.
        availability: ``available``, ``unavailable`` or ``output_limit`` when the
            response ran out of room.
        text: Chunk text, when available.
        document_id: Document the chunk belongs to.
        document_version_id: Version the chunk belongs to.
        document_title: Title of that document.
        source_scope_id: Source scope the document came from.
        connector_type: Connector that ingested the document.
        chunk_kind: Kind of chunk, such as text, table or code.
        ordinal: Position of the chunk within its document version.
        section_path: Headings from the document root to the chunk.
        citation_locator: Where the chunk sits in its source, for citations.
    """

    chunk_id: str
    availability: EvidenceAvailability
    text: str | None = None
    document_id: str | None = None
    document_version_id: str | None = None
    document_title: str | None = None
    source_scope_id: str | None = None
    connector_type: str | None = None
    chunk_kind: str | None = None
    ordinal: int | None = None
    section_path: tuple[str, ...] = ()
    citation_locator: dict[str, object] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class EvidenceReadResponse:
    """Chunks read for one ``EvidenceReadRequest``, in request order.

    Args:
        request_id: Identifies this read in logs and diagnostics.
        items: One entry per requested chunk.
    """

    request_id: str
    items: tuple[EvidenceReadItem, ...]


@dataclass(frozen=True, slots=True)
class DocumentContextRequest:
    """A window of consecutive chunks of one document.

    Args:
        access: Caller identity; the document must be readable by it.
        document_id: Document to read; must not be blank.
        expected_document_version_id: Version the caller saw; any other active
            version answers ``version_changed``.
        anchor_chunk_id: Chunk the window starts from; excludes ``anchor_section_path``.
        anchor_section_path: Section the window starts from; excludes ``anchor_chunk_id``.
        offset: Position in the document the window starts from; not negative. A
            non-zero offset wins over the anchors, and ``next_offset`` continues a
            window.
        limit: Chunks to return, from 1 to ``DOCUMENT_CONTEXT_LIMIT``.
        include_outline: Also return the section paths the window covers.
    """

    access: AccessContext
    document_id: str
    expected_document_version_id: str | None = None
    anchor_chunk_id: str | None = None
    anchor_section_path: tuple[str, ...] = ()
    offset: int = 0
    limit: int = 10
    include_outline: bool = False

    def __post_init__(self) -> None:
        """Reject blank IDs, both anchors at once, and an out-of-range window."""
        if not self.document_id.strip():
            raise ValueError("context document ID must be non-empty")
        if self.expected_document_version_id is not None and not (
            self.expected_document_version_id.strip()
        ):
            raise ValueError("expected document version ID must be non-empty when provided")
        if self.anchor_chunk_id is not None and not self.anchor_chunk_id.strip():
            raise ValueError("anchor chunk ID must be non-empty when provided")
        if any(not part.strip() for part in self.anchor_section_path):
            raise ValueError("anchor section path entries must be non-empty")
        if self.anchor_chunk_id is not None and self.anchor_section_path:
            raise ValueError("use either anchor_chunk_id or anchor_section_path")
        if self.offset < 0 or not 1 <= self.limit <= DOCUMENT_CONTEXT_LIMIT:
            raise ValueError("context offset or limit is outside its bounded range")


@dataclass(frozen=True, slots=True)
class DocumentContextChunk:
    """One chunk inside a document context window.

    Args:
        chunk_id: Chunk identifier.
        ordinal: Position of the chunk within its document version.
        text: Chunk text.
        chunk_kind: Kind of chunk, such as text, table or code.
        section_path: Headings from the document root to the chunk.
        citation_locator: Where the chunk sits in its source, for citations.
    """

    chunk_id: str
    ordinal: int
    text: str
    chunk_kind: str
    section_path: tuple[str, ...]
    citation_locator: dict[str, object]


@dataclass(frozen=True, slots=True)
class DocumentContextResponse:
    """A document context window, or why there is none.

    Args:
        request_id: Identifies this read in logs and diagnostics.
        outcome: ``ok``, ``unavailable``, ``version_changed`` or ``output_limit``.
        document_id: Document that was asked for.
        document_version_id: Active version the window was read from.
        chunks: Chunks of the window, in document order.
        outline: Section paths the window covers, when an outline was asked for.
        next_offset: Offset that continues the window; ``None`` at the end.
        document_title: Title read from the window's chunks. Last, with a default,
            so positional constructions of the earlier fields keep working.
    """

    request_id: str
    outcome: DocumentContextOutcome
    document_id: str
    document_version_id: str | None = None
    chunks: tuple[DocumentContextChunk, ...] = ()
    outline: tuple[tuple[str, ...], ...] = ()
    next_offset: int | None = None
    document_title: str | None = None


@dataclass(frozen=True, slots=True)
class SourceListRequest:
    """A page of the source scopes the caller can read.

    Args:
        access: Caller identity; only readable scopes are listed.
        source_scope_ids: Scopes to keep, up to ``SOURCE_LIST_LIMIT``; empty keeps all.
        connector_types: Connector types to keep, up to
            ``SOURCE_CONNECTOR_FILTER_LIMIT``; empty keeps all.
        after_source_scope_id: Cursor: list the scopes after this one.
        limit: Scopes to return, from 1 to ``SOURCE_LIST_LIMIT``.
    """

    access: AccessContext
    source_scope_ids: tuple[str, ...] = ()
    connector_types: tuple[str, ...] = ()
    after_source_scope_id: str | None = None
    limit: int = 20

    def __post_init__(self) -> None:
        """Reject a limit or filters outside their bounds."""
        if not 1 <= self.limit <= SOURCE_LIST_LIMIT:
            raise ValueError(f"source list limit must be between 1 and {SOURCE_LIST_LIMIT}")
        if (
            len(self.source_scope_ids) > SOURCE_LIST_LIMIT
            or len(self.connector_types) > SOURCE_CONNECTOR_FILTER_LIMIT
        ):
            raise ValueError("source list filters exceed their bounds")


@dataclass(frozen=True, slots=True)
class SourceListResponse:
    """One page of readable source scopes.

    Args:
        request_id: Identifies this read in logs and diagnostics.
        sources: Scopes on this page.
        has_more: More scopes follow this page.
    """

    request_id: str
    sources: tuple[ReadableSource, ...]
    has_more: bool = False


@dataclass(frozen=True, slots=True)
class DocumentMetadata:
    """What a document list shows for one document.

    Args:
        document_id: Document identifier.
        document_version_id: Active version of the document.
        title: Document title, when the source has one.
        source_scope_id: Source scope the document came from.
        connector_type: Connector that ingested the document.
        chunk_count: Chunks in the active version.
    """

    document_id: str
    document_version_id: str
    title: str | None
    source_scope_id: str
    connector_type: str
    chunk_count: int


@dataclass(frozen=True, slots=True)
class DocumentListRequest:
    """A page of the documents the caller can read.

    Args:
        access: Caller identity; only readable documents are listed.
        after_document_id: Cursor: list the documents after this one.
        limit: Documents to return, from 1 to ``DOCUMENT_LIST_LIMIT``.
    """

    access: AccessContext
    after_document_id: str | None = None
    limit: int = DOCUMENT_LIST_LIMIT

    def __post_init__(self) -> None:
        """Reject an out-of-range limit or a blank cursor."""
        if not 1 <= self.limit <= DOCUMENT_LIST_LIMIT:
            raise ValueError(f"document list limit must be between 1 and {DOCUMENT_LIST_LIMIT}")
        if self.after_document_id is not None and not self.after_document_id.strip():
            raise ValueError("document cursor must be non-empty")


@dataclass(frozen=True, slots=True)
class DocumentListResponse:
    """One page of readable documents.

    Args:
        request_id: Identifies this read in logs and diagnostics.
        documents: Documents on this page.
        next_document_id: Cursor for the next page; ``None`` at the end.
    """

    request_id: str
    documents: tuple[DocumentMetadata, ...]
    next_document_id: str | None = None


@dataclass(frozen=True, slots=True)
class GraphNodeResolveRequest:
    """Resolve names or keys to knowledge-graph nodes.

    Args:
        access: Caller identity; only readable nodes are returned.
        query: What to resolve and how many candidates to return.
    """

    access: AccessContext
    query: GraphNodeResolutionQuery


@dataclass(frozen=True, slots=True)
class GraphNodeResolveResponse:
    """Graph nodes that match a ``GraphNodeResolveRequest``.

    Args:
        request_id: Identifies this read in logs and diagnostics.
        candidates: Matching nodes, best first.
        truncated: More nodes matched than were returned.
    """

    request_id: str
    candidates: tuple[GraphNodeRecord, ...]
    truncated: bool = False


EVIDENCE_ITEM_FIELDS = tuple(value.name for value in fields(EvidenceReadItem))
DOCUMENT_CONTEXT_CHUNK_FIELDS = tuple(value.name for value in fields(DocumentContextChunk))


@dataclass(frozen=True, slots=True)
class RetrievalRequest:
    """One passage search within the caller's tenant.

    Args:
        access: Caller identity, tenant and corpus mode; every read is scoped by it.
        query: Question to search for; must not be blank.
        top_k: Passages to return, from 1 to 100.
        filters: Evidence filter; ``facet.*`` keys select source entities and
            ``fields.*`` keys an item's own fields.
        lane: Dense vectors, sparse terms, or both fused.
        observe_graph: Observe the knowledge graph around the results.
        graph_seed_node_keys: Graph nodes to also start observation from, beyond the
            ones the results sit on, such as the entities a session's recalled
            memories reference. Read only when ``observe_graph`` is on.
        mode: ``FLAT`` ranks passages only; any other mode also selects topology
            evidence around them.
        rerank: Reorder the passages with the model catalog's rerank model before
            keeping ``top_k``.
    """

    access: AccessContext
    query: str
    top_k: int = 10
    filters: dict[str, object] = field(default_factory=dict)
    lane: RetrievalLane = RetrievalLane.HYBRID
    observe_graph: bool = False
    graph_seed_node_keys: tuple[str, ...] = ()
    mode: RetrievalMode = RetrievalMode.FLAT
    rerank: bool = False

    def __post_init__(self) -> None:
        """Coerce ``mode`` and reject a blank query or an out-of-range ``top_k``."""
        object.__setattr__(self, "mode", RetrievalMode(self.mode))
        if not self.query.strip():
            raise ValueError("retrieval query must be non-empty")
        if not 1 <= self.top_k <= 100:
            raise ValueError("retrieval top_k must be between 1 and 100")


@dataclass(frozen=True, slots=True)
class RetrievalResponse:
    """Passages found for one ``RetrievalRequest``.

    Args:
        request_id: Identifies this search in logs and diagnostics.
        lane: Lane that ranked the passages.
        results: Passages, best first.
        diagnostics: Counts and timings of the search, for operators.
        evidence: Topology evidence selected around the passages.
    """

    request_id: str
    lane: RetrievalLane
    results: tuple[RetrievalResult, ...]
    diagnostics: dict[str, object]
    evidence: EvidenceBundle = field(default_factory=EvidenceBundle)


ENTITY_FIND_LIMIT = 50


@dataclass(frozen=True, slots=True)
class EntityFindRequest:
    """Rank source entities against a question, within declared facet values.

    ``facets`` is keyed by facet name; a value is a string (equals), a list
    (any of), or a mapping of ``gte``/``gt``/``lte``/``lt`` bounds for an integer
    facet. It is the one place a caller states *which* entities qualify, as
    opposed to what to look for in them.

    Args:
        access: Caller identity; only released entities and evidence are returned.
        query: Question to rank entities against; must not be blank.
        facets: Facet name to the value or bounds an entity must have; at most 12.
        source_scope_ids: Scopes to search, at most 100; empty searches all.
        limit: Entities to return, from 1 to ``ENTITY_FIND_LIMIT``.
    """

    access: AccessContext
    query: str
    facets: dict[str, object] = field(default_factory=dict)
    source_scope_ids: tuple[str, ...] = ()
    limit: int = 10

    def __post_init__(self) -> None:
        """Reject a blank query, an out-of-range limit and oversized filters."""
        if not self.query.strip():
            raise ValueError("entity query must be non-empty")
        if not 1 <= self.limit <= ENTITY_FIND_LIMIT:
            raise ValueError(f"entity limit must be between 1 and {ENTITY_FIND_LIMIT}")
        if len(self.facets) > 12 or len(self.source_scope_ids) > 100:
            raise ValueError("entity find request exceeds bounded filters")


@dataclass(frozen=True, slots=True)
class EntityMatch:
    """One ranked source entity with the evidence released for it.

    Args:
        node_key: Graph node of the entity.
        source_scope_id: Source scope the entity came from.
        rank: Position in the answer, from 1.
        score: Similarity of the entity card to the query.
        description: Summary card description, when one was released.
        coverage_mode: ``complete``, or ``partial`` when the card covers only some of
            the entity's source items.
        attributes: Card attributes, each with ``name``, ``values`` and
            ``from_document_ids``.
        evidence_chunk_ids: Evidence chunks behind the card, capped per entity.
    """

    node_key: str
    source_scope_id: str | None
    rank: int
    score: float
    description: str | None
    coverage_mode: str | None
    attributes: tuple[dict[str, object], ...]
    evidence_chunk_ids: tuple[str, ...]


ENTITY_INDEX_UNAVAILABLE = "entity_index_unavailable"
ENTITIES_WITHOUT_RELEASED_EVIDENCE = "entities_without_released_evidence"


@dataclass(frozen=True, slots=True)
class EntityFindResponse:
    """Entities ranked for one ``EntityFindRequest``.

    Args:
        request_id: Identifies this search in logs and diagnostics.
        matches: Ranked entities, best first.
        truncated: More entities matched than were returned.
        reasons: Why the answer may hold fewer matches than the corpus does, other
            than truncation: ``ENTITY_INDEX_UNAVAILABLE`` or
            ``ENTITIES_WITHOUT_RELEASED_EVIDENCE``. Without them "no index has been
            built" and "nothing matched" would be the same empty answer, and a caller
            would have no reason to fall back to chunk search.
        withheld_count: Ranked entities left out because the summary authority
            released no card or evidence for them, counted only where that cannot
            reveal a private entity.
    """

    request_id: str
    matches: tuple[EntityMatch, ...]
    truncated: bool = False
    reasons: tuple[str, ...] = ()
    withheld_count: int = 0


@dataclass(frozen=True, slots=True)
class EvidenceFetchRequest:
    """Fetch up to 20 chunks by ID as retrieval results.

    Args:
        access: Caller identity; only readable chunks are returned.
        chunk_ids: Chunks to fetch, each non-blank and at most once.
    """

    access: AccessContext
    chunk_ids: tuple[str, ...]

    def __post_init__(self) -> None:
        """Reject an empty or oversized batch, blank IDs and repeated IDs."""
        if not 1 <= len(self.chunk_ids) <= 20:
            raise ValueError("evidence fetch requires between 1 and 20 chunk IDs")
        if any(not item.strip() for item in self.chunk_ids):
            raise ValueError("evidence chunk IDs must be non-empty")
        if len(set(self.chunk_ids)) != len(self.chunk_ids):
            raise ValueError("evidence chunk IDs must be unique")


@dataclass(frozen=True, slots=True)
class EvidenceFetchResponse:
    """Chunks fetched for one ``EvidenceFetchRequest``.

    Args:
        request_id: Identifies this read in logs and diagnostics.
        results: Fetched chunks as retrieval results.
        unavailable_chunk_ids: Requested chunks that were not returned; the response
            does not say why.
    """

    request_id: str
    results: tuple[RetrievalResult, ...]
    unavailable_chunk_ids: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class EntityResolveRequest:
    """Resolve an entity name to its canonical mentions.

    Args:
        access: Caller identity; only readable mentions are returned.
        name: Entity name, from 1 to 256 characters.
        limit: Mentions to return, from 1 to 20.
    """

    access: AccessContext
    name: str
    limit: int = 5

    def __post_init__(self) -> None:
        """Reject a blank or overlong name and an out-of-range limit."""
        if not self.name.strip() or len(self.name) > 256:
            raise ValueError("entity name must contain between 1 and 256 characters")
        if not 1 <= self.limit <= 20:
            raise ValueError("entity resolution limit must be between 1 and 20")


@dataclass(frozen=True, slots=True)
class EntityResolveResponse:
    """Canonical mentions resolved for one ``EntityResolveRequest``.

    Args:
        request_id: Identifies this read in logs and diagnostics.
        mentions: Matching mentions, best first.
        truncated: More mentions matched than were returned.
    """

    request_id: str
    mentions: tuple[CanonicalMention, ...]
    truncated: bool = False


@dataclass(frozen=True, slots=True)
class RelationSearchRequest:
    """Assertions that connect one canonical entity to others.

    Args:
        access: Caller identity; only readable assertions are returned.
        entity_id: Canonical entity to start from; must not be blank.
        predicates: Relation types to keep, at most five; empty keeps all.
        direction: ``outgoing``, ``incoming`` or ``either``.
        limit: Assertions to return, from 1 to 30.
    """

    access: AccessContext
    entity_id: str
    predicates: tuple[str, ...] = ()
    direction: str = "either"
    limit: int = 8

    def __post_init__(self) -> None:
        """Reject a blank entity, an unknown direction and out-of-range bounds."""
        if not self.entity_id.strip():
            raise ValueError("relation search entity ID must be non-empty")
        if self.direction not in {"outgoing", "incoming", "either"}:
            raise ValueError("relation direction must be outgoing, incoming, or either")
        if not 1 <= self.limit <= 30:
            raise ValueError("relation search limit must be between 1 and 30")
        if len(self.predicates) > 5 or any(not item.strip() for item in self.predicates):
            raise ValueError("relation predicates must contain at most five non-empty values")


@dataclass(frozen=True, slots=True)
class RelationSearchResponse:
    """Assertions found for one ``RelationSearchRequest``.

    Args:
        request_id: Identifies this read in logs and diagnostics.
        assertions: Matching assertions.
        mentions: Canonical mentions the assertions refer to.
        truncated: More assertions matched than were returned.
    """

    request_id: str
    assertions: tuple[CanonicalAssertion, ...]
    mentions: tuple[CanonicalMention, ...]
    truncated: bool = False


@dataclass(frozen=True, slots=True)
class SemanticPathRequest:
    """Assertion paths between two canonical entities.

    Args:
        access: Caller identity; only readable assertions are used.
        start_entity_id: Entity the paths start from.
        end_entity_id: Entity the paths end at; must differ from the start.
        predicates: Relation types a path may use, at most five; empty allows all.
        traversal: ``directed`` follows assertion direction; ``either`` ignores it.
        max_hops: Assertions per path, from 1 to 3.
        limit: Paths to return, from 1 to 5.
    """

    access: AccessContext
    start_entity_id: str
    end_entity_id: str
    predicates: tuple[str, ...] = ()
    traversal: str = "either"
    max_hops: int = 2
    limit: int = 3

    def __post_init__(self) -> None:
        """Reject blank or equal endpoints, an unknown traversal and out-of-range bounds."""
        if not self.start_entity_id.strip() or not self.end_entity_id.strip():
            raise ValueError("semantic path endpoints must be non-empty")
        if self.start_entity_id == self.end_entity_id:
            raise ValueError("semantic path endpoints must be distinct")
        if self.traversal not in {"directed", "either"}:
            raise ValueError("semantic path traversal must be directed or either")
        if not 1 <= self.max_hops <= 3:
            raise ValueError("semantic path max_hops must be between 1 and 3")
        if not 1 <= self.limit <= 5:
            raise ValueError("semantic path limit must be between 1 and 5")
        if len(self.predicates) > 5 or any(not item.strip() for item in self.predicates):
            raise ValueError("path predicates must contain at most five non-empty values")


@dataclass(frozen=True, slots=True)
class SemanticPathResponse:
    """Paths found for one ``SemanticPathRequest``.

    Args:
        request_id: Identifies this read in logs and diagnostics.
        paths: Each path as its assertions in order.
        mentions: Canonical mentions the assertions refer to.
        truncated: More paths existed than were returned.
    """

    request_id: str
    paths: tuple[tuple[CanonicalAssertion, ...], ...]
    mentions: tuple[CanonicalMention, ...]
    truncated: bool = False


@dataclass(frozen=True, slots=True)
class GraphTripletRequest:
    """Search knowledge-graph triplets.

    Args:
        access: Caller identity; only readable triplets are returned.
        query: What to search for and its bounds.
    """

    access: AccessContext
    query: GraphTripletQuery


@dataclass(frozen=True, slots=True)
class GraphTripletResponse:
    """Triplets found for one ``GraphTripletRequest``.

    Args:
        triplets: Matching triplets.
        diagnostics: Counts and limits of the search, for operators.
    """

    triplets: tuple[GraphTriplet, ...]
    diagnostics: dict[str, object]


@dataclass(frozen=True, slots=True)
class GraphPathRequest:
    """Search knowledge-graph paths between nodes.

    Args:
        access: Caller identity; only readable paths are returned.
        query: Endpoints and bounds of the search.
    """

    access: AccessContext
    query: GraphPathQuery


@dataclass(frozen=True, slots=True)
class GraphPathResponse:
    """Paths found for one ``GraphPathRequest``.

    Args:
        paths: Matching paths.
        diagnostics: Counts and limits of the search, for operators.
    """

    paths: tuple[GraphPath, ...]
    diagnostics: dict[str, object]


@dataclass(frozen=True, slots=True)
class GraphSubgraphRequest:
    """Expand the knowledge-graph neighbourhood of some nodes.

    Args:
        access: Caller identity; only readable nodes and relations are returned.
        query: Seed nodes and bounds of the expansion.
    """

    access: AccessContext
    query: GraphSubgraphQuery


@dataclass(frozen=True, slots=True)
class GraphSubgraphResponse:
    """Subgraph expanded for one ``GraphSubgraphRequest``.

    Args:
        nodes: Nodes in the subgraph.
        relations: Relations between those nodes.
        diagnostics: Counts and limits of the expansion, for operators.
    """

    nodes: tuple[GraphNodeRecord, ...]
    relations: tuple[GraphEdgeRecord, ...]
    diagnostics: dict[str, object]
