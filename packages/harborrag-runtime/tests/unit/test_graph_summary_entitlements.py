"""Graph, summary, topology and contextual reads stay inside the reader's entitled datasources."""

from __future__ import annotations

from dataclasses import replace

import pytest
from retrieval_test_support import policy, resources
from test_contextual_evidence_search import PROFILE, query
from test_contextual_evidence_search import Authority as DerivedAuthority
from test_contextual_evidence_search import Vectors as DerivedVectors
from test_entity_summary_search import PROFILE as ENTITY_PROFILE
from test_entity_summary_search import Vectors as EntityVectors
from test_entity_summary_search import entity_hit
from topology_retrieval_support import (
    ActiveVersions,
    CanonicalTopology,
    TopologyVectors,
    mention,
    record,
)

from harborrag_core.chunking import RelationType
from harborrag_core.contracts.errors import HarborAuthorizationUnavailableError
from harborrag_core.contracts.reader import EntityFindRequest
from harborrag_core.domain.retrieval import RetrievalResult
from harborrag_core.indexing import FilterOperator, VectorSearchResult
from harborrag_core.ingestion import (
    GraphEdgeRecord,
    GraphEntityType,
    GraphNodeRecord,
    GraphOwnershipScope,
    KnowledgeNodeKind,
)
from harborrag_core.security import AccessContext
from harborrag_core.storage import StorageOperationContext
from harborrag_core.summary_cards import SummaryCard, SummaryView
from harborrag_core.topology.extraction import EvidenceSpan, ExtractedAssertion
from harborrag_core.topology.records import CanonicalAssertion
from harborrag_core.topology.search import EvidencePath, RetrievalMode, TopologyEvidence
from harborrag_engine.retrieval.graph_visibility import apply_graph_permissions
from harborrag_runtime.retrieval import RetrievalOptions, RuntimeRetrievalService
from harborrag_runtime.retrieval.contextual import ContextualEvidenceSearch
from harborrag_runtime.retrieval.entity_summary import EntitySummarySearch
from harborrag_runtime.retrieval.permissions import RetrievalPermissions
from harborrag_runtime.retrieval.summary_views import apply_summary_views
from harborrag_runtime.retrieval.topology import TopologyCandidates
from harborrag_runtime.retrieval.topology_visibility import restrict_to_readable_documents

ENGINEERING = "scope-engineering"
HR = "scope-hr"
TENANT = "tenant-1"


def access(entitled: frozenset[str] | None, *, corpus_mode: str = "tenant_shared") -> AccessContext:
    return AccessContext(
        principal_id="reader",
        tenant_id=TENANT,
        corpus_mode=corpus_mode,  # type: ignore[arg-type]
        entitled_source_scopes=entitled,
    )


def context_for(held: AccessContext) -> StorageOperationContext:
    return StorageOperationContext.for_access(
        held, operation_kind="retrieval", idempotency_key="test"
    )


# --- summary cards ---------------------------------------------------------------------


class Cards:
    """A summary reader that releases a card for every key it is asked about.

    It stands for any reader that does not apply the entitlement itself, so the only thing
    keeping a card back is not asking for it.
    """

    def __init__(self) -> None:
        self.asked: list[tuple[str, ...]] = []

    async def views(self, tenant_id, node_keys, *, access, source_scopes=None):
        self.asked.append(tuple(node_keys))
        return {
            key: SummaryView(
                status="current",
                card=SummaryCard(description=f"Card of {key}.", topics=("topic",)),
            )
            for key in node_keys
        }

    async def entity_evidence(self, tenant_id, node_keys, *, access):  # pragma: no cover
        return {}


def source_node(key: str, scope: str) -> GraphNodeRecord:
    return GraphNodeRecord(
        node_key=key,
        node_kind=KnowledgeNodeKind.SOURCE_ENTITY,
        entity_type=GraphEntityType.CONFLUENCE_PAGE,
        logical_id=key,
        ownership_scope=GraphOwnershipScope.SOURCE_SCOPE,
        owner_id=TENANT,
        source_scope_id=scope,
        title=key,
    )


def tenant_node(key: str = "tenant-node") -> GraphNodeRecord:
    return GraphNodeRecord(
        node_key=key,
        node_kind=KnowledgeNodeKind.TENANT,
        entity_type=GraphEntityType.TENANT,
        logical_id=TENANT,
        ownership_scope=GraphOwnershipScope.TENANT,
        owner_id=TENANT,
        title=TENANT,
    )


