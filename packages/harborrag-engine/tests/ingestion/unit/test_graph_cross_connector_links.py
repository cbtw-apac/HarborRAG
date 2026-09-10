"""Node-key convergence for a relation whose far end is on another connector."""

from __future__ import annotations

from harborrag_core.chunking import RelationType
from harborrag_core.domain.document import DocumentRelation
from harborrag_core.ingestion import GraphEntityType

from .graph_convergence_helpers import keys as _keys
from .graph_convergence_helpers import project as _project


def test_cross_connector_link_converges_with_the_page_it_names() -> None:
    """A Jira issue linking to a Confluence page must key on that page.

    Both the entity type and the provider-id reduction used to be taken from the
    *linking* document's connector, so a ``confluence://`` target became a
    ``jira_issue`` whose provider id was still the whole URI. Two of the three
    inputs to ``source_entity_node_key`` were wrong at once, so the stub could
    never converge with the page however often either side was reprojected -- and
    the mismatch is silent, because nothing compares them within one build.
    """

    page = _project(
        "confluence",
        {"space_id": "space-1", "space_key": "ENG", "page_id": "77"},
        source_item_id="confluence://ENG/77",
    )
    issue = _project(
        "jira",
        {"project_id": "10", "project_key": "OPS", "issue_key": "OPS-3"},
        source_item_id="jira://OPS/OPS-3",
        relations=[
            DocumentRelation(
                predicate="links_to",
                target_id="confluence://ENG/77",
                target_type="document",
            )
        ],
    )

    # The stand-in the Jira batch writes is the page's own node, not a phantom
    # beside it -- so the concrete projection claims it instead of forking.
    assert _keys(issue, GraphEntityType.CONFLUENCE_PAGE) == _keys(
        page, GraphEntityType.CONFLUENCE_PAGE
    )
    # Nothing in the Jira batch is a second issue wearing the page's identity.
    assert "confluence://ENG/77" not in _keys(issue, GraphEntityType.JIRA_ISSUE)
    assert {
        (relation.source_node_key, relation.target_node_key)
        for relation in issue.relations
        if relation.relation_type is RelationType.LINKS_TO
    } == {
        (
            _keys(issue, GraphEntityType.JIRA_ISSUE)["OPS-3"],
            _keys(page, GraphEntityType.CONFLUENCE_PAGE)["77"],
        )
    }


def test_same_connector_link_identity_is_unchanged_by_scheme_reading() -> None:
    """Reading the target's scheme must not move a same-connector target.

    Connectors emit fully-schemed identities for their own items, so the scheme
    is present on the common case too; it has to resolve back to the declaring
    connector or every working placeholder would be re-keyed.
    """

    target = _project(
        "confluence",
        {"space_id": "space-1", "space_key": "ENG", "page_id": "88"},
        source_item_id="confluence://ENG/88",
    )
    linking = _project(
        "confluence",
        {"space_id": "space-1", "space_key": "ENG", "page_id": "77"},
        source_item_id="confluence://ENG/77",
        relations=[
            DocumentRelation(
                predicate="links_to",
                target_id="confluence://ENG/88",
                target_type="document",
            )
        ],
    )

    assert (
        _keys(target, GraphEntityType.CONFLUENCE_PAGE)["88"]
        == _keys(linking, GraphEntityType.CONFLUENCE_PAGE)["88"]
    )
