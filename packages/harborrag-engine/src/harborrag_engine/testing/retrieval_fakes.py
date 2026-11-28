"""In-memory collaborators for retrieval tests in the engine and runtime suites.

Imports only core and engine, and no test framework, so any suite can use it. A
fake that needs runtime or adapters belongs in that suite's own support module.

Typical usage example:

    service = RuntimeRetrievalService(resources=resources(), policy=policy())
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import replace
from types import SimpleNamespace
from typing import Any

from harborrag_core.chunking import (
    ChunkKind,
    ChunkRecord,
    ChunkSecurity,
    ConnectorType,
    DocumentKind,
    RecordKind,
)
from harborrag_core.indexing import VectorIndexRecord, VectorSearchResult
from harborrag_core.ingestion import (
    ActiveDocumentVersion,
    DocumentIdentityBuilder,
    KnowledgeGraphTraversal,
    SparseEncoderProfile,
)
from harborrag_core.retrieval import GraphTraversalQuery
from harborrag_core.schemas.ids import ChunkId, DocumentId, DocumentVersionId, TenantId
from harborrag_core.storage import StorageOperationContext
from harborrag_core.topology.extraction import EvidenceSpan, ExtractedAssertion, ExtractedEntity
from harborrag_core.topology.records import CanonicalAssertion, CanonicalMention
from harborrag_engine.ingestion import BM25SparseEncoder
from harborrag_engine.retrieval.service.contracts import RetrievalPolicy, RetrievalResources
from harborrag_engine.retrieval.service.service import RuntimeRetrievalService

_EVIDENCE_CONTENT = "The activity timeout is 30 seconds."


class FakeEmbedClient:
    """Embedding client that returns one fixed vector and records its requests.

    Attributes:
        requests: Every request passed to ``aembed``, in order.
    """

    requests: list[Any]

    def __init__(self) -> None:
        self.requests = []

    async def aembed(self, *, request: Any) -> SimpleNamespace:  # noqa: ANN401 - any request shape
        """Record ``request`` and return the vector ``(1.0, 0.0, 0.0)``."""
        self.requests.append(request)
        return SimpleNamespace(embeddings=(SimpleNamespace(value=(1.0, 0.0, 0.0)),))

    async def aclose(self) -> None:
        """Release nothing."""


class FakeVectorRepository:
    """Vector repository that answers every evidence search with one point.

    Searches of an index whose name has no ``evidence`` in it find nothing.

    Attributes:
        dense_queries: ``(query, context)`` of every dense search.
        sparse_queries: ``(query, context)`` of every sparse search.
        hybrid_queries: ``(query, context)`` of every hybrid search.
        drop_provenance: Leave the document title and section path out of the point.
    """

    dense_queries: list[tuple[Any, Any]]
    sparse_queries: list[tuple[Any, Any]]
    hybrid_queries: list[tuple[Any, Any]]
    drop_provenance: bool = False

    def __init__(self) -> None:
        self.dense_queries = []
        self.sparse_queries = []
        self.hybrid_queries = []

    async def search(self, query: Any, *, context: Any) -> list[VectorSearchResult]:  # noqa: ANN401 - any query shape
        """Record the dense query and return the canned results of its index."""
        self.dense_queries.append((query, context))
        return self._results(query.index_name)

    async def sparse_search(self, query: Any, *, context: Any) -> list[VectorSearchResult]:  # noqa: ANN401 - any query shape
        """Record the sparse query and return the canned results of its index."""
        self.sparse_queries.append((query, context))
        return self._results(query.index_name)

    async def hybrid_search(self, query: Any, *, context: Any) -> list[VectorSearchResult]:  # noqa: ANN401 - any query shape
        """Record the hybrid query and return the canned results of its index."""
        self.hybrid_queries.append((query, context))
        return self._results(query.index_name)

    def _results(self, collection: str) -> list[VectorSearchResult]:
        if "evidence" not in collection:
            return []
        provenance = (
            {}
            if self.drop_provenance
            else {
                "document_title": "FE Onboarding Checklist",
                "section_path": ["Onboarding", "Accounts"],
            }
        )
        return [
            VectorSearchResult(
                id="point-1",
                score=0.9,
                raw_score=0.9,
                relevance=0.9,
                payload={
                    "chunk_id": "chunk-1",
                    "document_id": "document-1",
                    "document_version_id": "version-1",
                    "record_kind": "evidence",
                    "chunk_kind": ["text"],
                    "connector_type": "local",
                    "content": _EVIDENCE_CONTENT,
                    **provenance,
                },
            )
        ]


class FakeActiveVersions:
    """Active-version resolver that says ``version-1`` is active for ``document-1``."""

    async def active_versions(
        self,
        document_ids: Sequence[str],
    ) -> dict[str, ActiveDocumentVersion]:
        """Return the one active version, whatever the requested ``document_ids``."""
        del document_ids
        return {
            "document-1": ActiveDocumentVersion(
                document_id=DocumentId("document-1"),
                document_version_id=DocumentVersionId("version-1"),
            )
        }


class FakeChunkReader:
    """Chunk reader that returns one public evidence chunk and records its references.

    Attributes:
        references: ``(reference, context)`` of every read.
    """

    references: list[tuple[Any, Any]]

    def __init__(self) -> None:
        self.references = []

    async def get_reference(self, reference: Any, *, context: Any) -> ChunkRecord:  # noqa: ANN401 - any reference shape
        """Record the read and return the evidence chunk of ``context``'s tenant."""
        self.references.append((reference, context))
        return ChunkRecord(
            strategy_version="strategy-1",
            logical_chunk_id=ChunkId("logical-chunk:1"),
            chunk_id=ChunkId("chunk-1"),
            connector_type=ConnectorType.LOCAL,
            document_kind=DocumentKind.LOCAL_FILE,
            record_kind=RecordKind.EVIDENCE,
            chunk_kind=ChunkKind.TEXT,
            tenant_id=TenantId(str(context.tenant_id)),
            connection_id="connection-1",
            source_scope_id="scope-1",
            source_item_id="guide.md",
            source_version="source-version-1",
            document_id=DocumentId("document-1"),
            document_version_id=DocumentVersionId("version-1"),
            ordinal=0,
            content=_EVIDENCE_CONTENT,
            embedding_text=_EVIDENCE_CONTENT,
            search_text=_EVIDENCE_CONTENT,
            token_count=6,
            content_hash="content-hash",
            security=ChunkSecurity(permission_set_id="permission-set:public"),
        )


