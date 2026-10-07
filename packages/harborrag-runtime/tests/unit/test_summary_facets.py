"""Facets: a closed, per-scope vocabulary copied from the source's own fields."""

from datetime import UTC, datetime

import pytest
from test_entity_summary_search import PROFILE, Embed, Vectors

from harborrag_core.chunking import ConnectorType
from harborrag_core.indexing import FilterOperator, VectorFilter, VectorFilterCondition
from harborrag_core.ingestion import (
    DocumentIdentityBuilder,
    GraphEntityType,
    GraphNodeRecord,
    GraphOwnershipScope,
    KnowledgeNodeKind,
)
from harborrag_core.schemas.ids import TenantId
from harborrag_core.storage import StorageOperationContext
from harborrag_core.summaries import (
    SummaryAttribute,
    SummaryBinding,
    SummaryCard,
    SummaryFacet,
    SummaryManifest,
    SummaryPolicy,
)
from harborrag_engine.ingestion.projections.graph.collaboration_source_projectors import (
    _issue_attributes,
)
from harborrag_runtime.config.graph_build import GraphBuildConfig
from harborrag_runtime.retrieval.entity_summary import (
    EntitySummarySearch,
    facet_filter_from_mapping,
    split_facet_filters,
)
from harborrag_runtime.topology.entity_summary_index import (
    EntitySummaryIndex,
    entity_point_id,
    facet_payload,
)
from harborrag_runtime.topology.summary_service import _with_facets

STAGE = SummaryFacet(name="stage", field="Stage")
YEARS = SummaryFacet(name="years_experience", field="customfield_10042", kind="integer")
SKILLS = SummaryFacet(name="skills", field="Skills")
POLICY = SummaryPolicy(model_fingerprint="model", facets=(STAGE, YEARS, SKILLS))


def test_the_config_declares_facets_per_scope_and_they_reach_the_policy() -> None:
    config = GraphBuildConfig.model_validate(
        {
            "tenants": [
                {
                    "tenant_id": "AUTA-2",
                    "sources": [
                        {
                            "source_scope_id": "AUTA-2",
                            "facets": [
                                {"name": "stage", "field": "Stage"},
                                {"name": "skills", "field": "Skills"},
                            ],
                        },
                        {"source_scope_id": "other"},
                    ],
                }
            ]
        },
        strict=True,
    )
    scoped, plain = config.tenants[0].sources
    assert [facet.as_policy_facet().name for facet in scoped.facets] == ["stage", "skills"]
    assert plain.facets == []
    # The vocabulary is model-affecting, so two scopes of one tenant may run under
    # different fingerprints -- and only the scope whose facets changed regenerates.
    with_facets = SummaryPolicy(
        model_fingerprint="m", facets=tuple(f.as_policy_facet() for f in scoped.facets)
    )
    assert with_facets.fingerprint != SummaryPolicy(model_fingerprint="m").fingerprint


@pytest.mark.parametrize(
    "payload",
    (
        {"name": "Stage", "field": "Stage"},  # not lower-case
        {"name": "stage"},  # copies nothing
        # The model-read facet source is gone; a config still declaring one fails
        # loudly instead of silently dropping the facet.
        {"name": "skills", "from": "extracted", "hint": "technologies"},
        {"name": "stage", "from": "source_field", "field": "Stage"},
    ),
)
def test_incoherent_facets_are_rejected_at_load(payload: dict[str, object]) -> None:
    with pytest.raises(Exception, match="pattern|String should match|Field required|Extra inputs"):
        GraphBuildConfig.model_validate(
            {
                "tenants": [
                    {"tenant_id": "T", "sources": [{"source_scope_id": "S", "facets": [payload]}]}
                ]
            },
            strict=True,
        )


