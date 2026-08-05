"""Provider-aware stable source topology projectors for graph schema v2."""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import PurePosixPath
from typing import Any, Protocol

from harborrag_core.chunking import RelationType
from harborrag_core.domain.document import Document
from harborrag_core.ingestion import GraphEntityType, GraphNodeRecord

from .graph_state import GraphProjectionState, GraphRelationSpec


class GraphSourceProjector(Protocol):
    """Build stable provider hierarchy from canonical provenance metadata."""

    def project(
        self,
        state: GraphProjectionState,
        document: Document,
        data_source: GraphNodeRecord,
        document_version: GraphNodeRecord,
    ) -> GraphNodeRecord: ...


class GraphSourceProjectorRegistry:
    """Resolve provider projectors without leaking providers into core contracts."""

    def __init__(self) -> None:
        self._projectors: dict[str, GraphSourceProjector] = {}

    def register(self, connector_type: str, projector: GraphSourceProjector) -> None:
        key = connector_type.strip().casefold()
        if not key:
            raise ValueError("graph source projector connector type must be non-empty")
        self._projectors[key] = projector

    def resolve(self, connector_type: str) -> GraphSourceProjector:
        return self._projectors.get(connector_type.casefold(), GenericSourceProjector())


def default_graph_source_projector_registry() -> GraphSourceProjectorRegistry:
    registry = GraphSourceProjectorRegistry()
    registry.register("confluence", ConfluenceSourceProjector())
    registry.register("jira", JiraSourceProjector())
    registry.register("github", GitHubSourceProjector())
    registry.register("sharepoint", SharePointSourceProjector())
    registry.register("local", LocalSourceProjector())
    return registry


class _BaseSourceProjector:
    entity_type = GraphEntityType.GENERIC_SOURCE_ITEM

    def _source_item(  # noqa: PLR0913
        self,
        state: GraphProjectionState,
        document: Document,
        *,
        provider_id: str | None = None,
        title: str | None = None,
        attributes: dict[str, Any] | None = None,
        entity_type: GraphEntityType | None = None,
    ) -> GraphNodeRecord:
        return state.source_node(
            entity_type or self.entity_type,
            provider_id or state.context.source_item_id,
            title=title or document.title,
            attributes=attributes,
        )

    @staticmethod
    def _edge(
        state: GraphProjectionState,
        relation_type: RelationType,
        source: GraphNodeRecord,
        target: GraphNodeRecord,
        *,
        explicit: bool = False,
    ) -> None:
        state.relation(
            GraphRelationSpec(
                relation_type=relation_type,
                source=source,
                target=target,
                source_explicit=explicit,
            )
        )

    def _version(
        self,
        state: GraphProjectionState,
        source_item: GraphNodeRecord,
        document_version: GraphNodeRecord,
    ) -> None:
        self._edge(state, RelationType.HAS_VERSION, source_item, document_version)


class GenericSourceProjector(_BaseSourceProjector):
    def project(
        self,
        state: GraphProjectionState,
        document: Document,
        data_source: GraphNodeRecord,
        document_version: GraphNodeRecord,
    ) -> GraphNodeRecord:
        item = self._source_item(state, document)
        self._edge(state, RelationType.CONTAINS, data_source, item)
        self._version(state, item, document_version)
        return item