class FakeGraphRepository:
    """Graph repository that finds nothing and records its traversals.

    Attributes:
        queries: ``(start_node_key, query)`` of every traversal.
    """

    queries: list[tuple[str, GraphTraversalQuery]]

    def __init__(self) -> None:
        self.queries = []

    async def traverse(
        self, query: GraphTraversalQuery, *, context: StorageOperationContext
    ) -> KnowledgeGraphTraversal:
        """Record the traversal and return an empty graph."""
        del context
        self.queries.append((query.start_node_key, query))
        return KnowledgeGraphTraversal(nodes=(), relations=())


class FailingGraphRepository(FakeGraphRepository):
    """Graph repository whose traversals raise ``ConnectionError``."""

    async def traverse(
        self, query: GraphTraversalQuery, *, context: StorageOperationContext
    ) -> KnowledgeGraphTraversal:
        """Raise ``ConnectionError``."""
        del query, context
        raise ConnectionError("graph unavailable")


class MixedVectorRepository(FakeVectorRepository):
    """Vector repository that adds a point without ``content`` to every result."""

    def _results(self, collection: str) -> list[VectorSearchResult]:
        results = super()._results(collection)
        if not results:
            return []
        malformed = results[0].model_copy(
            update={
                "id": "point-2",
                "payload": {
                    key: value for key, value in results[0].payload.items() if key != "content"
                },
            }
        )
        return [*results, malformed]