def test_the_issue_node_carries_its_typed_custom_fields() -> None:
    attributes = _issue_attributes(
        {
            "issue_key": "AUTA-7",
            "status": "In Review",
            "typed_custom_attributes": [
                {"field_id": "customfield_10001", "name": "Stage", "text": "Round 2"},
                {"field_id": "customfield_10042", "name": "Years of Experience", "text": "7"},
                {"field_id": "customfield_10099", "name": "Empty", "text": ""},
            ],
        }
    )
    assert attributes["custom_fields"] == [
        {"field_id": "customfield_10001", "name": "Stage", "value": "Round 2"},
        {"field_id": "customfield_10042", "name": "Years of Experience", "value": "7"},
    ]
    # And the graph attribute contract accepts the shape.
    GraphNodeRecord(
        node_key="k",
        node_kind=KnowledgeNodeKind.SOURCE_ENTITY,
        entity_type=GraphEntityType.JIRA_ISSUE,
        logical_id="AUTA-7",
        ownership_scope=GraphOwnershipScope.SOURCE_SCOPE,
        owner_id=TenantId("DEFAULT"),
        source_scope_id="AUTA-2",
        attributes=attributes,
    )


ISSUE_DOCUMENT = str(
    DocumentIdentityBuilder().document_id(
        tenant_id="DEFAULT",
        connector_type=ConnectorType.JIRA,
        connection_id="c",
        source_item_id="AUTA-7",
    )
)


def _issue_node() -> GraphNodeRecord:
    # A source entity never carries a document id of its own -- the graph contract
    # reserves that for version-owned nodes -- which is why provenance is passed in.
    return GraphNodeRecord(
        node_key="node-7",
        node_kind=KnowledgeNodeKind.SOURCE_ENTITY,
        entity_type=GraphEntityType.JIRA_ISSUE,
        logical_id="AUTA-7",
        ownership_scope=GraphOwnershipScope.SOURCE_SCOPE,
        owner_id=TenantId("DEFAULT"),
        source_scope_id="AUTA-2",
        attributes={
            "custom_fields": [
                {"field_id": "customfield_10001", "name": "Stage", "value": "Round 2"},
                {"field_id": "customfield_10042", "name": "Years of Experience", "value": "7"},
            ]
        },
    )


def test_source_fields_are_copied_onto_the_card() -> None:
    node = _issue_node()
    card = SummaryCard(description="Senior engineer.")
    merged = _with_facets(card, node, POLICY, (ISSUE_DOCUMENT,))
    by_name = {item.name: item for item in merged.attributes}
    assert by_name["stage"].values == ("Round 2",)
    assert by_name["stage"].from_document_ids == (ISSUE_DOCUMENT,)
    # Matched by field id as well as by display name.
    assert by_name["years_experience"].values == ("7",)
    # The issue sets no Skills field, so that facet is absent rather than guessed.
    assert "skills" not in by_name
    assert merged.description == card.description
    # No declared facets: the card is untouched.
    assert _with_facets(card, node, SummaryPolicy(model_fingerprint="m"), ()) is card


def _binding(attributes: tuple[SummaryAttribute, ...]) -> SummaryBinding:
    card = SummaryCard(description="Candidate.", attributes=attributes)
    return SummaryBinding(
        manifest=SummaryManifest(
            node_key="node-7",
            kind="SourceEntity",
            source_scope_id="AUTA-2",
            input_chunk_ids=("chunk-1",),
            policy_fingerprint="p",
            membership_digest="m",
            input_digest="i",
        ),
        card=card,
        generation_key="g",
        artifact_hash=card.artifact_hash,
        revision=1,
        updated_at=datetime.now(UTC),
    )


def test_facets_land_as_typed_filterable_payload() -> None:
    binding = _binding(
        (
            SummaryAttribute(name="stage", values=("Round 2",)),
            SummaryAttribute(name="years_experience", values=("7 years",)),
            SummaryAttribute(name="skills", values=("Java", "Kubernetes")),
            SummaryAttribute(name="undeclared", values=("x",)),
        )
    )
    assert facet_payload(binding, POLICY.facets) == {
        "stage": ["round 2"],
        "years_experience": [7],
        "skills": ["java", "kubernetes"],
    }


@pytest.mark.asyncio
async def test_publication_indexes_each_declared_facet_under_the_dotted_path() -> None:
    vectors, embed = Vectors(), Embed()
    binding = _binding((SummaryAttribute(name="skills", values=("Java",)),))
    await EntitySummaryIndex(vectors, embed, PROFILE).publish(
        "DEFAULT", "AUTA-2", (binding,), POLICY.facets
    )
    point = vectors.records[PROFILE.entity_index_name][
        entity_point_id("DEFAULT", "node-7", PROFILE.entity_fingerprint)
    ]
    assert point.payload["facet"] == {"skills": ["java"]}
    # Filterable means indexed: the spec asked for a payload index per facet.
    assert vectors.specs == [PROFILE.entity_index_name]


