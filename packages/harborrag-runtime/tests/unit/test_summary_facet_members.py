"""A multi-valued field becomes one facet value per member, as on evidence."""

from __future__ import annotations

from test_summary_facets import ISSUE_DOCUMENT, _binding

from harborrag_adapters.connectors.jira.content import custom_field_metadata, filterable_fields
from harborrag_core.ingestion import (
    GraphEntityType,
    GraphNodeRecord,
    GraphOwnershipScope,
    KnowledgeNodeKind,
)
from harborrag_core.schemas.ids import TenantId
from harborrag_core.summaries import SummaryCard, SummaryFacet, SummaryPolicy
from harborrag_engine.ingestion.projections.graph.collaboration_source_projectors import (
    _issue_attributes,
)
from harborrag_runtime.topology.entity_summary_index import facet_payload
from harborrag_runtime.topology.summary_service import _with_facets

SKILLS = SummaryFacet(name="skill_set", field="Skill Set")
LABELS = SummaryFacet(name="labels", field="labels")
YEARS = SummaryFacet(name="years", field="Years", kind="integer")


def _node(issue: dict[str, object], labels: list[str]) -> GraphNodeRecord:
    rows = [
        {
            "field_id": item.field_id,
            "name": item.name,
            "text": item.text,
            "value_kind": item.value_kind.value,
        }
        for item in custom_field_metadata(issue)
    ]
    attributes = _issue_attributes(
        {"issue_key": "A-1", "labels": labels, "typed_custom_attributes": rows}
    )
    return GraphNodeRecord(
        node_key="n",
        node_kind=KnowledgeNodeKind.SOURCE_ENTITY,
        entity_type=GraphEntityType.JIRA_ISSUE,
        logical_id="A-1",
        ownership_scope=GraphOwnershipScope.SOURCE_SCOPE,
        owner_id=TenantId("DEFAULT"),
        source_scope_id="S",
        attributes=attributes,
    )


def test_a_multi_select_and_labels_keep_each_member() -> None:
    issue = {
        "fields": {
            "customfield_1": [{"value": "Java", "id": "1"}, {"value": "Python", "id": "2"}],
            "customfield_2": 7,
        },
        "names": {"customfield_1": "Skill Set", "customfield_2": "Years"},
        "schema": {
            "customfield_1": {
                "type": "array",
                "custom": "com.atlassian.jira.plugin.system.customfieldtypes:multiselect",
                "items": "option",
            },
            "customfield_2": {"type": "number"},
        },
    }
    facets = (SKILLS, LABELS, YEARS)
    card = _with_facets(
        SummaryCard(description="x"),
        _node(issue, ["Java", "Python", "Java"]),
        SummaryPolicy(model_fingerprint="m", facets=facets),
        (ISSUE_DOCUMENT,),
    )

    payload = facet_payload(_binding(card.attributes), facets)

    assert payload == {"skill_set": ["java", "python"], "labels": ["java", "python"], "years": [7]}
    # The same members the evidence path filters ``fields.skill_set`` by.
    evidence = filterable_fields(custom_field_metadata(issue))["skill_set"]
    assert [value.casefold() for value in evidence] == payload["skill_set"]


def test_a_single_value_is_still_one_member() -> None:
    issue = {
        "fields": {"customfield_1": {"value": "Java", "id": "1"}},
        "names": {"customfield_1": "Skill Set"},
        "schema": {"customfield_1": {"type": "option"}},
    }
    card = _with_facets(
        SummaryCard(description="x"),
        _node(issue, []),
        SummaryPolicy(model_fingerprint="m", facets=(SKILLS,)),
        (ISSUE_DOCUMENT,),
    )
    assert [(item.name, item.values) for item in card.attributes] == [("skill_set", ("Java",))]