def resources(  # noqa: PLR0913 - one override per collaborator
    *,
    embed: Any = None,  # noqa: ANN401 - any embedding client double
    vectors: Any = None,  # noqa: ANN401 - any vector repository double
    chunks: Any = None,  # noqa: ANN401 - any chunk reader double
    graph: Any = None,  # noqa: ANN401 - any graph repository double
    active_versions: Any = None,  # noqa: ANN401 - any resolver double
) -> RetrievalResources:
    """Build retrieval resources from fakes, replacing any collaborator passed in."""
    return RetrievalResources(
        embed_client=embed or FakeEmbedClient(),  # type: ignore[arg-type]  # structural double
        vector_repository=vectors or FakeVectorRepository(),  # type: ignore[arg-type]  # structural double
        active_versions=active_versions or FakeActiveVersions(),
        chunk_reader=chunks or FakeChunkReader(),  # type: ignore[arg-type]  # structural double
        sparse_encoder=BM25SparseEncoder(SparseEncoderProfile(profile_id="bm25-v1")),
        graph_repository=graph,
    )


def policy() -> RetrievalPolicy:
    """Return the retrieval policy of the three-dimension test embedding model."""
    return RetrievalPolicy(embedding_model="embed", embedding_dimensions=3)


def mention(
    entity: str,
    chunk: str,
    name: str,
    *,
    owner: tuple[str, str, str] = ("build-1", "document-1", "version-1"),
) -> CanonicalMention:
    """Build the mention of ``name`` in ``chunk``, owned by a ``(build, document, version)``."""
    build, document, version = owner
    return CanonicalMention(
        mention_id=f"mention-{entity}-{chunk}",
        entity_id=entity,
        tenant_id="tenant-1",
        build_id=build,
        document_id=document,
        document_version_id=version,
        chunk_id=chunk,
        observation=ExtractedEntity(
            local_id=entity,
            name=name,
            entity_type="service",
            span=EvidenceSpan(start=0, end=len(name), quote=name),
        ),
    )


class CanonicalTopology:
    """Topology repository with two services, ``Alpha`` and ``Beta``, and one dependency.

    Attributes:
        mentions: Canonical mentions of both services.
        assertions: The one ``depends_on`` assertion from ``Alpha`` to ``Beta``.
        eligible: Build IDs whose data is served.
        calls: Arguments of every ``active_mentions`` call.
        validation_calls: Arguments of every ``eligible_build_ids`` call.
    """

    mentions: tuple[CanonicalMention, ...]
    assertions: tuple[CanonicalAssertion, ...]
    eligible: set[str]
    calls: list[tuple[Any, ...]]
    validation_calls: list[tuple[Any, ...]]

    def __init__(self) -> None:
        self.mentions = (
            mention("entity-1", "chunk-1", "Alpha"),
            mention(
                "entity-2",
                "chunk-2",
                "Beta",
                owner=("build-2", "document-2", "version-2"),
            ),
        )
        self.assertions = (
            CanonicalAssertion(
                assertion_id="assertion-1",
                tenant_id="tenant-1",
                build_id="build-1",
                document_id="document-1",
                document_version_id="version-1",
                chunk_id="chunk-1",
                subject_entity_id="entity-1",
                object_entity_id="entity-2",
                observation=ExtractedAssertion(
                    local_id="a1",
                    subject_id="entity-1",
                    object_id="entity-2",
                    predicate="depends_on",
                    span=EvidenceSpan(start=0, end=22, quote="Alpha depends on Beta."),
                ),
            ),
        )
        self.eligible = {"build-1", "build-2"}
        self.calls = []
        self.validation_calls = []

    async def active_mentions(  # noqa: PLR0913 - mirrors the authorized repository contract
        self,
        tenant_id: str,
        *,
        labels: Sequence[str] = (),
        chunk_ids: Sequence[str] = (),
        entity_ids: Sequence[str] = (),
        limit: int = 100,
        access: object | None = None,
    ) -> tuple[CanonicalMention, ...]:
        """Return the mentions named by label, chunk or entity, up to ``limit``."""
        self.calls.append((tenant_id, labels, chunk_ids, entity_ids, limit))
        folded = {label.casefold() for label in labels}
        return tuple(
            item
            for item in self.mentions
            if item.tenant_id == tenant_id
            and (
                item.observation.name.casefold() in folded
                or item.chunk_id in chunk_ids
                or item.entity_id in entity_ids
            )
        )[:limit]

    async def active_assertions(
        self,
        tenant_id: str,
        *,
        entity_ids: Sequence[str] = (),
        limit: int = 100,
        access: object | None = None,
    ) -> tuple[CanonicalAssertion, ...]:
        """Return the assertions that touch any of ``entity_ids``, up to ``limit``."""
        return tuple(
            item
            for item in self.assertions
            if item.tenant_id == tenant_id
            and (item.subject_entity_id in entity_ids or item.object_entity_id in entity_ids)
        )[:limit]

    async def eligible_build_ids(
        self, tenant_id: str, build_ids: Sequence[str], *, access: object | None = None
    ) -> set[str]:
        """Record the call and return the requested builds that are eligible."""
        self.validation_calls.append((tenant_id, build_ids))
        return set(build_ids) & self.eligible

    async def allowed_document_ids(
        self, tenant_id: str, *, access: object | None, limit: int = 10000
    ) -> tuple[str, ...]:
        """Return both documents."""
        return ("document-1", "document-2")

    async def authorized_document_ids(
        self, tenant_id: str, document_ids: Sequence[str], *, access: object | None
    ) -> set[str]:
        """Return the requested documents that ``allowed_document_ids`` lists."""
        return set(document_ids) & set(await self.allowed_document_ids(tenant_id, access=access))


