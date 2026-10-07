"""A run generates cards in dependency waves and skips the model where it adds nothing."""

from harborrag_core.chunking import (
    ChunkKind,
    ChunkRecord,
    ChunkSecurity,
    ConnectorType,
    DocumentKind,
    RecordKind,
)
from harborrag_core.ingestion import (
    GraphEntityType,
    GraphNodeRecord,
    GraphOwnershipScope,
    KnowledgeNodeKind,
)
from harborrag_core.schemas.ids import TenantId
from harborrag_core.summaries import CardWordBudgets, SummaryCard, SummaryFacet, SummaryPolicy
from harborrag_engine.topology.summary_planner import SummaryPlanNode
from harborrag_runtime.topology.summary_service import _shortcut, _waves

POLICY = SummaryPolicy(model_fingerprint="m")


def node(key: str, kind: KnowledgeNodeKind, entity: GraphEntityType) -> GraphNodeRecord:
    version_owned = kind in {KnowledgeNodeKind.STRUCTURE, KnowledgeNodeKind.DOCUMENT_VERSION}
    return GraphNodeRecord(
        node_key=key,
        node_kind=kind,
        entity_type=entity,
        logical_id=key,
        ownership_scope=GraphOwnershipScope.DOCUMENT_VERSION
        if version_owned
        else GraphOwnershipScope.SOURCE_SCOPE,
        owner_id=TenantId("DEFAULT"),
        source_scope_id="scope",
        document_id="document:d1" if version_owned else None,
        document_version_id="document-version:v1" if version_owned else None,
    )


def chunk(text: str, chunk_id: str = "chunk-1") -> ChunkRecord:
    return ChunkRecord(
        strategy_version="c1",
        logical_chunk_id="logical-1",
        chunk_id=chunk_id,
        connector_type=ConnectorType.LOCAL,
        document_kind=DocumentKind.LOCAL_FILE,
        record_kind=RecordKind.EVIDENCE,
        chunk_kind=ChunkKind.TEXT,
        tenant_id="DEFAULT",
        connection_id="files",
        source_scope_id="scope",
        source_item_id="item",
        source_version="one",
        document_id="document:d1",
        document_version_id="document-version:v1",
        ordinal=0,
        content=text,
        embedding_text=text,
        search_text=text,
        content_hash="h",
        token_count=5,
        security=ChunkSecurity(permission_set_id="permission-set:public"),
    )


def plan_node(key, kind, entity, *, children=(), direct=()):
    return SummaryPlanNode(
        node(key, kind, entity),
        kind_name(kind),
        tuple(children),
        tuple(direct),
        ("chunk-1",),
        ("document:d1",),
    )


def kind_name(kind: KnowledgeNodeKind) -> str:
    return {
        KnowledgeNodeKind.STRUCTURE: "Structure",
        KnowledgeNodeKind.DOCUMENT_VERSION: "DocumentVersion",
        KnowledgeNodeKind.SOURCE_ENTITY: "SourceEntity",
        KnowledgeNodeKind.DATA_SOURCE: "DataSource",
    }[kind]


def test_waves_put_every_child_in_an_earlier_wave_than_its_parent() -> None:
    section = plan_node("s1", KnowledgeNodeKind.STRUCTURE, GraphEntityType.SECTION)
    other_doc = plan_node(
        "d2", KnowledgeNodeKind.DOCUMENT_VERSION, GraphEntityType.DOCUMENT_VERSION
    )
    document = plan_node(
        "d1", KnowledgeNodeKind.DOCUMENT_VERSION, GraphEntityType.DOCUMENT_VERSION, children=("s1",)
    )
    entity = plan_node(
        "e1", KnowledgeNodeKind.SOURCE_ENTITY, GraphEntityType.JIRA_ISSUE, children=("d1", "d2")
    )
    source = plan_node(
        "ds", KnowledgeNodeKind.DATA_SOURCE, GraphEntityType.DATA_SOURCE, children=("e1",)
    )
    waves = _waves((section, other_doc, document, entity, source))
    keys = [tuple(item.node.node_key for item in wave) for wave in waves]
    # Leaves share a wave regardless of kind; parents strictly follow their children.
    assert keys == [("s1", "d2"), ("d1",), ("e1",), ("ds",)]


def test_a_single_child_node_inherits_its_childs_card_without_a_model_call() -> None:
    child = SummaryCard(description="The CV.", topics=("java",))
    entity = plan_node(
        "e1", KnowledgeNodeKind.SOURCE_ENTITY, GraphEntityType.JIRA_ATTACHMENT, children=("d1",)
    )
    result = _shortcut("DEFAULT", POLICY, entity, (), {"d1": child})
    assert result is not None
    card, key = result
    assert card is child and key.startswith("")
    # Two children: a real composition, which is the model's job.
    two = plan_node(
        "e2", KnowledgeNodeKind.SOURCE_ENTITY, GraphEntityType.JIRA_ISSUE, children=("d1", "d2")
    )
    assert _shortcut("DEFAULT", POLICY, two, (), {"d1": child, "d2": child}) is None


def test_content_that_already_fits_the_budget_is_the_card() -> None:
    section = plan_node(
        "s1",
        KnowledgeNodeKind.STRUCTURE,
        GraphEntityType.SECTION,
        direct=(chunk("Cleared round two."),),
    )
    result = _shortcut("DEFAULT", POLICY, section, section.direct_chunks, {})
    assert result is not None and result[0].description == "Cleared round two."
    # Over the budget: the model has to compress.
    long = plan_node(
        "s2", KnowledgeNodeKind.STRUCTURE, GraphEntityType.SECTION, direct=(chunk("word " * 80),)
    )
    assert _shortcut("DEFAULT", POLICY, long, long.direct_chunks, {}) is None
    # A leaf with several short children concatenates them, still without a model.
    document = plan_node(
        "d1",
        KnowledgeNodeKind.DOCUMENT_VERSION,
        GraphEntityType.DOCUMENT_VERSION,
        children=("s1", "s3"),
    )
    cards = {"s1": SummaryCard(description="First."), "s3": SummaryCard(description="Second.")}
    result = _shortcut("DEFAULT", POLICY, document, (), cards)
    assert result is not None and result[0].description == "First.\n\nSecond."
    # An entity with direct content and several children is never a shortcut: the
    # dossier and its facets are exactly what the model is for.
    issue = plan_node(
        "e1",
        KnowledgeNodeKind.SOURCE_ENTITY,
        GraphEntityType.JIRA_ISSUE,
        children=("d1", "d2"),
        direct=(chunk("Short."),),
    )
    assert (
        _shortcut(
            "DEFAULT", POLICY, issue, issue.direct_chunks, {"d1": cards["s1"], "d2": cards["s3"]}
        )
        is None
    )


def test_leaf_cards_are_keyed_without_facets_so_a_facet_edit_leaves_them_cached() -> None:
    plain = SummaryPolicy(model_fingerprint="m")
    faceted = plain.model_copy(update={"facets": (SummaryFacet(name="skills", field="Skills"),)})
    assert plain.fingerprint != faceted.fingerprint
    assert plain.leaf_fingerprint == faceted.leaf_fingerprint
    # Scheduling knobs never move either fingerprint.
    assert plain.model_copy(update={"max_concurrency": 9}).fingerprint == plain.fingerprint
    assert (
        plain.model_copy(update={"card_words": CardWordBudgets(source_entity=300)}).leaf_fingerprint
        != plain.leaf_fingerprint
    )
