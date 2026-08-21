"""Source topology projectors for collaboration platforms."""

from __future__ import annotations

from harborrag_core.chunking import RelationType
from harborrag_core.ingestion import GraphEntityType

from .source_projector_support import (
    BaseSourceProjector,
    is_attachment,
    mapping_sequence,
    mapping_value,
    selected_values,
    text_sequence,
    text_value,
)


class ConfluenceSourceProjector(BaseSourceProjector):
    entity_type = GraphEntityType.CONFLUENCE_PAGE

    def project(self, state, document, data_source, document_version):  # type: ignore[no-untyped-def]
        extra = document.provenance.extra
        space_id = text_value(extra, "space_id", "space_key") or "unknown-space"
        space = state.source_node(
            GraphEntityType.CONFLUENCE_SPACE,
            space_id,
            title=text_value(extra, "space_name", "space_key") or space_id,
            attributes=selected_values(extra, "space_key"),
        )
        self.edge(state, RelationType.CONTAINS, data_source, space)

        item_type = (
            GraphEntityType.CONFLUENCE_ATTACHMENT
            if is_attachment(state, extra)
            else GraphEntityType.CONFLUENCE_PAGE
        )
        item_id = text_value(extra, "page_id", "content_id") or state.context.source_item_id
        item = self.source_item(
            state,
            document,
            provider_id=item_id,
            entity_type=item_type,
            attributes=selected_values(extra, "page_id", "space_key"),
        )

        if item_type == GraphEntityType.CONFLUENCE_ATTACHMENT:
            parent_id = text_value(extra, "parent_source_item_id", "parent_page_id")
            if parent_id:
                parent = state.source_node(
                    GraphEntityType.CONFLUENCE_PAGE,
                    parent_id,
                    title=parent_id,
                    attributes={"placeholder": True},
                )
                self.edge(state, RelationType.CONTAINS, space, parent)
                self.edge(state, RelationType.HAS_ATTACHMENT, parent, item, explicit=True)
            else:
                self.edge(state, RelationType.CONTAINS, space, item)
        else:
            ancestor_ids = text_sequence(extra.get("ancestor_ids"))
            ancestor_titles = text_sequence(extra.get("ancestor_titles"))
            parent = space
            for index, ancestor_id in enumerate(ancestor_ids):
                ancestor = state.source_node(
                    GraphEntityType.CONFLUENCE_PAGE,
                    ancestor_id,
                    title=(ancestor_titles[index] if index < len(ancestor_titles) else ancestor_id),
                    attributes={"placeholder": True},
                )
                relation = RelationType.CONTAINS if parent == space else RelationType.PARENT_OF
                self.edge(state, relation, parent, ancestor)
                parent = ancestor
            self.edge(
                state,
                RelationType.CONTAINS if parent == space else RelationType.PARENT_OF,
                parent,
                item,
            )
        for attachment_value in mapping_sequence(extra.get("attachments")):
            attachment_id = text_value(attachment_value, "id", "attachment_id")
            if not attachment_id:
                continue
            attachment = state.source_node(
                GraphEntityType.CONFLUENCE_ATTACHMENT,
                attachment_id,
                title=text_value(attachment_value, "title", "filename", "name") or attachment_id,
                attributes={"placeholder": True},
            )
            self.edge(state, RelationType.HAS_ATTACHMENT, item, attachment, explicit=True)
        self.version(state, item, document_version)
        return item


class JiraSourceProjector(BaseSourceProjector):
    entity_type = GraphEntityType.JIRA_ISSUE

    def project(self, state, document, data_source, document_version):  # type: ignore[no-untyped-def]
        extra = document.provenance.extra
        project_id = text_value(extra, "project_id", "project_key") or "unknown-project"
        project = state.source_node(
            GraphEntityType.JIRA_PROJECT,
            project_id,
            title=text_value(extra, "project_name", "project_key") or project_id,
            attributes=selected_values(extra, "project_key"),
        )
        self.edge(state, RelationType.CONTAINS, data_source, project)
        issue_id = text_value(extra, "issue_key", "issue_id") or state.context.source_item_id
        issue = self.source_item(
            state,
            document,
            provider_id=issue_id,
            attributes=selected_values(extra, "issue_key", "status", "issue_type", "resolved_at"),
        )
        self.edge(state, RelationType.CONTAINS, project, issue)
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
            self.edge(state, RelationType.CONTAINS, project, parent_issue)
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
            self.edge(state, RelationType.CONTAINS, project, child_issue)
            self.edge(state, RelationType.PARENT_OF, issue, child_issue, explicit=True)
        self.version(state, issue, document_version)
        return issue


__all__ = ["ConfluenceSourceProjector", "JiraSourceProjector"]