@pytest.mark.asyncio
async def test_tenant_card_hidden_for_a_narrowed_reader() -> None:
    """The tenant-wide card summarises every datasource, so a narrowed reader gets no card."""

    reader = Cards()
    nodes = (tenant_node(), source_node("eng-entity", ENGINEERING))

    narrowed = await apply_summary_views(nodes, reader, access(frozenset({ENGINEERING})))

    by_key = {node.node_key: node for node in narrowed}
    tenant = by_key["tenant-node"]
    assert tenant.title == TENANT
    assert tenant.summary is None
    assert tenant.description == nodes[0].description
    assert by_key["eng-entity"].summary is not None
    assert reader.asked == [("eng-entity",)]


@pytest.mark.asyncio
async def test_tenant_card_still_served_without_an_entitlement_set() -> None:
    reader = Cards()

    ungated = await apply_summary_views((tenant_node(),), reader, access(None))

    assert ungated[0].summary is not None
    assert ungated[0].summary.card is not None
    assert reader.asked == [("tenant-node",)]


@pytest.mark.asyncio
async def test_scope_cards_only_for_entitled_datasources() -> None:
    reader = Cards()
    nodes = (source_node("eng-entity", ENGINEERING), source_node("hr-entity", HR))

    both = await apply_summary_views(nodes, reader, access(frozenset({ENGINEERING, HR})))
    engineering = await apply_summary_views(nodes, reader, access(frozenset({ENGINEERING})))

    assert [node.summary is not None for node in both] == [True, True]
    assert [node.node_key for node in engineering if node.summary is not None] == ["eng-entity"]
    hr_node = next(node for node in engineering if node.node_key == "hr-entity")
    assert hr_node.summary is None and hr_node.description == nodes[1].description
    # The reader was never asked for a card the entitlement would refuse.
    assert reader.asked == [("eng-entity", "hr-entity"), ("eng-entity",)]


