"""Source topology projectors for repository and filesystem providers."""

from __future__ import annotations

from pathlib import PurePosixPath

from harborrag_core.chunking import RelationType
from harborrag_core.ingestion import GraphEntityType

from .source_projector_support import (
    BaseSourceProjector,
    mapping_value,
    portable_path,
    project_structure_chain,
    selected_values,
    source_item_provider_id,
    text_value,
)


class GitHubSourceProjector(BaseSourceProjector):
    entity_type = GraphEntityType.GITHUB_FILE

    def project(self, state, document, data_source, document_version):  # type: ignore[no-untyped-def]
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

        relative_path = portable_path(text_value(extra, "path") or source_item_provider_id(state))
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
        # Membership: the repository holds every file, flat, so counting them is one hop.
        # A directory is not a document, so it never joins that set -- it lives on the
        # structure axis only.
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
            # A ref is neither a document nor a position in the file tree; it is version
            # metadata that belongs to the repository. Keeping it off CONTAINS is what
            # keeps that set homogeneous, and PARENT_OF is the closest honest axis. This
            # is the one place the two-axis split reads oddly -- see the design note.
            self.edge(state, RelationType.PARENT_OF, repository, ref)
            self.edge(state, RelationType.POINTS_TO, ref, commit, explicit=True)
            self.edge(state, RelationType.RESOLVED_AT, document_version, commit, explicit=True)
        self.version(state, item, document_version)
        return item


class SharePointSourceProjector(BaseSourceProjector):
    entity_type = GraphEntityType.SHAREPOINT_FILE

    def project(self, state, document, data_source, document_version):  # type: ignore[no-untyped-def]
        extra = document.provenance.extra
        site_id = text_value(extra, "site_id") or "unknown-site"
        drive_id = text_value(extra, "drive_id") or "unknown-drive"
        site = state.source_node(
            GraphEntityType.SHAREPOINT_SITE,
            site_id,
            title=text_value(extra, "site_name") or site_id,
        )
        drive = state.source_node(
            GraphEntityType.SHAREPOINT_DRIVE,
            drive_id,
            title=text_value(extra, "drive_name") or drive_id,
            attributes=selected_values(extra, "drive_type"),
        )
        self.edge(state, RelationType.CONTAINS, data_source, site)
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
        path_suffix = (parent_path.split("root:", 1)[-1] if parent_path else "").strip("/")
        path_parts = PurePosixPath(path_suffix).parts if path_suffix else ()
        accumulated: list[str] = []
        folders = []
        # Key every folder by its drive-relative path, the way GitHub and local directory
        # chains do. Keying only the terminal element by its provider ``parent_id`` split
        # one folder into two nodes: the same folder is terminal for a file directly
        # inside it and an ancestor for anything deeper, so it was reached under two
        # different keys and its CONTAINS chain forked.
        for part in path_parts:
            accumulated.append(part)
            folder_path = "/".join(accumulated)
            folders.append(
                state.source_node(
                    GraphEntityType.SHAREPOINT_FOLDER,
                    folder_path,
                    title=part,
                    attributes={"placeholder": True, "relative_path": folder_path},
                )
            )
        # No parent path to key on. The provider id is all that is left, so a folder
        # reached this way still cannot converge with the same folder reached by path;
        # that is a metadata gap in the item, not a choice between two keyings.
        if not folders and parent_id:
            folders.append(
                state.source_node(
                    GraphEntityType.SHAREPOINT_FOLDER,
                    parent_id,
                    title=parent_id,
                    attributes={"placeholder": True},
                )
            )
        # Membership from the drive; folders carry position only.
        self.edge(state, RelationType.CONTAINS, drive, item)
        project_structure_chain(self, state, container=drive, item=item, ancestors=folders)
        self.version(state, item, document_version)
        return item


class LocalSourceProjector(BaseSourceProjector):
    entity_type = GraphEntityType.LOCAL_FILE

    def project(self, state, document, data_source, document_version):  # type: ignore[no-untyped-def]
        extra = document.provenance.extra
        root = state.source_node(
            GraphEntityType.LOCAL_ROOT,
            state.context.source_scope_id,
            title=state.context.source_scope_id,
            attributes={"relative_path": "."},
        )
        self.edge(state, RelationType.CONTAINS, data_source, root)
        relative_path = portable_path(
            text_value(extra, "relative_path") or source_item_provider_id(state)
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
        # Membership from the root; directories carry position only.
        self.edge(state, RelationType.CONTAINS, root, item)
        project_structure_chain(self, state, container=root, item=item, ancestors=directories)
        self.version(state, item, document_version)
        return item


__all__ = ["GitHubSourceProjector", "LocalSourceProjector", "SharePointSourceProjector"]
