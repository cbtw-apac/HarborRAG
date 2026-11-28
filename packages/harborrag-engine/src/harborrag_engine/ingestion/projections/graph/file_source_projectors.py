"""Source topology projectors for repository and filesystem providers.

Each file sits on two axes. CONTAINS is membership, held flat by the file's
repository, drive or root. PARENT_OF is position, through the directories or
folders above the file.
"""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import PurePosixPath
from typing import TYPE_CHECKING, Any
from urllib.parse import unquote

from harborrag_core.chunking import RelationType
from harborrag_core.ingestion import GraphEntityType

from .source_projector_support import (
    BaseSourceProjector,
    identity_value,
    mapping_value,
    portable_path,
    project_structure_chain,
    selected_values,
    source_item_provider_id,
    text_value,
)

if TYPE_CHECKING:
    from harborrag_core.domain.document import Document
    from harborrag_core.ingestion import GraphNodeRecord

    from .graph_state import GraphProjectionState

_DRIVE_ROOT_MARKER = "root:"


class GitHubSourceProjector(BaseSourceProjector):
    """Project GitHub files under their owner, repository, directories and ref.

    Attributes:
        entity_type: Graph entity type of the file node.
    """

    entity_type = GraphEntityType.GITHUB_FILE

    def project(
        self,
        state: GraphProjectionState,
        document: Document,
        data_source: GraphNodeRecord,
        document_version: GraphNodeRecord,
    ) -> GraphNodeRecord:
        """Project one GitHub file with its directory chain and resolved commit.

        The repository CONTAINS every file flat, so counting files is one hop.
        Directories are not documents, so they sit on PARENT_OF only. A ref is
        version metadata of the repository, so it hangs off the repository by
        PARENT_OF, and its POINTS_TO commit is one edge that every file at that
        commit shares.

        Args:
            state: Projection being built for this document version.
            document: Canonical document of the file.
            data_source: Node of the connection the file came from.
            document_version: Node of the document version being projected.

        Returns:
            The file's source item node.
        """
        extra = document.provenance.extra
        owner_id = text_value(extra, "owner") or "unknown-owner"
        repo_id = text_value(extra, "repository_id", "repo") or "unknown-repository"
        owner = state.source_node(GraphEntityType.GITHUB_OWNER, owner_id, title=owner_id)
        repository = state.source_node(
            GraphEntityType.GITHUB_REPOSITORY,
            repo_id,
            title=text_value(extra, "repo") or repo_id,
            attributes=selected_values(extra, "default_branch"),
        )
        self.edge(state, RelationType.CONTAINS, data_source, owner)
        self.edge(state, RelationType.CONTAINS, owner, repository)

        relative_path = portable_path(
            identity_value(extra, "path") or source_item_provider_id(state)
        )
        item = self.source_item(
            state,
            document,
            provider_id=relative_path,
            attributes={"relative_path": relative_path, **selected_values(extra, "sha", "mode")},
        )
        path = PurePosixPath(relative_path)
        accumulated: list[str] = []
        directories = []
        for part in path.parts[:-1]:
            accumulated.append(part)
            directory_path = "/".join(accumulated)
            directories.append(
                state.source_node(
                    GraphEntityType.GITHUB_DIRECTORY,
                    directory_path,
                    title=part,
                    attributes={"relative_path": directory_path},
                )
            )
        self.edge(state, RelationType.CONTAINS, repository, item)
        project_structure_chain(self, state, container=repository, item=item, ancestors=directories)

        ref_name = text_value(extra, "ref")
        commit_sha = text_value(extra, "commit_sha")
        if ref_name and commit_sha:
            ref = state.source_node(GraphEntityType.GITHUB_REF, ref_name, title=ref_name)
            commit = state.source_node(
                GraphEntityType.GITHUB_COMMIT,
                commit_sha,
                title=commit_sha[:12],
            )
            self.edge(state, RelationType.PARENT_OF, repository, ref)
            self.edge(state, RelationType.POINTS_TO, ref, commit)
            self.edge(state, RelationType.RESOLVED_AT, document_version, commit, explicit=True)
        self.version(state, item, document_version)
        return item


