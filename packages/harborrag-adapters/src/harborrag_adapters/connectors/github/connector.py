"""GitHub repository discovery and raw-blob loading orchestration."""

from __future__ import annotations

import logging
from collections.abc import Iterator

from harborrag_core.domain.raw_document import RawDocument
from harborrag_core.domain.source import SourceRecord

from harborrag_adapters.connectors.base import BaseConnector
from harborrag_adapters.connectors.exceptions import DocumentProcessingError
from harborrag_adapters.connectors.schemas import ConnectorCapabilities, ConnectorQuery

from .client import GitHubClient, _RequestsGitHubClient
from .config import GitHubRepositoryConfig
from .filters import file_paths_from_query
from .mappers import (
    build_document_metadata,
    commit_timestamp,
    file_path_from_record,
    file_sha_from_record,
    tree_sha_from_commit,
)
from .repository import GitHubRepositoryAPI
from .utils import (
    github_blob_url,
    guess_mime_type,
    is_blob,
    normalize_repo_path,
    path_in_scope,
)

logger = logging.getLogger("harborrag.adapters.connectors.github")


class GitHubConnector(BaseConnector):
    """Connector for GitHub repository file ingestion through the REST API.

    Discovery resolves one repository ref and walks Git trees to produce file
    records. Loading fetches blob bytes by SHA so the loaded content matches the
    discovered commit instead of whatever the branch points to later.
    """

    provider_name = "github"
    capabilities = ConnectorCapabilities(
        pagination=True,
        incremental_sync=True,
        full_sync=True,
        relationships=True,
        local_files=False,
    )

    def __init__(
        self,
        config: GitHubRepositoryConfig,
        *,
        client: GitHubClient | None = None,
    ) -> None:
        """Initialize repository operations with an optional client override."""
        self.config = config
        self.owner = str(config.owner)
        self.repo = str(config.repo)
        self.client = client or _RequestsGitHubClient(config)
        self._github = GitHubRepositoryAPI(config, self.client)

    def close(self) -> None:
        """Release the underlying GitHub HTTP client's resources."""
        close = getattr(self.client, "close", None)
        if callable(close):
            close()

    def discover(self, query: ConnectorQuery | None = None) -> Iterator[SourceRecord]:
        """Discover repository file records from explicit paths or tree walking."""
        query = query or ConnectorQuery()
        repository = self._github.resolve_repository()
        commit = self._github.resolve_commit(repository)
        ref = self._github.resolved_ref or str(commit.get("sha") or "")
        logger.info(
            "Discovering GitHub files in %s/%s at %s",
            self.owner,
            self.repo,
            ref,
        )

        yielded = 0
        paths = file_paths_from_query(query)
        if paths:
            for path in paths:
                item = self._github.content_file_item(path, ref=ref)
                if self._github.should_process_file(item, query, commit=commit):
                    yield self._github.source_record(item, ref=ref, commit=commit)
                    yielded += 1
                    if query.limit is not None and yielded >= query.limit:
                        return
            return

        commit_updated_at = commit_timestamp(commit)
        if query.updated_after and commit_updated_at:
            if commit_updated_at <= query.updated_after:
                return

        root_path = normalize_repo_path(query.path or self.config.root_path)
        tree_sha = tree_sha_from_commit(commit)
        tree_recursive = query.recursive or bool(root_path)
        for item in self._github.iter_tree(tree_sha, recursive=tree_recursive):
            if not is_blob(item):
                continue
            if not path_in_scope(
                str(item.get("path") or ""), root_path, recursive=True
            ):
                continue
            if not query.recursive:
                if not path_in_scope(
                    str(item.get("path") or ""),
                    root_path,
                    recursive=False,
                ):
                    continue
            if not self._github.should_process_file(item, query, commit=commit):
                continue

            yield self._github.source_record(item, ref=ref, commit=commit)
            yielded += 1
            if query.limit is not None and yielded >= query.limit:
                return

    def load(self, record: SourceRecord) -> RawDocument:
        """Load one repository blob as a raw document."""
        repository = self._github.resolve_repository()
        commit = self._github.resolve_commit(repository)
        ref = self._github.resolved_ref or str(commit.get("sha") or "")
        path = file_path_from_record(record)
        sha = file_sha_from_record(record)

        if not sha:
            item = self._github.content_file_item(path, ref=ref)
            sha = str(item.get("sha") or "")
        else:
            item = {
                "path": path,
                "sha": sha,
                "size": int(record.metadata.get("size") or 0),
                "mode": record.metadata.get("mode"),
            }

        if not sha:
            raise DocumentProcessingError(
                f"GitHub source record {record.id!r} does not include a blob sha"
            )
        known_size = int(item.get("size") or 0)
        self._github.enforce_size_limit(path, known_size)

        logger.info("Loading GitHub file %s/%s:%s", self.owner, self.repo, path)
        content = self._github.load_blob(sha, known_size=known_size or None)
        metadata = build_document_metadata(
            item,
            owner=self.owner,
            repo=self.repo,
            ref=ref,
            commit=commit,
            repository=repository,
        )
        if not metadata.size:
            metadata.size = len(content)
        source_url = github_blob_url(
            web_url=self.config.web_url,
            owner=self.owner,
            repo=self.repo,
            ref=ref,
            path=path,
        )

        return RawDocument(
            id=record.id,
            source=source_url,
            content=content,
            content_type=guess_mime_type(path),
            metadata=metadata.to_dict(),
            raw=item,
        )

    def load_by_paths(self, paths: list[str]) -> Iterator[RawDocument]:
        """Load files for callers that already know repository paths."""
        for path in paths:
            yield self.load(self._github.record_for_path(path))