class RecordingAuthority:
    """Records what entity search asks of the summary authority, and releases everything."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, tuple[str, ...], AccessContext, object]] = []

    async def entity_evidence(self, tenant_id, node_keys, *, access):
        self.calls.append(("evidence", node_keys, access, None))
        return dict.fromkeys(node_keys, ("chunk-1",))

    async def views(self, tenant_id, node_keys, *, access, source_scopes=None):
        self.calls.append(("views", node_keys, access, source_scopes))
        return {
            key: SummaryView(
                status="current",
                card=SummaryCard(description="Candidate card.", topics=("hiring",)),
            )
            for key in node_keys
        }


@pytest.mark.asyncio
async def test_find_reads_entity_cards_under_the_requests_entitlement() -> None:
    """Entity cards come from the same ``views`` read as graph cards, under the same access.

    The datasource rule is applied inside that read (see the adapters test of it), so what
    ``find`` owes is to ask with the request's own access and nothing wider.
    """

    vectors = EntityVectors(
        records={ENTITY_PROFILE.entity_index_name: {}}, hits=(entity_hit("node-a"),)
    )
    authority = RecordingAuthority()
    request = EntityFindRequest(access=access(frozenset({ENGINEERING})), query="java")

    response = await EntitySummarySearch(authority, vectors, ENTITY_PROFILE).find(
        request, "entities-x", (1.0, 0.0), context_for(request.access)
    )

    assert [match.node_key for match in response.matches] == ["node-a"]
    assert {call[0] for call in authority.calls} == {"evidence", "views"}
    assert all(call[2] is request.access for call in authority.calls)
    assert all(call[3] is None for call in authority.calls)


# --- graph post-check ------------------------------------------------------------------

DOCUMENT_SCOPES = {"doc-eng": ENGINEERING, "doc-hr": HR}


class EntitledAuthorizer:
    """A ``GraphVisibilityAuthorizer`` answering by Task 2's rule.

    Every document and source is readable to the principal; what narrows the answer is the
    datasource set the access context carries.
    """

    @staticmethod
    def _scopes(access: AccessContext) -> set[str]:
        entitled = access.entitled_source_scopes
        return set(DOCUMENT_SCOPES.values()) if entitled is None else set(entitled)

    async def allowed_document_ids(self, tenant_id, *, access, limit=10000):
        scopes = self._scopes(access)
        return tuple(doc for doc, scope in DOCUMENT_SCOPES.items() if scope in scopes)

    async def allowed_source_scope_ids(self, tenant_id, *, access, limit=10000):
        return tuple(sorted(self._scopes(access)))

    async def authorized_document_ids(self, tenant_id, document_ids, *, access):
        return set(document_ids) & set(await self.allowed_document_ids(tenant_id, access=access))

    async def authorized_source_scope_ids(self, tenant_id, source_scope_ids, *, access):
        return set(source_scope_ids) & self._scopes(access)


def owned_by_document(key: str, document: str) -> GraphNodeRecord:
    return GraphNodeRecord(
        node_key=key,
        node_kind=KnowledgeNodeKind.DOCUMENT_VERSION,
        entity_type=GraphEntityType.DOCUMENT_VERSION,
        logical_id=key,
        ownership_scope=GraphOwnershipScope.DOCUMENT_VERSION,
        owner_id=TENANT,
        source_scope_id=DOCUMENT_SCOPES[document],
        document_id=document,
        document_version_id=f"{document}-v1",
        title=key,
    )


def edge_in(scope: str, source: GraphNodeRecord, target: GraphNodeRecord) -> GraphEdgeRecord:
    return GraphEdgeRecord(
        relation_id=f"edge-{source.node_key}-{target.node_key}",
        relation_type=RelationType.HAS_DATA_SOURCE,
        source_node_key=source.node_key,
        target_node_key=target.node_key,
        ownership_scope=GraphOwnershipScope.SOURCE_SCOPE,
        owner_id=TENANT,
        source_scope_id=scope,
        source_relation_version="v1",
        source_explicit=False,
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("corpus_mode", ["tenant_shared", "source_acl"])
async def test_graph_post_check_drops_records_of_other_datasources(corpus_mode: str) -> None:
    eng_source, hr_source = source_node("eng-source", ENGINEERING), source_node("hr-source", HR)
    eng_doc, hr_doc = owned_by_document("eng-doc", "doc-eng"), owned_by_document("hr-doc", "doc-hr")
    records = {
        "eng-source": eng_source,
        "hr-source": hr_source,
        "eng-doc": eng_doc,
        "hr-doc": hr_doc,
        "eng-edge": edge_in(ENGINEERING, eng_source, eng_doc),
        "hr-edge": edge_in(HR, hr_source, hr_doc),
        "tenant": tenant_node("tenant"),
    }

    states = await apply_graph_permissions(
        records,
        dict.fromkeys(records, "active"),
        EntitledAuthorizer(),  # type: ignore[arg-type]
        context_for(access(frozenset({ENGINEERING}), corpus_mode=corpus_mode)),
    )

    assert states == {
        "eng-source": "active",
        "hr-source": "denied",
        "eng-doc": "active",
        "hr-doc": "denied",
        "eng-edge": "active",
        "hr-edge": "denied",
        # Tenant nodes keep their names; only their cards are held back (see above).
        "tenant": "active",
    }


# --- contextual lane -------------------------------------------------------------------


def scope_condition(*scopes: str):
    return ("source_scope_id", FilterOperator.IN, list(scopes))


def conditions(search_call) -> list[tuple[str, FilterOperator, object]]:
    scoped, _ = search_call
    return [(item.field, item.operator, item.value) for item in scoped.filters.must]


@pytest.mark.asyncio
@pytest.mark.parametrize("parent", [False, True])
async def test_contextual_lane_ranks_inside_the_entitled_datasources(parent: bool) -> None:
    authority, vectors = DerivedAuthority(parent=parent), DerivedVectors(parent=parent)

    await ContextualEvidenceSearch(authority, vectors, PROFILE).search(
        query(), context=context_for(access(frozenset({HR, ENGINEERING})))
    )

    must = conditions(vectors.search_calls[0])
    assert must[0][0] == "projection_point_id"
    assert must[-1] == scope_condition(ENGINEERING, HR)


@pytest.mark.asyncio
async def test_contextual_lane_is_unrestricted_without_an_entitlement_set() -> None:
    authority, vectors = DerivedAuthority(), DerivedVectors()

    await ContextualEvidenceSearch(authority, vectors, PROFILE).search(
        query(), context=context_for(access(None))
    )

    assert "source_scope_id" not in {field for field, _, _ in conditions(vectors.search_calls[0])}


# --- topology metadata -----------------------------------------------------------------

TOPOLOGY_DOCUMENT_SCOPES = {"document-1": ENGINEERING, "document-2": ENGINEERING, "document-hr": HR}


class EntitledTopology(CanonicalTopology):
    """Canonical topology as ``build_permissions_current(serving=True)`` serves it.

    Mentions and assertions come back for every accepted build whatever the reader's
    datasource set is; only the document check knows about datasources. The one assertion
    is owned by the HR document and joins two entities that engineering chunks mention.
    """

    def __init__(self) -> None:
        super().__init__()
        self.mentions = (
            mention("entity-1", "chunk-1", "Alpha"),
            mention("entity-2", "chunk-2", "Beta", owner=("build-2", "document-2", "version-2")),
        )
        self.assertions = (
            CanonicalAssertion(
                assertion_id="assertion-hr",
                tenant_id=TENANT,
                build_id="build-hr",
                document_id="document-hr",
                document_version_id="version-hr",
                chunk_id="chunk-hr",
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
        self.eligible = {"build-1", "build-2", "build-hr"}
        self.document_checks: list[tuple[str, ...]] = []

    @staticmethod
    def _readable(access: AccessContext) -> set[str]:
        entitled = access.entitled_source_scopes
        return {
            document
            for document, scope in TOPOLOGY_DOCUMENT_SCOPES.items()
            if entitled is None or scope in entitled
        }

    async def allowed_document_ids(self, tenant_id, *, access, limit=10000):
        return tuple(sorted(self._readable(access)))

    async def authorized_document_ids(self, tenant_id, document_ids, *, access):
        self.document_checks.append(tuple(document_ids))
        return set(document_ids) & self._readable(access)


class TouchingHr(TopologyVectors):
    """Vectors that also hold the HR passage the assertion points at."""

    def __init__(self) -> None:
        super().__init__()
        hr = record("chunk-hr", "document-hr", "version-hr")
        self.records.append(
            hr.model_copy(update={"payload": {**hr.payload, "content": "Alpha depends on Beta."}})
        )


class TouchingHrVersions(ActiveVersions):
    def __init__(self) -> None:
        super().__init__()
        self.versions["document-hr"] = "version-hr"


class HrNavigation:
    """A contextual lane whose only hit is a navigation summary of the HR passage."""

    def __init__(self, vectors: TouchingHr) -> None:
        self.record = vectors.records[2]

    async def search(self, query, *, context):
        return (
            (
                VectorSearchResult(
                    id=self.record.id, score=0.8, raw_score=0.8, payload=self.record.payload
                ),
                TopologyEvidence(
                    "chunk-hr",
                    "document-hr",
                    "version-hr",
                    ("build-hr",),
                    navigation_summaries=(
                        {"parent_key": "hr-handbook", "description": "HR handbook navigation"},
                    ),
                ),
            ),
        )


def local_semantic_service(topology: EntitledTopology) -> RuntimeRetrievalService:
    vectors = TouchingHr()
    return RuntimeRetrievalService(
        resources=replace(
            resources(vectors=vectors, active_versions=TouchingHrVersions()),
            topology_repository=topology,
            contextual_search=HrNavigation(vectors),
        ),
        policy=policy(),
    )


def assertion_documents(results: tuple[RetrievalResult, ...]) -> set[str]:
    return {
        str(item["document_id"])
        for result in results
        for item in result.metadata.get("qualified_assertions", ())
    }


@pytest.mark.asyncio
async def test_retrieval_topology_metadata_stays_in_entitled_datasources() -> None:
    topology = EntitledTopology()

    report = await local_semantic_service(topology).retrieve(
        "dependencies",
        tenant_id=TENANT,
        top_k=3,
        options=RetrievalOptions(mode=RetrievalMode.LOCAL_SEMANTIC),
        access=access(frozenset({ENGINEERING})),
    )

    # The engineering passage reached through the HR assertion is still served.
    assert "chunk-2" in {item.id for item in report.results}
    assert not {item.id for item in report.results} & {"chunk-hr"}
    # What is served says nothing about the HR document.
    assert "document-hr" not in assertion_documents(report.results)
    assert all(item.document_id != "document-hr" for item in report.evidence.relevant_assertions)
    assert all(
        "assertion-hr" not in result.metadata.get("topology_assertion_ids", ())
        for result in report.results
    )
    assert all("assertion-hr" not in path.assertion_ids for path in report.evidence.evidence_paths)
    assert not report.evidence.navigation_summaries
    assert not any(result.metadata.get("navigation_summaries") for result in report.results)


@pytest.mark.asyncio
async def test_topology_metadata_keeps_every_datasource_for_an_ungated_reader() -> None:
    """The same retrieval with no entitlement set still explains itself with the HR assertion."""

    topology = EntitledTopology()

    report = await local_semantic_service(topology).retrieve(
        "dependencies",
        tenant_id=TENANT,
        top_k=3,
        options=RetrievalOptions(mode=RetrievalMode.LOCAL_SEMANTIC),
        access=access(None),
    )

    assert "document-hr" in assertion_documents(report.results)
    assert {item.document_id for item in report.evidence.relevant_assertions} == {"document-hr"}


def evidence_of(
    chunk: str, document: str, *, assertions=(), navigation=(), paths=()
) -> TopologyEvidence:
    return TopologyEvidence(
        chunk,
        document,
        f"{document}-v1",
        ("build-1",),
        assertion_ids=tuple(item.assertion_id for item in assertions),
        paths=paths,
        assertions=assertions,
        navigation_summaries=navigation,
    )


@pytest.mark.asyncio
async def test_restriction_drops_what_names_an_unreadable_document_and_keeps_the_rest() -> None:
    hr_assertion = EntitledTopology().assertions[0]
    eng_assertion = hr_assertion.model_copy(
        update={
            "assertion_id": "assertion-eng",
            "document_id": "document-1",
            "build_id": "build-1",
        }
    )
    through_hr = EvidencePath("chunk-1", "chunk-2", ("entity-1", "entity-2"), ("assertion-hr",))
    through_eng = EvidencePath("chunk-1", "chunk-2", ("entity-1", "entity-2"), ("assertion-eng",))
    incidence = EvidencePath("chunk-1", "chunk-2", ("entity-1",))
    navigation = ({"parent_key": "handbook", "description": "Navigation"},)
    batch = TopologyCandidates(
        flat=(),
        evidence={
            "both": evidence_of(
                "both",
                "document-1",
                assertions=(hr_assertion, eng_assertion),
                paths=(through_hr, through_eng, incidence),
                navigation=navigation,
            ),
            "hr-passage": evidence_of("hr-passage", "document-hr", navigation=navigation),
        },
    )
    topology = EntitledTopology()

    restricted = await restrict_to_readable_documents(
        batch,
        RetrievalPermissions(topology),  # type: ignore[arg-type]
        context_for(access(frozenset({ENGINEERING}))),
    )

    both = restricted.evidence["both"]
    assert [item.assertion_id for item in both.assertions] == ["assertion-eng"]
    assert both.assertion_ids == ("assertion-eng",)
    assert both.paths == (through_eng, incidence)
    assert both.navigation_summaries == navigation
    # A navigation summary follows the document of the passage it rides on.
    assert restricted.evidence["hr-passage"].navigation_summaries == ()
    # One canonical read answers for the whole batch.
    assert topology.document_checks == [("document-1", "document-hr")]


@pytest.mark.asyncio
async def test_restriction_reads_nothing_without_an_entitlement_set() -> None:
    hr_assertion = EntitledTopology().assertions[0]
    batch = TopologyCandidates(
        flat=(),
        evidence={"chunk-2": evidence_of("chunk-2", "document-2", assertions=(hr_assertion,))},
    )
    topology = EntitledTopology()

    restricted = await restrict_to_readable_documents(
        batch,
        RetrievalPermissions(topology),  # type: ignore[arg-type]
        context_for(access(None)),
    )

    assert restricted is batch
    assert topology.document_checks == []


@pytest.mark.asyncio
async def test_an_unavailable_document_check_fails_the_retrieval_closed() -> None:
    class Unavailable(EntitledTopology):
        async def authorized_document_ids(self, tenant_id, document_ids, *, access):
            raise ConnectionError("unavailable")

    with pytest.raises(HarborAuthorizationUnavailableError):
        await local_semantic_service(Unavailable()).retrieve(
            "dependencies",
            tenant_id=TENANT,
            top_k=3,
            options=RetrievalOptions(mode=RetrievalMode.LOCAL_SEMANTIC),
            access=access(frozenset({ENGINEERING})),
        )