def record(chunk: str, document: str, version: str) -> VectorIndexRecord:
    """Build the indexed evidence point of ``chunk``, for ``chunk-1`` or ``chunk-2``."""
    return VectorIndexRecord(
        id=DocumentIdentityBuilder().point_id(chunk_id=chunk),
        tenant_id=TenantId("tenant-1"),
        vector=[1, 0, 0],
        payload={
            "chunk_id": chunk,
            "document_id": document,
            "document_version_id": version,
            "record_kind": "evidence",
            "chunk_kind": ["text"],
            "connector_type": "local",
            "content": "Alpha depends on Beta." if chunk == "chunk-1" else "Beta controls retries.",
        },
    )


class TopologyVectors(FakeVectorRepository):
    """Vector repository whose searches find the point of ``chunk-1``.

    Attributes:
        get_calls: ``(collection, ids, context)`` of every ``get_records`` call.
        records: Indexed points of ``chunk-1`` and ``chunk-2``.
    """

    get_calls: list[tuple[Any, ...]]
    records: list[VectorIndexRecord]

    def __init__(self) -> None:
        super().__init__()
        self.get_calls = []
        self.records = [
            record("chunk-1", "document-1", "version-1"),
            record("chunk-2", "document-2", "version-2"),
        ]

    def _results(self, collection: str) -> list[VectorSearchResult]:
        item = self.records[0]
        return [VectorSearchResult(id=item.id, score=0.9, raw_score=0.9, payload=item.payload)]

    async def get_records(
        self, collection: str, ids: Sequence[str], *, context: object
    ) -> list[VectorIndexRecord]:
        """Record the call and return the stored points among ``ids``."""
        self.get_calls.append((collection, ids, context))
        return [item for item in self.records if item.id in ids]


class ActiveVersions:
    """Active-version resolver for ``document-1`` and ``document-2``.

    Attributes:
        versions: Active version ID of each document.
    """

    versions: dict[str, str]

    def __init__(self) -> None:
        self.versions = {"document-1": "version-1", "document-2": "version-2"}

    async def active_versions(
        self, document_ids: Sequence[str]
    ) -> dict[str, ActiveDocumentVersion]:
        """Return the active version of each requested document that has one."""
        return {
            document: ActiveDocumentVersion(
                document_id=DocumentId(document),
                document_version_id=DocumentVersionId(self.versions[document]),
            )
            for document in document_ids
            if document in self.versions
        }


def service(
    topology: object,
    vectors: FakeVectorRepository | None = None,
    authority: object | None = None,
) -> RuntimeRetrievalService:
    """Build a retrieval service over ``topology`` and the topology vectors and versions."""
    return RuntimeRetrievalService(
        resources=replace(
            resources(
                vectors=vectors or TopologyVectors(), active_versions=authority or ActiveVersions()
            ),
            topology_repository=topology,  # type: ignore[arg-type]  # any topology double
        ),
        policy=policy(),
    )
