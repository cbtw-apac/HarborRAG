from __future__ import annotations

from collections.abc import Mapping, Sequence

from harborrag_core.schemas.documents import ChunkRecord

from .projectionstate import GraphProjectionState


class SourceGraphProjector:
    """Add bounded Jira, Confluence, and document relations when explicitly present."""

    def project(
        self,
        state: GraphProjectionState,
        records: tuple[ChunkRecord, ...],
        *,
        artifact_node_id: str,
        chunk_node_ids: Mapping[str, str],
        parent_node_ids: Mapping[str, str],
    ) -> None:
        """Project explicit source metadata into bounded domain relations."""

        if not records:
            return
        source_kind = self._text(records[0].metadata, "source_kind") or "unknown"
        if source_kind == "jira":
            self._jira(state, records, artifact_node_id, chunk_node_ids)
        elif source_kind == "confluence":
            self._confluence(
                state,
                records,
                artifact_node_id,
                chunk_node_ids,
                parent_node_ids,
            )
        else:
            self._document(state, records, chunk_node_ids, parent_node_ids)

    def _jira(
        self,
        state: GraphProjectionState,
        records: tuple[ChunkRecord, ...],
        issue_node_id: str,
        chunk_node_ids: Mapping[str, str],
    ) -> None:
        metadata = records[0].metadata
        project_key = self._text(metadata, "project_key")
        if project_key:
            project = state.node(
                kind="jira_project",
                key=project_key,
                labels={"JiraProject"},
                properties={"project_key": project_key},
            )
            state.edge("HAS_ISSUE", project, issue_node_id)
        self._jira_reference(state, issue_node_id, metadata.get("parent"), "PARENT_ISSUE")
        self._jira_reference(
            state,
            issue_node_id,
            metadata.get("epic") or metadata.get("epic_key"),
            "IN_EPIC",
        )
        for link in self._values(metadata.get("issue_links")):
            target = link.get("issue") if isinstance(link, Mapping) else link
            self._jira_reference(state, issue_node_id, target, "LINKS_TO")
        self._person_relation(state, issue_node_id, metadata.get("assignee"), "ASSIGNED_TO")
        self._person_relation(state, issue_node_id, metadata.get("reporter"), "REPORTED_BY")
        for record in records:
            chunk_id = chunk_node_ids[str(record.chunk_revision_id)]
            if record.role == "jira.comment":
                state.edge("HAS_COMMENT", issue_node_id, chunk_id)
            elif record.role == "jira.attachment":
                state.edge("HAS_ATTACHMENT", issue_node_id, chunk_id)

    def _jira_reference(
        self,
        state: GraphProjectionState,
        issue_node_id: str,
        value: object,
        relationship_type: str,
    ) -> None:
        key = self._entity_key(value, "key", "issue_key", "id")
        if not key:
            return
        target = state.node(
            kind="jira_issue_reference",
            key=key,
            labels={"JiraIssueReference"},
            properties={"issue_key": key},
        )
        state.edge(relationship_type, issue_node_id, target)

    def _person_relation(
        self,
        state: GraphProjectionState,
        source_id: str,
        value: object,
        relationship_type: str,
    ) -> None:
        key = self._entity_key(value, "account_id", "id", "display_name", "name")
        if not key:
            return
        person = state.node(
            kind="person",
            key=key,
            labels={"Person"},
            properties={"identity": key},
        )
        state.edge(relationship_type, source_id, person)

    def _confluence(
        self,
        state: GraphProjectionState,
        records: tuple[ChunkRecord, ...],
        page_node_id: str,
        chunk_node_ids: Mapping[str, str],
        parent_node_ids: Mapping[str, str],
    ) -> None:
        metadata = records[0].metadata
        space_key = self._text(metadata, "space_key")
        if space_key:
            space = state.node(
                kind="confluence_space",
                key=space_key,
                labels={"ConfluenceSpace"},
                properties={"space_key": space_key},
            )
            state.edge("HAS_PAGE", space, page_node_id)
        ancestors = self._values(metadata.get("ancestors"))
        if ancestors:
            parent_key = self._entity_key(ancestors[-1], "id", "content_id", "page_id")
            if parent_key:
                parent = state.node(
                    kind="confluence_page_reference",
                    key=parent_key,
                    labels={"ConfluencePageReference"},
                    properties={"page_id": parent_key},
                )
                state.edge("PARENT_PAGE", page_node_id, parent)
        for label in self._values(metadata.get("labels")):
            key = self._entity_key(label, "name", "id")
            if key:
                label_node = state.node(
                    kind="confluence_label",
                    key=key,
                    labels={"ConfluenceLabel"},
                    properties={"name": key},
                )
                state.edge("HAS_LABEL", page_node_id, label_node)
        for link in self._values(metadata.get("linked_pages") or metadata.get("linked_page_ids")):
            key = self._entity_key(link, "id", "content_id", "page_id")
            if key:
                target = state.node(
                    kind="confluence_page_reference",
                    key=key,
                    labels={"ConfluencePageReference"},
                    properties={"page_id": key},
                )
                state.edge("LINKS_TO", page_node_id, target)
        top_sections = {
            parent_node_ids[str(record.chunk_revision_id)]
            for record in records
            if record.context.structural_path
        }
        for section_id in sorted(top_sections):
            state.edge("HAS_SECTION", page_node_id, section_id)
        for record in records:
            if "attachment" in record.role:
                state.edge(
                    "HAS_ATTACHMENT",
                    page_node_id,
                    chunk_node_ids[str(record.chunk_revision_id)],
                )

    def _document(
        self,
        state: GraphProjectionState,
        records: tuple[ChunkRecord, ...],
        chunk_node_ids: Mapping[str, str],
        parent_node_ids: Mapping[str, str],
    ) -> None:
        for record in records:
            parent_id = parent_node_ids[str(record.chunk_revision_id)]
            chunk_id = chunk_node_ids[str(record.chunk_revision_id)]
            span = record.source_span
            if span is not None and span.page_start is not None and span.page_end is not None:
                for page in self._pages(span.page_start, span.page_end):
                    page_node = state.node(
                        kind="page",
                        key=str(page),
                        labels={"Page"},
                        properties={"page": page},
                    )
                    state.edge("HAS_PAGE", parent_id, page_node)
            if record.role == "table":
                state.edge("HAS_TABLE", parent_id, chunk_id)
            elif record.role in {"figure", "caption"}:
                state.edge("HAS_FIGURE", parent_id, chunk_id)

    @staticmethod
    def _pages(start: int, end: int) -> tuple[int, ...]:
        if end - start <= 50:
            return tuple(range(start, end + 1))
        return (start, end)

    @staticmethod
    def _text(metadata: Mapping[str, object], key: str) -> str | None:
        value = metadata.get(key)
        return value.strip() if isinstance(value, str) and value.strip() else None

    @staticmethod
    def _values(value: object) -> tuple[object, ...]:
        if isinstance(value, Sequence) and not isinstance(value, (str, bytes)):
            return tuple(value)
        return (value,) if value is not None else ()

    @staticmethod
    def _entity_key(value: object, *keys: str) -> str | None:
        if isinstance(value, str) and value.strip():
            return value.strip()
        if isinstance(value, Mapping):
            for key in keys:
                item = value.get(key)
                if isinstance(item, str) and item.strip():
                    return item.strip()
        return None
