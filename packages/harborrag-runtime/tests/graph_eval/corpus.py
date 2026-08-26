"""Deterministic eval corpus whose gold retrieval answers are known by construction.

Local topology (every Task 7-9 golden expectation derives from it):

    runbook  --links_to-->  architecture  --links_to-->  decisions
    incident --blocks--->   architecture
    runbook  --links_to-->  missing-page          (unresolved -> placeholder node)

See ``sources/fixtures/`` for the per-source shapes (one directory per source type,
one JSON file per sample) and ``CORPUS_SIGNATURES`` below for the edge vocabulary the
whole corpus is required to exercise.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import dotenv_values

from harborrag_core.ingestion import KnowledgeNodeKind
from harborrag_core.schemas.ids import DocumentId, DocumentVersionId
from harborrag_core.testing.chunking import CharacterCounter, CharacterRefiner
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
from harborrag_engine.testing.edge_signatures import PROJECTED_EDGE_SIGNATURES

from .sources import eval_documents

# Overridable via env/.env.database (or exported variables). The defaults are
# the committed-baseline identity: overriding HARBORRAG_EVAL_TENANT_ID also
# moves the baseline filename unit/test_health_baseline.py diffs against.
#
# Read into a dict rather than load_dotenv'd into os.environ: that file also carries
# HARBORRAG_SECRETS_ENCRYPTION_KEY, and importing this module during collection used to
# publish it process-wide, so three test_runtime_settings.py cases that assert what
# happens when the key is *absent* failed whenever graph_eval was collected alongside
# them -- passing on their own, failing in the suite.
_ENV_FILE_VALUES = dotenv_values(Path(__file__).resolve().parents[4] / "env/.env.database")


def _eval_setting(name: str, default: str) -> str:
    """Resolve one eval override; exported variables win, as override=False did."""

    value = os.getenv(name) or _ENV_FILE_VALUES.get(name) or ""
    return value.strip() or default


TENANT_ID = _eval_setting("HARBORRAG_EVAL_TENANT_ID", "graph-eval")
GRAPH_NAME = _eval_setting("HARBORRAG_EVAL_GRAPH_NAME", "harborrag-graph-eval")

# The reviewed vocabulary lives in harborrag_engine.testing; test_corpus.py
# asserts this corpus exercises every edge shape set-for-set.
CORPUS_SIGNATURES = PROJECTED_EDGE_SIGNATURES


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
        token_counter=CharacterCounter(),
        refiner=CharacterRefiner(),
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