class ConfluenceSourceProjector(_BaseSourceProjector):
    entity_type = GraphEntityType.CONFLUENCE_PAGE

    def project(self, state, document, data_source, document_version):  # type: ignore[no-untyped-def]
        extra = document.provenance.extra
        space_id = _text(extra, "space_id", "space_key") or "unknown-space"
        space = state.source_node(
            GraphEntityType.CONFLUENCE_SPACE,
            space_id,
            title=_text(extra, "space_name", "space_key") or space_id,
            attributes=_selected(extra, "space_key"),
        )
        self._edge(state, RelationType.CONTAINS, data_source, space)

        item_type = (
            GraphEntityType.CONFLUENCE_ATTACHMENT
            if _is_attachment(state, extra)
            else GraphEntityType.CONFLUENCE_PAGE
        )
        item_id = _text(extra, "page_id", "content_id") or state.context.source_item_id
        item = self._source_item(
            state,
            document,
            provider_id=item_id,
            entity_type=item_type,
            attributes=_selected(extra, "page_id", "space_key"),
        )

        if item_type == GraphEntityType.CONFLUENCE_ATTACHMENT:
            parent_id = _text(extra, "parent_source_item_id", "parent_page_id")
            if parent_id:
                parent = state.source_node(
                    GraphEntityType.CONFLUENCE_PAGE,
                    parent_id,
                    title=parent_id,
                    attributes={"placeholder": True},
                )
                self._edge(state, RelationType.CONTAINS, space, parent)
                self._edge(state, RelationType.HAS_ATTACHMENT, parent, item, explicit=True)
            else:
                self._edge(state, RelationType.CONTAINS, space, item)
        else:
            ancestor_ids = _sequence(extra.get("ancestor_ids"))
            ancestor_titles = _sequence(extra.get("ancestor_titles"))
            parent = space
            for index, ancestor_id in enumerate(ancestor_ids):
                ancestor = state.source_node(
                    GraphEntityType.CONFLUENCE_PAGE,
                    ancestor_id,
                    title=(ancestor_titles[index] if index < len(ancestor_titles) else ancestor_id),
                    attributes={"placeholder": True},
                )
                relation = RelationType.CONTAINS if parent == space else RelationType.PARENT_OF
                self._edge(state, relation, parent, ancestor)
                parent = ancestor
            self._edge(
                state,
                RelationType.CONTAINS if parent == space else RelationType.PARENT_OF,
                parent,
                item,
            )
        for attachment_value in _mapping_sequence(extra.get("attachments")):
            attachment_id = _text(attachment_value, "id", "attachment_id")
            if not attachment_id:
                continue
            attachment = state.source_node(
                GraphEntityType.CONFLUENCE_ATTACHMENT,
                attachment_id,
                title=(
                    _text(attachment_value, "title", "filename", "name") or attachment_id
                ),
                attributes={"placeholder": True},
            )
            self._edge(state, RelationType.HAS_ATTACHMENT, item, attachment, explicit=True)
        self._version(state, item, document_version)
        return item


class JiraSourceProjector(_BaseSourceProjector):
    entity_type = GraphEntityType.JIRA_ISSUE

    def project(self, state, document, data_source, document_version):  # type: ignore[no-untyped-def]
        extra = document.provenance.extra
        project_id = _text(extra, "project_id", "project_key") or "unknown-project"
        project = state.source_node(
            GraphEntityType.JIRA_PROJECT,
            project_id,
            title=_text(extra, "project_name", "project_key") or project_id,
            attributes=_selected(extra, "project_key"),
        )
        self._edge(state, RelationType.CONTAINS, data_source, project)
        issue_id = _text(extra, "issue_key", "issue_id") or state.context.source_item_id
        issue = self._source_item(
            state,
            document,
            provider_id=issue_id,
            attributes=_selected(extra, "issue_key", "status", "issue_type", "resolved_at"),
        )
        self._edge(state, RelationType.CONTAINS, project, issue)
        parent = _mapping(extra.get("parent"))
        parent_id = _text(parent, "id", "key")
        if parent_id:
            parent_issue = state.source_node(
                GraphEntityType.JIRA_ISSUE,
                parent_id,
                title=_text(parent, "summary", "key") or parent_id,
                attributes={"placeholder": True},
            )
            self._edge(state, RelationType.CONTAINS, project, parent_issue)
            self._edge(state, RelationType.PARENT_OF, parent_issue, issue, explicit=True)
        for child in _mapping_sequence(extra.get("subtasks")):
            child_id = _text(child, "id", "key")
            if not child_id:
                continue
            child_issue = state.source_node(
                GraphEntityType.JIRA_ISSUE,
                child_id,
                title=_text(child, "summary", "key") or child_id,
                attributes={"placeholder": True},
            )
            self._edge(state, RelationType.CONTAINS, project, child_issue)
            self._edge(state, RelationType.PARENT_OF, issue, child_issue, explicit=True)
        self._version(state, issue, document_version)
        return issue


