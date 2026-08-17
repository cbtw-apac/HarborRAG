"""Deterministic eval corpus whose gold retrieval answers are known by construction.

Local topology (every Task 7-9 golden expectation derives from it):

    runbook  --links_to-->  architecture  --links_to-->  decisions
    incident --blocks--->   architecture
    runbook  --links_to-->  missing-page          (unresolved -> placeholder node)

Phase 3b widens that to one sample set per provider projector, additively -- see
``sources/fixtures/`` for the per-source shapes (one directory per source type, one
JSON file per sample) and ``CORPUS_SIGNATURES`` below for the edge vocabulary the
whole corpus is required to exercise.
"""

from __future__ import annotations

from dataclasses import dataclass

from harborrag_core.contracts.chunking import (
    SourceSpan,
    SplitBoundaryKind,
    TextRefinementRequest,
    TextSplit,
)
from harborrag_core.ingestion import KnowledgeNodeKind
from harborrag_core.schemas.ids import DocumentId, DocumentVersionId
from harborrag_engine.ingestion import (
    GraphDocumentTarget,
    GraphProjectionBatch,
    GraphProjectionBuilder,
    GraphProjectionInput,
)
from harborrag_engine.ingestion.chunking import (
    ChunkingConfig,
    ChunkingLimits,
    ChunkingProfile,
    ChunkingRequest,
    ChunkingService,
    build_chunking_service,
)

from .sources import eval_documents

TENANT_ID = "graph-eval"
GRAPH_NAME = "harborrag-graph-eval"

# KEEP IN SYNC (by review) with EXPECTED_SIGNATURES in
# packages/harborrag-engine/tests/ingestion/unit/test_graph_signatures.py -- engine test
# modules are not importable from this package, so this is a reviewed copy. Task 1 locks
# what the projection *can* emit; test_corpus.py asserts equality with what this corpus
# *does* emit, so the two frozensets have to stay identical set-for-set.
CORPUS_SIGNATURES: frozenset[tuple[str, str, str]] = frozenset(
    {
        ("Tenant", "has_data_source", "DataSource"),
        ("DataSource", "contains", "SourceEntity"),
        ("SourceEntity", "has_version", "DocumentVersion"),
        ("DocumentVersion", "contains", "Structure"),
        ("Structure", "parent_of", "Structure"),
        ("Structure", "contains", "Structure"),
        ("Structure", "links_to", "Structure"),
        ("Structure", "reply_to", "Structure"),
        ("Chunk", "supports", "Structure"),
        ("Chunk", "supports", "DocumentVersion"),
        ("SourceEntity", "links_to", "SourceEntity"),
        ("SourceEntity", "parent_of", "SourceEntity"),
        ("SourceEntity", "blocks", "SourceEntity"),
        ("SourceEntity", "duplicates", "SourceEntity"),
        ("SourceEntity", "relates_to", "SourceEntity"),
        ("SourceEntity", "has_attachment", "SourceEntity"),
        ("SourceEntity", "contains", "SourceEntity"),
        ("SourceEntity", "points_to", "SourceEntity"),
        ("DocumentVersion", "resolved_at", "SourceEntity"),
    }
)


class _CharacterCounter:
    def count(self, text: str) -> int:
        return len(text)


class _CharacterRefiner:
    def split(self, request: TextRefinementRequest) -> tuple[TextSplit, ...]:
        if not request.content:
            return ()
        results: list[TextSplit] = []
        start = 0
        base = request.source_span
        offset = base.start_offset if base and base.start_offset is not None else 0
        while start < len(request.content):
            end = min(start + request.maximum_tokens, len(request.content))
            results.append(
                TextSplit(
                    content=request.content[start:end],
                    token_count=end - start,
                    source_span=SourceSpan(
                        start_offset=offset + start,
                        end_offset=offset + end,
                        element_ids=base.element_ids if base else (),
                    ),
                    boundary_kind=SplitBoundaryKind.FORCED,
                    structural_path=request.structural_path,
                    forced_split=True,
                )
            )
            start = end
        return tuple(results)


def _chunking_service() -> ChunkingService:
    profile = ChunkingProfile(
        name="canonical", strategy="canonical", limits=ChunkingLimits(2, 40, 60, 0)
    )
    return build_chunking_service(
        config=ChunkingConfig(
            configuration_version="graph-eval-1",
            default_profile=profile.name,
            create_route_chunks=False,
            profiles={profile.name: profile},
            source_profiles={},
        ),
        token_counter=_CharacterCounter(),
        refiner=_CharacterRefiner(),
    )


@dataclass(frozen=True, slots=True)
class EvalCorpus:
    batches: dict[str, GraphProjectionBatch]
    versions: dict[str, str]

    def source_item_key(self, document_id: str) -> str:
        """The document's own source item: the has_version source for its version node.

        Structural rather than name-based -- a batch also holds the local root and one
        source node per link target, so filtering SOURCE_ENTITY nodes by logical_id is
        ambiguous. See ``GenericSourceProjector.project``.
        """

        version_key = self.document_version_key(document_id)
        keys = {
            relation.source_node_key
            for relation in self.batches[document_id].relations
            if relation.relation_type.value == "has_version"
            and relation.target_node_key == version_key
        }
        if len(keys) != 1:
            raise AssertionError(f"expected one source item for {document_id}, got {len(keys)}")
        return next(iter(keys))

    def document_version_key(self, document_id: str) -> str:
        return self._single_key(document_id, KnowledgeNodeKind.DOCUMENT_VERSION)

    def chunk_keys(self, document_id: str) -> frozenset[str]:
        return frozenset(
            node.node_key
            for node in self.batches[document_id].nodes
            if node.node_kind == KnowledgeNodeKind.CHUNK
        )

    def _single_key(self, document_id: str, kind: KnowledgeNodeKind) -> str:
        keys = [node.node_key for node in self.batches[document_id].nodes if node.node_kind == kind]
        if len(keys) != 1:
            raise AssertionError(f"expected one {kind} node for {document_id}, got {len(keys)}")
        return keys[0]


def build_corpus() -> EvalCorpus:
    service = _chunking_service()
    documents = eval_documents()
    versions = {document_id: f"{document_id}-version-1" for document_id in documents}
    # Every corpus document is its own resolved link target; anything a document links to
    # that is not a corpus document (missing-page, the cross-source Confluence URI) is
    # what the projection turns into a placeholder node.
    resolved = {
        document_id: GraphDocumentTarget(
            source_item_id=document_id,
            document_id=DocumentId(document_id),
            document_version_id=DocumentVersionId(versions[document_id]),
            source_scope_id=TENANT_ID,
            title=None,
        )
        for document_id in documents
    }
    batches: dict[str, GraphProjectionBatch] = {}
    for document_id, document in documents.items():
        chunks = service.chunk(
            ChunkingRequest(
                tenant_id=TENANT_ID,
                document_version_id=versions[document_id],
                connector_type=str(document.provenance.extra["connector_type"]),
                document=document,
            )
        ).chunks
        batches[document_id] = GraphProjectionBuilder().build(
            GraphProjectionInput(
                document=document,
                chunks=chunks,
                resolved_targets=resolved,
                graph_projection_version="graph-eval-v1",
            )
        )
    return EvalCorpus(batches=batches, versions=versions)