class SharePointSourceProjector(BaseSourceProjector):
    """Project SharePoint drive files and site pages under their site.

    Attributes:
        entity_type: Graph entity type of a drive file node.
    """

    entity_type = GraphEntityType.SHAREPOINT_FILE

    def project(
        self,
        state: GraphProjectionState,
        document: Document,
        data_source: GraphNodeRecord,
        document_version: GraphNodeRecord,
    ) -> GraphNodeRecord:
        """Project one drive file under its drive and folders, or one page under its site.

        The drive CONTAINS every file, and folders carry position only. Each
        folder is keyed by its drive-relative path, so one folder is one node
        whether a file sits in it or deeper. An item with no parent path gets one
        placeholder folder keyed by its parent's ID, which cannot converge with
        the same folder reached by path.

        Args:
            state: Projection being built for this document version.
            document: Canonical document of the file or page.
            data_source: Node of the connection the item came from.
            document_version: Node of the document version being projected.

        Returns:
            The file's or page's source item node.
        """
        extra = document.provenance.extra
        site_id = text_value(extra, "site_id") or "unknown-site"
        site = state.source_node(
            GraphEntityType.SHAREPOINT_SITE,
            site_id,
            title=text_value(extra, "site_name") or site_id,
        )
        self.edge(state, RelationType.CONTAINS, data_source, site)
        if text_value(extra, "record_kind") == "page":
            return self._project_page(state, document, extra, site, document_version)

        drive_id = text_value(extra, "drive_id") or "unknown-drive"
        drive = state.source_node(
            GraphEntityType.SHAREPOINT_DRIVE,
            drive_id,
            title=text_value(extra, "drive_name") or drive_id,
            attributes=selected_values(extra, "drive_type"),
        )
        self.edge(state, RelationType.CONTAINS, site, drive)

        item_id = text_value(extra, "item_id")
        item = self.source_item(
            state,
            document,
            provider_id=item_id,
            attributes=selected_values(extra, "item_name", "etag", "ctag"),
        )
        parent_ref = mapping_value(extra.get("parent"))
        parent_id = text_value(parent_ref, "id") or text_value(extra, "parent_id")
        parent_path = text_value(parent_ref, "path") or text_value(extra, "parent_path")
        folders = [
            state.source_node(
                GraphEntityType.SHAREPOINT_FOLDER,
                folder_path,
                title=PurePosixPath(folder_path).name,
                attributes={"placeholder": True, "relative_path": folder_path},
            )
            for folder_path in _drive_folder_paths(parent_path)
        ]
        if not folders and parent_id:
            folders.append(
                state.source_node(
                    GraphEntityType.SHAREPOINT_FOLDER,
                    parent_id,
                    title=parent_id,
                    attributes={"placeholder": True},
                )
            )
        self.edge(state, RelationType.CONTAINS, drive, item)
        project_structure_chain(self, state, container=drive, item=item, ancestors=folders)
        self.version(state, item, document_version)
        return item

    def _project_page(
        self,
        state: GraphProjectionState,
        document: Document,
        extra: Mapping[str, Any],
        site: GraphNodeRecord,
        document_version: GraphNodeRecord,
    ) -> GraphNodeRecord:
        """Project a modern page straight under its site, which has no drive or folder."""
        item = self.source_item(
            state,
            document,
            provider_id=text_value(extra, "page_id"),
            attributes=selected_values(extra, "item_name", "etag"),
            entity_type=GraphEntityType.SHAREPOINT_PAGE,
        )
        self.edge(state, RelationType.CONTAINS, site, item)
        project_structure_chain(self, state, container=site, item=item, ancestors=())
        self.version(state, item, document_version)
        return item


class LocalSourceProjector(BaseSourceProjector):
    """Project local files under their source root and directories.

    Attributes:
        entity_type: Graph entity type of the file node.
    """

    entity_type = GraphEntityType.LOCAL_FILE

    def project(
        self,
        state: GraphProjectionState,
        document: Document,
        data_source: GraphNodeRecord,
        document_version: GraphNodeRecord,
    ) -> GraphNodeRecord:
        """Project one local file under its root and directory chain.

        The root CONTAINS every file, and directories carry position only.

        Args:
            state: Projection being built for this document version.
            document: Canonical document of the file.
            data_source: Node of the connection the file came from.
            document_version: Node of the document version being projected.

        Returns:
            The file's source item node.
        """
        extra = document.provenance.extra
        root = state.source_node(
            GraphEntityType.LOCAL_ROOT,
            state.context.source_scope_id,
            title=state.context.source_scope_id,
            attributes={"relative_path": "."},
        )
        self.edge(state, RelationType.CONTAINS, data_source, root)
        relative_path = portable_path(
            identity_value(extra, "relative_path") or source_item_provider_id(state)
        )
        item = self.source_item(
            state,
            document,
            provider_id=relative_path,
            title=PurePosixPath(relative_path).name,
            attributes={"relative_path": relative_path, **selected_values(extra, "suffix")},
        )
        accumulated: list[str] = []
        directories = []
        for part in PurePosixPath(relative_path).parts[:-1]:
            accumulated.append(part)
            directory_path = "/".join(accumulated)
            directories.append(
                state.source_node(
                    GraphEntityType.LOCAL_DIRECTORY,
                    directory_path,
                    title=part,
                    attributes={"relative_path": directory_path},
                )
            )
        self.edge(state, RelationType.CONTAINS, root, item)
        project_structure_chain(self, state, container=root, item=item, ancestors=directories)
        self.version(state, item, document_version)
        return item


def _drive_folder_paths(parent_path: str | None) -> list[str]:
    """Return the drive-relative path of each folder above an item, outermost first.

    Graph percent-encodes ``parentReference.path``, so it is decoded exactly once
    here, as the connector does. A folder literally named ``Budget%2026`` arrives
    as ``Budget%252026`` and keeps its ``%``.
    """
    if not parent_path:
        return []
    drive_relative = unquote(parent_path.split(_DRIVE_ROOT_MARKER, 1)[-1]).strip("/")
    parts = PurePosixPath(drive_relative).parts if drive_relative else ()
    return ["/".join(parts[: depth + 1]) for depth in range(len(parts))]
