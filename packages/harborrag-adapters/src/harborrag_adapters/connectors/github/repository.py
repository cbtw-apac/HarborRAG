"""Repository metadata, tree traversal, and blob-loading operations."""

from __future__ import annotations

import base64
import binascii
import logging
from collections.abc import Iterator
from typing import Any

from harborrag_adapters.connectors.exceptions import DocumentProcessingError, FetchError
from harborrag_adapters.connectors.schemas import ConnectorQuery
from harborrag_core.domain.source import SourceRecord

from .client import GitHubClient
from .config import GitHubRepositoryConfig
from .filters import should_process_file
from .mappers import build_source_record, tree_sha_from_commit
from .repository_paths import (
    GITHUB_BLOB_LIMIT_BYTES,
    blob_endpoint,
    commit_endpoint,
    content_endpoint,
    is_blob,
    is_tree,
    normalize_repo_path,
    repo_endpoint,
    tree_endpoint,
)

logger = logging.getLogger("harborrag.adapters.connectors.github")


class GitHubRepositoryAPI:
    """Repository-scoped GitHub traversal, filtering, and blob loading."""

    def __init__(self, config: GitHubRepositoryConfig, client: GitHubClient) -> None:
        """Bind repository operations to a client and validated config."""
        self.config = config
        self.client = client
        self.owner = str(config.owner)
        self.repo = str(config.repo)
        self._repository: dict[str, Any] | None = None
        self._commit: dict[str, Any] | None = None
        self._resolved_ref: str | None = None

    @property
    def resolved_ref(self) -> str | None:
        """Return the configured ref resolved for the current ingestion run."""
        return self._resolved_ref

    def iter_tree(self, tree_sha: str, *, recursive: bool) -> Iterator[dict[str, Any]]:
        """Yield Git tree items, falling back if GitHub truncates recursion."""
        params = {"recursive": "1"} if recursive else None
        response = self.client.get_json(
            tree_endpoint(self.owner, self.repo, tree_sha),
            params=params,
        )
        if not isinstance(response, dict):
            raise FetchError("GitHub tree response was not an object")

        if recursive and response.get("truncated"):
            logger.warning(
                "GitHub recursive tree for %s/%s was truncated; falling back to subtree traversal",
                self.owner,
                self.repo,
            )
            yield from self._walk_tree_non_recursive(tree_sha)
            return

        yield from response.get("tree", [])

    def load_blob(self, sha: str, *, known_size: int | None = None) -> bytes:
        """Fetch and decode a Git blob while enforcing GitHub and config limits.

        When ``known_size`` is supplied (e.g. from a prior contents-API lookup
        for a single path), the configured size limit is enforced before the
        blob endpoint is ever called so oversized files are rejected without
        being downloaded and JSON-parsed first.
        """
        if known_size is not None:
            self.enforce_size_limit(sha, known_size)

        blob = self.client.get_json(blob_endpoint(self.owner, self.repo, sha))
        if not isinstance(blob, dict):
            raise FetchError("GitHub blob response was not an object")

        size = int(blob.get("size") or 0)
        if size > GITHUB_BLOB_LIMIT_BYTES:
            raise DocumentProcessingError(f"GitHub blob {sha} exceeds the 100 MB REST API limit")
        self.enforce_size_limit(sha, size)
        encoding = str(blob.get("encoding") or "")
        content = str(blob.get("content") or "")
        if encoding != "base64":
            raise DocumentProcessingError(
                f"GitHub blob {sha} returned unsupported encoding {encoding!r}"
            )
        try:
            return base64.b64decode(content, validate=False)
        except (binascii.Error, ValueError) as exc:
            raise DocumentProcessingError(f"GitHub blob {sha} is not valid base64") from exc

    def content_file_item(self, path: str, *, ref: str) -> dict[str, Any]:
        """Resolve one path through the contents API and normalize it as a blob."""
        response = self.client.get_json(
            content_endpoint(self.owner, self.repo, path),
            params={"ref": ref},
        )
        if not isinstance(response, dict):
            raise DocumentProcessingError(f"GitHub path {path!r} is not a file")
        if not is_blob(response):
            raise DocumentProcessingError(f"GitHub path {path!r} is not a file")
        return {
            "path": normalize_repo_path(str(response.get("path") or path)),
            "sha": response.get("sha"),
            "size": int(response.get("size") or 0),
            "mode": "100644",
            "type": "blob",
        }

    def source_record(
        self,
        item: dict[str, Any],
        *,
        ref: str,
        commit: dict[str, Any],
    ) -> SourceRecord:
        """Map a tree item and its anchoring commit to a source record."""
        return build_source_record(
            item,
            owner=self.owner,
            repo=self.repo,
            ref=ref,
            commit_sha=str(commit.get("sha") or ""),
            commit=commit,
        )

    def record_for_path(self, path: str) -> SourceRecord:
        """Resolve one repository path and return its source record."""
        repository = self.resolve_repository()
        commit = self.resolve_commit(repository)
        ref = self.resolved_ref or str(commit.get("sha") or "")
        item = self.content_file_item(path, ref=ref)
        return self.source_record(item, ref=ref, commit=commit)

    def resolve_repository(self) -> dict[str, Any]:
        """Fetch and cache repository metadata needed for default branch lookup."""
        if self._repository is not None:
            return self._repository
        response = self.client.get_json(repo_endpoint(self.owner, self.repo))
        if not isinstance(response, dict) or not response.get("full_name"):
            raise FetchError("GitHub repository response did not include full_name")
        self._repository = response
        return response

    def resolve_commit(self, repository: dict[str, Any]) -> dict[str, Any]:
        """Resolve and cache the commit that anchors this ingestion run."""
        if self._commit is not None:
            return self._commit

        ref = (
            self.config.commit_sha
            or self.config.ref
            or self.config.branch
            or repository.get("default_branch")
        )
        if not ref:
            raise FetchError("GitHub repository did not include default_branch")

        response = self.client.get_json(
            commit_endpoint(self.owner, self.repo, str(ref)),
            params={"per_page": 1},
        )
        if not isinstance(response, dict) or not response.get("sha"):
            raise FetchError("GitHub commit response did not include sha")
        if not tree_sha_from_commit(response):
            raise FetchError("GitHub commit response did not include tree sha")
        self._commit = response
        self._resolved_ref = str(ref)
        return response

    def should_process_file(
        self,
        item: dict[str, Any],
        query: ConnectorQuery,
        *,
        commit: dict[str, Any],
    ) -> bool:
        """Apply query/config filters to one GitHub tree or content item."""
        return should_process_file(self.config, item, query, commit=commit)

    def enforce_size_limit(self, label: str, size: int) -> None:
        """Prevent large blobs from being materialized by direct loads."""
        if self.config.max_file_size_bytes is None or not size:
            return
        if size > self.config.max_file_size_bytes:
            raise DocumentProcessingError(
                f"GitHub file {label!r} size {size} exceeds "
                f"max_file_size_bytes {self.config.max_file_size_bytes}"
            )

    def _walk_tree_non_recursive(
        self,
        tree_sha: str,
        *,
        prefix: str = "",
    ) -> Iterator[dict[str, Any]]:
        """Walk subtrees manually when GitHub's recursive tree API is truncated."""
        response = self.client.get_json(tree_endpoint(self.owner, self.repo, tree_sha))
        if not isinstance(response, dict):
            raise FetchError("GitHub tree response was not an object")

        for item in response.get("tree", []):
            path = normalize_repo_path(
                f"{prefix}/{item.get('path')}" if prefix else item.get("path")
            )
            item = {**item, "path": path}
            if is_tree(item):
                yield from self._walk_tree_non_recursive(
                    str(item.get("sha") or ""),
                    prefix=path,
                )
            else:
                yield item
