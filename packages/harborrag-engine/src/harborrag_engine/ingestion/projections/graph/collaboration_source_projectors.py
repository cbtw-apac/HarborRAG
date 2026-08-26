"""Source topology projectors for collaboration platforms."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from harborrag_core.chunking import RelationType
from harborrag_core.ingestion import GraphEntityType, GraphNodeRecord

from .graph_state import GraphProjectionState, GraphRelationSpec
from .source_projector_support import (
    BaseSourceProjector,
    is_attachment,
    mapping_sequence,
    mapping_value,
    project_structure_chain,
    selected_values,
    source_provider_id,
    text_sequence,
    text_value,
)


def _parent_provider_id(
    state: GraphProjectionState,
    extra: Mapping[str, Any],
) -> str | None:
    """Key an attachment's parent the way that parent's own projection keys itself.

    ``parent_source_item_id`` is the parent's canonical source item identity, which
    ``attachment_source_record`` copies verbatim from the parent ``SourceRecord.id`` --
    a ``scheme://scope/id`` URI for both Atlassian connectors. The parent's own
    projector keys it by the bare provider id, and ``entity_type`` plus ``provider_id``
    are both hashed into the source-entity node key, so skipping this reduction leaves
    the placeholder on a key no concrete projection ever produces.
    """

    raw = text_value(extra, "parent_source_item_id", "parent_page_id", "parent_issue_key")
    if raw is None:
        return None
    return source_provider_id(state.context.connector_type.value, raw)


def _container(  # noqa: PLR0913
    state: GraphProjectionState,
    data_source: GraphNodeRecord,
    *,
    entity_type: GraphEntityType,
    provider_id: str | None,
    title: str | None,
    attributes: dict[str, Any],
) -> GraphNodeRecord:
    """Resolve the space or project a document belongs to, or fall back to its data source.

    An attachment is dispatched as its own source item and carries none of its parent's
    space or project metadata, so the old ``or "unknown-space"`` fallback minted a real
    container node and then asserted ``CONTAINS`` from it -- handing the attachment's
    genuine parent page or issue a second, fictional container parent. Measured on a live
    graph, 19 of 20 Confluence space-root edges were wrong and every one came from an
    attachment.

    The data source is the honest anchor: it exists, it is correctly scoped, and the
    parent's own projection still files it under its real container. That keeps every
    concrete node reachable from the tenant spine -- which is the reason a fallback hub
    was there in the first place -- without asserting a membership nobody knows.
    """

    if provider_id is None:
        return data_source
    container = state.source_node(
        entity_type,
        provider_id,
        title=title or provider_id,
        attributes=attributes,
    )
    _edge(state, data_source, container)
    return container


def _edge(
    state: GraphProjectionState,
    source: GraphNodeRecord,
    target: GraphNodeRecord,
) -> None:
    state.relation(
        GraphRelationSpec(
            relation_type=RelationType.CONTAINS,
            source=source,
            target=target,
            source_explicit=False,
        )
    )


class ConfluenceSourceProjector(BaseSourceProjector):
    entity_type = GraphEntityType.CONFLUENCE_PAGE

    def project(self, state, document, data_source, document_version):  # type: ignore[no-untyped-def]
        extra = document.provenance.extra
        container = _container(
            state,
            data_source,
            entity_type=GraphEntityType.CONFLUENCE_SPACE,
            provider_id=text_value(extra, "space_id", "space_key"),
            title=text_value(extra, "space_name", "space_key"),
            attributes=selected_values(extra, "space_key"),
        )

        item_type = (
            GraphEntityType.CONFLUENCE_ATTACHMENT
            if is_attachment(state, extra)
            else GraphEntityType.CONFLUENCE_PAGE
        )
        item_id = text_value(extra, "page_id", "content_id")
        item = self.source_item(
            state,
            document,
            provider_id=item_id,
            entity_type=item_type,
            attributes=selected_values(extra, "page_id", "space_key"),
        )

        if item_type == GraphEntityType.CONFLUENCE_ATTACHMENT:
            parent_id = _parent_provider_id(state, extra)
            if parent_id:
                parent = state.source_node(
                    GraphEntityType.CONFLUENCE_PAGE,
                    parent_id,
                    title=parent_id,
                    attributes={"placeholder": True},
                )
                # An attachment belongs to one page and is not a member of the space's
                # document set: putting it there made `space CONTAINS` hold pages *and*
                # attachments, so "how many pages in this space" needed an entity-type
                # filter. Its parent is a page, so the parent is what earns membership --
                # measured, all 159 attachments reach the space this way, so nothing is
                # lost by keeping the attachment off that axis.
                self.edge(state, RelationType.CONTAINS, container, parent)
                self.edge(state, RelationType.HAS_ATTACHMENT, parent, item, explicit=True)
            else:
                # No discoverable parent: the only remaining anchor is the container, and a
                # stranded node is worse than a heterogeneous edge. Not reached by either
                # Atlassian connector, which always carries parent_source_item_id.
                self.edge(state, RelationType.CONTAINS, container, item)
        else:
            ancestor_ids = text_sequence(extra.get("ancestor_ids"))
            ancestor_titles = text_sequence(extra.get("ancestor_titles"))
            # Membership and ancestry are different questions, so they get different
            # edges. CONTAINS previously reached only a page with no ancestors, which in
            # a real space is the single root: measured on a live graph, the space had a
            # CONTAINS out-degree of 1 for 98 pages, so "how many pages does this space
            # have" cost a 7-hop variable-length walk against a traversal ceiling of 8.
            # The space does contain every one of its pages, and JiraSourceProjector
            # already files every issue under its project this way, so state it directly
            # and let PARENT_OF carry the tree on its own. Ancestors are members too --
            # one that is never itself ingested would otherwise have no edge to the space.
            ancestors = [
                state.source_node(
                    GraphEntityType.CONFLUENCE_PAGE,
                    ancestor_id,
                    title=(ancestor_titles[index] if index < len(ancestor_titles) else ancestor_id),
                    attributes={"placeholder": True},
                )
                for index, ancestor_id in enumerate(ancestor_ids)
            ]
            # Membership: every page in the space, ancestors included -- an ancestor is a
            # page, so it belongs to the same homogeneous set. Structure is separate.
            for ancestor in ancestors:
                self.edge(state, RelationType.CONTAINS, container, ancestor)
            self.edge(state, RelationType.CONTAINS, container, item)
            project_structure_chain(
                self, state, container=container, item=item, ancestors=ancestors
            )
            # Only a page lists attachments. Running this for an attachment document
            # would let one attachment own another.
            for attachment_value in mapping_sequence(extra.get("attachments")):
                attachment_id = text_value(attachment_value, "id", "attachment_id")
                if not attachment_id:
                    continue
                attachment = state.source_node(
                    GraphEntityType.CONFLUENCE_ATTACHMENT,
                    attachment_id,
                    title=(
                        text_value(attachment_value, "title", "filename", "name") or attachment_id
                    ),
                    attributes={"placeholder": True},
                )
                self.edge(state, RelationType.HAS_ATTACHMENT, item, attachment, explicit=True)
        self.version(state, item, document_version)
        return item


class JiraSourceProjector(BaseSourceProjector):
    entity_type = GraphEntityType.JIRA_ISSUE

    def project(self, state, document, data_source, document_version):  # type: ignore[no-untyped-def]
        extra = document.provenance.extra
        container = _container(
            state,
            data_source,
            entity_type=GraphEntityType.JIRA_PROJECT,
            provider_id=text_value(extra, "project_id", "project_key"),
            title=text_value(extra, "project_name", "project_key"),
            attributes=selected_values(extra, "project_key"),
        )
        if is_attachment(state, extra):
            return self._project_attachment(state, document, container, document_version)
        issue_id = text_value(extra, "issue_key", "issue_id")
        issue = self.source_item(
            state,
            document,
            provider_id=issue_id,
            attributes=selected_values(extra, "issue_key", "status", "issue_type", "resolved_at"),
        )
        self.edge(state, RelationType.CONTAINS, container, issue)
        parent = mapping_value(extra.get("parent"))
        # Key before numeric id, matching the real issue's identity above —
        # otherwise the placeholder and the ingested issue land as two nodes.
        parent_id = text_value(parent, "key", "id")
        if parent_id:
            parent_issue = state.source_node(
                GraphEntityType.JIRA_ISSUE,
                parent_id,
                title=text_value(parent, "summary", "key") or parent_id,
                attributes={"placeholder": True},
            )
            # No CONTAINS from this issue's project: a parent may live in another
            # project (Advanced Roadmaps allows it), and filing it here gave it a second,
            # wrong project. Where the parent is in the same project the edge merely
            # duplicated the one its own projection makes, so nothing is lost.
            self.edge(state, RelationType.PARENT_OF, parent_issue, issue, explicit=True)
        for child in mapping_sequence(extra.get("subtasks")):
            child_id = text_value(child, "key", "id")
            if not child_id:
                continue
            child_issue = state.source_node(
                GraphEntityType.JIRA_ISSUE,
                child_id,
                title=text_value(child, "summary", "key") or child_id,
                attributes={"placeholder": True},
            )
            # Same reasoning as the parent above: the subtask's own projection files it.
            self.edge(state, RelationType.PARENT_OF, issue, child_issue, explicit=True)
        self.version(state, issue, document_version)
        return issue

    def _project_attachment(self, state, document, container, document_version):  # type: ignore[no-untyped-def]
        """Project an attachment dispatched as its own source item, not as an issue.

        Jira attachments arrive through the same independent-source-item path as
        Confluence ones, but this projector had no branch for them, so every attachment
        was typed ``jira_issue`` and hung off the project as a sibling of real issues.
        """

        # No attributes: `media_type` and `size_bytes` are not allowlisted, so passing them
        # raised and failed every Jira attachment outright. They are media metadata rather
        # than identifiers, so they belong in the vector payload, not in a graph node whose
        # job is topology -- and the filename already survives as the node title. This
        # mirrors the Confluence branch, which carries only locators (page_id, space_key).
        attachment = self.source_item(
            state,
            document,
            entity_type=GraphEntityType.JIRA_ATTACHMENT,
        )
        parent_id = _parent_provider_id(state, document.provenance.extra)
        if parent_id:
            parent_issue = state.source_node(
                GraphEntityType.JIRA_ISSUE,
                parent_id,
                title=parent_id,
                attributes={"placeholder": True},
            )
            # The issue earns project membership; the attachment hangs off the issue only.
            # Same reasoning as the Confluence branch: `project CONTAINS` stays issues.
            self.edge(state, RelationType.CONTAINS, container, parent_issue)
            self.edge(state, RelationType.HAS_ATTACHMENT, parent_issue, attachment, explicit=True)
        else:
            self.edge(state, RelationType.CONTAINS, container, attachment)
        self.version(state, attachment, document_version)
        return attachment


__all__ = ["ConfluenceSourceProjector", "JiraSourceProjector"]