class GitHubSourceProjector(_BaseSourceProjector):
    entity_type = GraphEntityType.GITHUB_FILE

    def project(self, state, document, data_source, document_version):  # type: ignore[no-untyped-def]
        extra = document.provenance.extra
        owner_id = _text(extra, "owner") or "unknown-owner"
        repo_id = _text(extra, "repository_id", "repo") or "unknown-repository"
        owner = state.source_node(GraphEntityType.GITHUB_OWNER, owner_id, title=owner_id)
        repository = state.source_node(
            GraphEntityType.GITHUB_REPOSITORY,
            repo_id,
            title=_text(extra, "repo") or repo_id,
            attributes=_selected(extra, "default_branch"),
        )
        self._edge(state, RelationType.CONTAINS, data_source, owner)
        self._edge(state, RelationType.CONTAINS, owner, repository)

        relative_path = _portable_path(_text(extra, "path") or state.context.source_item_id)
        item = self._source_item(
            state,
            document,
            provider_id=relative_path,
            attributes={"relative_path": relative_path, **_selected(extra, "sha", "mode")},
        )
        parent = repository
        path = PurePosixPath(relative_path)
        accumulated: list[str] = []
        for part in path.parts[:-1]:
            accumulated.append(part)
            directory_path = "/".join(accumulated)
            directory = state.source_node(
                GraphEntityType.GITHUB_DIRECTORY,
                directory_path,
                title=part,
                attributes={"relative_path": directory_path},
            )
            self._edge(state, RelationType.CONTAINS, parent, directory)
            parent = directory
        self._edge(state, RelationType.CONTAINS, parent, item)

        ref_name = _text(extra, "ref")
        commit_sha = _text(extra, "commit_sha")
        if ref_name and commit_sha:
            ref = state.source_node(GraphEntityType.GITHUB_REF, ref_name, title=ref_name)
            commit = state.source_node(
                GraphEntityType.GITHUB_COMMIT,
                commit_sha,
                title=commit_sha[:12],
            )
            self._edge(state, RelationType.CONTAINS, repository, ref)
            self._edge(state, RelationType.POINTS_TO, ref, commit, explicit=True)
            self._edge(state, RelationType.RESOLVED_AT, document_version, commit, explicit=True)
        self._version(state, item, document_version)
        return item


class SharePointSourceProjector(_BaseSourceProjector):
    entity_type = GraphEntityType.SHAREPOINT_FILE

    def project(self, state, document, data_source, document_version):  # type: ignore[no-untyped-def]
        extra = document.provenance.extra
        site_id = _text(extra, "site_id") or "unknown-site"
        drive_id = _text(extra, "drive_id") or "unknown-drive"
        site = state.source_node(
            GraphEntityType.SHAREPOINT_SITE,
            site_id,
            title=_text(extra, "site_name") or site_id,
        )
        drive = state.source_node(
            GraphEntityType.SHAREPOINT_DRIVE,
            drive_id,
            title=_text(extra, "drive_name") or drive_id,
            attributes=_selected(extra, "drive_type"),
        )
        self._edge(state, RelationType.CONTAINS, data_source, site)
        self._edge(state, RelationType.CONTAINS, site, drive)

        item_id = _text(extra, "item_id") or state.context.source_item_id
        item = self._source_item(
            state,
            document,
            provider_id=item_id,
            attributes=_selected(extra, "item_name", "etag", "ctag"),
        )
        parent_ref = _mapping(extra.get("parent"))
        parent_id = _text(parent_ref, "id") or _text(extra, "parent_id")
        parent_path = _text(parent_ref, "path") or _text(extra, "parent_path")
        path_suffix = (parent_path.split("root:", 1)[-1] if parent_path else "").strip("/")
        parent = drive
        accumulated: list[str] = []
        for index, part in enumerate(PurePosixPath(path_suffix).parts if path_suffix else ()):
            accumulated.append(part)
            folder_path = "/".join(accumulated)
            folder_id = (
                parent_id
                if parent_id is not None
                and index == len(PurePosixPath(path_suffix).parts) - 1
                else folder_path
            )
            folder = state.source_node(
                GraphEntityType.SHAREPOINT_FOLDER,
                folder_id,
                title=part,
                attributes={"placeholder": True, "relative_path": folder_path},
            )
            self._edge(state, RelationType.CONTAINS, parent, folder)
            parent = folder
        if parent == drive and parent_id:
            parent = state.source_node(
                GraphEntityType.SHAREPOINT_FOLDER,
                parent_id,
                title=parent_id,
                attributes={"placeholder": True},
            )
            self._edge(state, RelationType.CONTAINS, drive, parent)
        self._edge(state, RelationType.CONTAINS, parent, item)
        self._version(state, item, document_version)
        return item