def test_facet_filters_are_routed_to_the_entity_lane_and_off_the_chunks() -> None:
    mixed = VectorFilter(
        must=[
            VectorFilterCondition(field="facet.stage", value="round 2"),
            VectorFilterCondition(field="issue_key", value="AUTA-7"),
        ]
    )
    facet, rest = split_facet_filters(mixed)
    assert [c.field for c in facet.must] == ["facet.stage"]
    assert [c.field for c in rest.must] == ["issue_key"]
    assert split_facet_filters(None) == (None, None)
    only_chunks = VectorFilter(must=[VectorFilterCondition(field="issue_key", value="x")])
    assert split_facet_filters(only_chunks) == (None, only_chunks)


def test_a_facet_mapping_becomes_case_folded_and_ranged_conditions() -> None:
    built = facet_filter_from_mapping(
        {"stage": "Round 2", "skills": ["Java", "K8s"], "years_experience": {"gte": 5}},
        ("AUTA-2",),
    )
    conditions = {(c.field, c.operator): c.value for c in built.must}
    assert conditions[("facet.stage", FilterOperator.EQUALS)] == "round 2"
    assert conditions[("facet.skills", FilterOperator.IN)] == ["java", "k8s"]
    assert conditions[("facet.years_experience", FilterOperator.GREATER_THAN_OR_EQUAL)] == 5
    assert conditions[("source_scope_id", FilterOperator.IN)] == ["AUTA-2"]
    assert facet_filter_from_mapping({}) is None
    with pytest.raises(ValueError, match="not supported"):
        facet_filter_from_mapping({"years_experience": {"between": 5}})


class Summaries:
    def __init__(self, released: dict[str, tuple[str, ...]]):
        self.released = released

    async def entity_evidence(self, tenant_id, node_keys, *, access):
        return {key: self.released[key] for key in node_keys if key in self.released}

    async def views(self, *args, **kwargs):
        return {}


@pytest.mark.asyncio
async def test_with_a_facet_filter_the_lane_selects_instead_of_enriching() -> None:
    from test_entity_summary_search import entity_hit, evidence_record

    chunk = "chunk-1"
    vectors = Vectors(
        records={
            PROFILE.entity_index_name: {},
            "evidence": {evidence_record(chunk).id: evidence_record(chunk)},
        },
        hits=(entity_hit("node-a"),),
    )
    search = EntitySummarySearch(Summaries({"node-a": (chunk,)}), vectors, PROFILE)
    context = StorageOperationContext.system("DEFAULT")
    unrelated = VectorFilter(must=[VectorFilterCondition(field="facet.stage", value="round 2")])
    existing = (evidence_record("chunk-other"),)
    from harborrag_core.indexing import VectorSearchResult

    prior = tuple(
        VectorSearchResult(id=row.id, score=1.0, raw_score=1.0, payload=row.payload)
        for row in existing
    )
    # Enrichment: prior chunk candidates survive alongside the entity's evidence.
    fused = await search.expand(prior, (1.0, 0.0), context=context)
    assert {str(item.payload["chunk_id"]) for item in fused} == {"chunk-other", chunk}
    # Selection: only evidence from entities that passed the facet filter remains.
    selected = await search.expand(prior, (1.0, 0.0), context=context, facet_filter=unrelated)
    assert [str(item.payload["chunk_id"]) for item in selected] == [chunk]
    # Selection with nothing to embed yields nothing rather than the unfiltered set.
    assert await search.expand(prior, None, context=context, facet_filter=unrelated) == ()


def test_a_real_issue_observation_beats_the_placeholder_an_attachment_projects() -> None:
    """Every attachment document projects its parent issue as a placeholder stub under
    the same node key. The summary must read the issue's own observation, whichever
    document the loader happened to read first."""

    from harborrag_runtime.topology.summary_inputs import _node_rank

    real = _issue_node()
    stub = real.model_copy(update={"title": "CPM-147848", "attributes": {"placeholder": True}})
    thinner = real.model_copy(update={"attributes": {}})
    assert _node_rank(real) < _node_rank(stub)
    assert _node_rank(real) < _node_rank(thinner)
    assert _node_rank(thinner) < _node_rank(stub)
    # Ties between equal observations stay order-independent.
    assert _node_rank(real) == _node_rank(real.model_copy())