class LocalSourceProjector(_BaseSourceProjector):
    entity_type = GraphEntityType.LOCAL_FILE

    def project(self, state, document, data_source, document_version):  # type: ignore[no-untyped-def]
        extra = document.provenance.extra
        root = state.source_node(
            GraphEntityType.LOCAL_ROOT,
            state.context.source_scope_id,
            title=state.context.source_scope_id,
            attributes={"relative_path": "."},
        )
        self._edge(state, RelationType.CONTAINS, data_source, root)
        relative_path = _portable_path(
            _text(extra, "relative_path") or state.context.source_item_id
        )
        item = self._source_item(
            state,
            document,
            provider_id=relative_path,
            title=PurePosixPath(relative_path).name,
            attributes={"relative_path": relative_path, **_selected(extra, "suffix")},
        )
        parent = root
        accumulated: list[str] = []
        for part in PurePosixPath(relative_path).parts[:-1]:
            accumulated.append(part)
            directory_path = "/".join(accumulated)
            directory = state.source_node(
                GraphEntityType.LOCAL_DIRECTORY,
                directory_path,
                title=part,
                attributes={"relative_path": directory_path},
            )
            self._edge(state, RelationType.CONTAINS, parent, directory)
            parent = directory
        self._edge(state, RelationType.CONTAINS, parent, item)
        self._version(state, item, document_version)
        return item


def source_entity_type(connector_type: str) -> GraphEntityType:
    return {
        "confluence": GraphEntityType.CONFLUENCE_PAGE,
        "jira": GraphEntityType.JIRA_ISSUE,
        "github": GraphEntityType.GITHUB_FILE,
        "sharepoint": GraphEntityType.SHAREPOINT_FILE,
        "local": GraphEntityType.LOCAL_FILE,
    }.get(connector_type.casefold(), GraphEntityType.GENERIC_SOURCE_ITEM)


def relation_entity_type(
    connector_type: str,
    relation_type: RelationType,
    target_type: str,
) -> GraphEntityType:
    if relation_type == RelationType.HAS_ATTACHMENT or "attachment" in target_type.casefold():
        return {
            "confluence": GraphEntityType.CONFLUENCE_ATTACHMENT,
            "jira": GraphEntityType.JIRA_ATTACHMENT,
        }.get(connector_type.casefold(), GraphEntityType.GENERIC_SOURCE_ITEM)
    return source_entity_type(connector_type)


def source_provider_id(connector_type: str, source_item_id: str) -> str:
    """Recover the provider ID/path carried by a canonical source item identity."""

    connector = connector_type.casefold()
    value = source_item_id.strip()
    if connector in {"confluence", "sharepoint"} and "/" in value:
        return value.rstrip("/").rsplit("/", 1)[-1]
    if connector == "jira" and value.casefold().startswith("jira://"):
        return value.rstrip("/").rsplit("/", 1)[-1]
    if connector == "github" and value.casefold().startswith("github://"):
        parts = value.removeprefix("github://").split("/", 2)
        return parts[2] if len(parts) == 3 else value
    return value


def _text(values: Mapping[str, Any], *keys: str) -> str | None:
    for key in keys:
        value = values.get(key)
        if value is not None and str(value).strip():
            return str(value).strip()
    return None


def _selected(values: Mapping[str, Any], *keys: str) -> dict[str, Any]:
    return {
        key: _display_value(values[key])
        for key in keys
        if values.get(key) is not None
    }


def _display_value(value: Any) -> str | int | float | bool:
    if isinstance(value, (str, int, float, bool)):
        return value
    isoformat = getattr(value, "isoformat", None)
    if callable(isoformat):
        return str(isoformat())
    return str(value)


def _mapping(value: object) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _mapping_sequence(value: object) -> tuple[Mapping[str, Any], ...]:
    if not isinstance(value, (list, tuple)):
        return ()
    return tuple(item for item in value if isinstance(item, Mapping))


def _sequence(value: object) -> tuple[str, ...]:
    if not isinstance(value, (list, tuple)):
        return ()
    return tuple(str(item).strip() for item in value if str(item).strip())


def _portable_path(value: str) -> str:
    normalized = value.replace("\\", "/").lstrip("/")
    parts = tuple(part for part in PurePosixPath(normalized).parts if part not in {"", "."})
    if not parts or ".." in parts:
        raise ValueError("provider graph path must be a portable relative path")
    return "/".join(parts)


def _is_attachment(state: GraphProjectionState, extra: Mapping[str, Any]) -> bool:
    return (
        state.context.document_kind.value == "attachment"
        or str(extra.get("binding_kind") or "").casefold() == "attachment"
    )


__all__ = [
    "ConfluenceSourceProjector",
    "GenericSourceProjector",
    "GitHubSourceProjector",
    "GraphSourceProjector",
    "GraphSourceProjectorRegistry",
    "JiraSourceProjector",
    "LocalSourceProjector",
    "SharePointSourceProjector",
    "default_graph_source_projector_registry",
    "source_entity_type",
    "source_provider_id",
    "relation_entity_type",
]