def test_a_source_field_facet_may_name_a_standard_issue_attribute() -> None:
    node = _issue_node().model_copy(
        update={
            "attributes": {**_issue_node().attributes, "status": "Placed", "labels": ["a", "b"]}
        }
    )
    policy = SummaryPolicy(
        model_fingerprint="m",
        facets=(
            SummaryFacet(name="stage", field="status"),
            SummaryFacet(name="tags", field="labels"),
            # No field by this name on the node: the facet stays absent, never guessed.
            SummaryFacet(name="absent", field="Notice Period"),
        ),
    )
    card = _with_facets(SummaryCard(description="c."), node, policy, (ISSUE_DOCUMENT,))
    by_name = {item.name: item for item in card.attributes}
    assert by_name["stage"].values == ("Placed",)
    assert by_name["tags"].values == ("a, b",)
    assert set(by_name) == {"stage", "tags"}


def test_integer_facets_read_the_number_not_the_digits() -> None:
    from harborrag_runtime.topology.entity_summary_index import _integer

    assert _integer("200.0") == 200
    assert _integer("5+ years") == 5
    assert _integer("7") == 7
    assert _integer("-3.9") == -3
    assert _integer("seven") is None


def _legacy_policy_row() -> dict[str, object]:
    """A scope row's policy as stored before facets became source-field only.

    This is the shape that stopped the worker: every facet tagged with ``source``
    and ``hint``, and the model-read ones naming no field at all.
    """

    current = SummaryPolicy(
        model_fingerprint="model",
        facets=(
            SummaryFacet(name="stage", field="status"),
            SummaryFacet(name="years_experience", field="Years of experience", kind="integer"),
        ),
    ).model_dump(mode="json")
    facets = [{**facet, "source": "source_field", "hint": None} for facet in current["facets"]]
    facets.append(
        {
            "name": "skills",
            "source": "extracted",
            "field": None,
            "hint": "distinct technologies the candidate has used",
            "kind": "text",
        }
    )
    return {**current, "facets": facets}


def test_a_policy_stored_with_model_read_facets_still_parses() -> None:
    from harborrag_core.summaries import SummaryLease

    stored = _legacy_policy_row()

    policy = SummaryPolicy.model_validate(stored)

    assert [facet.name for facet in policy.facets] == ["stage", "years_experience"]
    assert policy.facets[1].kind == "integer"
    # A lease is built straight from the row, which is where the worker failed.
    lease = SummaryLease(
        tenant_id="AUTA-3",
        source_scope_id="AUTA-3",
        revision=1,
        fence=1,
        policy=stored,
        lease_until=datetime.now(UTC),
    )
    assert lease.policy == policy
    # The upgraded form differs from the row, so ``configure`` rewrites the row
    # and an in-flight lease on the old one is treated as superseded.
    assert policy.model_dump(mode="json") != stored


def test_a_card_stored_with_tagged_attributes_is_verified_then_upgraded() -> None:
    """The stored hash is checked against the card as written, then re-derived."""

    legacy_card = {
        "description": "Candidate.",
        "topics": [],
        "key_entities": [],
        "content_types": [],
        "attributes": [
            {
                "name": "stage",
                "values": ["Placed"],
                "from_document_ids": [ISSUE_DOCUMENT],
                "source": "source_field",
            },
            {"name": "skills", "values": ["java"], "from_document_ids": [], "source": "extracted"},
        ],
    }
    from harborrag_core.summary_cards import card_digest

    stored = _binding(()).model_dump(mode="json")
    stored = {**stored, "card": legacy_card, "artifact_hash": card_digest(legacy_card)}

    binding = SummaryBinding.model_validate(stored)

    assert [item.name for item in binding.card.attributes] == ["stage"]
    assert binding.artifact_hash == binding.card.artifact_hash
    # Tampering is still caught: the check runs against the stored form.
    with pytest.raises(Exception, match="hash mismatch"):
        SummaryBinding.model_validate({**stored, "artifact_hash": "0" * 64})
