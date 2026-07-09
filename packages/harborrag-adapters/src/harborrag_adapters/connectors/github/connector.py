from __future__ import annotations

import base64
import binascii
import logging
import time
from collections.abc import Iterator
from typing import Any, Protocol

import requests
from harborrag_core.domain.raw_document import RawDocument
from harborrag_core.domain.source import SourceRecord

from harborrag_adapters.connectors.base import BaseConnector
from harborrag_adapters.connectors.exceptions import (
    AuthenticationError,
    DocumentProcessingError,
    FetchError,
    RateLimitError,
)
from harborrag_adapters.connectors.http_utils import (
    require_same_origin_url,
    retry_delay_seconds,
    safe_error_detail,
)
from harborrag_adapters.connectors.schemas import ConnectorCapabilities, ConnectorQuery

from .config import GitHubRepositoryConfig
from .mappers import (
    build_document_metadata,
    build_source_record,
    commit_timestamp,
    file_path_from_record,
    file_sha_from_record,
    tree_sha_from_commit,
)
from .utils import (
    GITHUB_BLOB_LIMIT_BYTES,
    blob_endpoint,
    commit_endpoint,
    content_endpoint,
    file_extension,
    guess_mime_type,
    is_blob,
    is_tree,
    normalize_repo_path,
    path_in_scope,
    path_matches_patterns,
    path_matches_query,
    repo_endpoint,
    tree_endpoint,
)


logger = logging.getLogger("harborrag.adapters.connectors.github")
_RETRYABLE_STATUS = {429, 500, 502, 503, 504}


class GitHubClient(Protocol):
    """Small API surface needed by ``GitHubConnector``."""

    def get_json(
        self,
        endpoint: str,
        *,
        params: dict[str, Any] | None = None,
    ) -> dict[str, Any] | list[dict[str, Any]]:
        ...


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
        self.config = config
        self.owner = str(config.owner)
        self.repo = str(config.repo)
        self.client = client or _RequestsGitHubClient(config)
        self._repository: dict[str, Any] | None = None
        self._commit: dict[str, Any] | None = None
        self._resolved_ref: str | None = None

    def discover(self, query: ConnectorQuery | None = None) -> Iterator[SourceRecord]:
        """Discover repository file records from explicit paths or tree walking."""
        query = query or ConnectorQuery()
        repository = self._resolve_repository()
        commit = self._resolve_commit(repository)
        ref = self._resolved_ref or str(commit.get("sha") or "")
        logger.info(
            "Discovering GitHub files in %s/%s at %s",
            self.owner,
            self.repo,
            ref,
        )

        yielded = 0
        paths = self._file_paths_from_query(query)
        if paths:
            for path in paths:
                item = self._content_file_item(path, ref=ref)
                if self._should_process_file(item, query, commit=commit):
                    yield self._source_record(item, ref=ref, commit=commit)
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
        for item in self._iter_tree(tree_sha, recursive=tree_recursive):
            if not is_blob(item):
                continue
            if not path_in_scope(str(item.get("path") or ""), root_path, recursive=True):
                continue
            if not query.recursive:
                if not path_in_scope(
                    str(item.get("path") or ""),
                    root_path,
                    recursive=False,
                ):
                    continue
            if not self._should_process_file(item, query, commit=commit):
                continue

            yield self._source_record(item, ref=ref, commit=commit)
            yielded += 1
            if query.limit is not None and yielded >= query.limit:
                return

    def load(self, record: SourceRecord) -> RawDocument:
        """Load one repository blob as a raw document."""
        repository = self._resolve_repository()
        commit = self._resolve_commit(repository)
        ref = self._resolved_ref or str(commit.get("sha") or "")
        path = file_path_from_record(record)
        sha = file_sha_from_record(record)

        if not sha:
            item = self._content_file_item(path, ref=ref)
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
        self._enforce_size_limit(path, int(item.get("size") or 0))

        logger.info("Loading GitHub file %s/%s:%s", self.owner, self.repo, path)
        content = self._load_blob(sha)
        metadata = build_document_metadata(
            item,
            owner=self.owner,
            repo=self.repo,
            web_url=self.config.web_url,
            ref=ref,
            commit=commit,
            repository=repository,
        )
        metadata["size"] = len(content) if not metadata.get("size") else metadata["size"]

        return RawDocument(
            id=record.id,
            source=str(record.metadata.get("html_url") or metadata["html_url"]),
            content=content,
            content_type=guess_mime_type(path),
            metadata=metadata,
            raw=item,
        )

    def load_by_paths(self, paths: list[str]) -> Iterator[RawDocument]:
        """Convenience loader for callers that already know repository paths."""
        for path in paths:
            yield self.load(self._record_for_path(path))

    def _iter_tree(self, tree_sha: str, *, recursive: bool) -> Iterator[dict[str, Any]]:
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
                "GitHub recursive tree for %s/%s was truncated; "
                "falling back to subtree traversal",
                self.owner,
                self.repo,
            )
            yield from self._walk_tree_non_recursive(tree_sha)
            return

        yield from response.get("tree", [])

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

    def _load_blob(self, sha: str) -> bytes:
        """Fetch and decode a Git blob while enforcing GitHub and config limits."""
        blob = self.client.get_json(blob_endpoint(self.owner, self.repo, sha))
        if not isinstance(blob, dict):
            raise FetchError("GitHub blob response was not an object")

        size = int(blob.get("size") or 0)
        if size > GITHUB_BLOB_LIMIT_BYTES:
            raise DocumentProcessingError(
                f"GitHub blob {sha} exceeds the 100 MB REST API limit"
            )
        self._enforce_size_limit(sha, size)
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

    def _content_file_item(self, path: str, *, ref: str) -> dict[str, Any]:
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

    def _source_record(
        self,
        item: dict[str, Any],
        *,
        ref: str,
        commit: dict[str, Any],
    ) -> SourceRecord:
        return build_source_record(
            item,
            owner=self.owner,
            repo=self.repo,
            web_url=self.config.web_url,
            ref=ref,
            commit_sha=str(commit.get("sha") or ""),
            commit=commit,
        )

    def _record_for_path(self, path: str) -> SourceRecord:
        repository = self._resolve_repository()
        commit = self._resolve_commit(repository)
        ref = self._resolved_ref or str(commit.get("sha") or "")
        item = self._content_file_item(path, ref=ref)
        return self._source_record(item, ref=ref, commit=commit)

    def _resolve_repository(self) -> dict[str, Any]:
        """Fetch and cache repository metadata needed for default branch lookup."""
        if self._repository is not None:
            return self._repository
        response = self.client.get_json(repo_endpoint(self.owner, self.repo))
        if not isinstance(response, dict) or not response.get("full_name"):
            raise FetchError("GitHub repository response did not include full_name")
        self._repository = response
        return response

    def _resolve_commit(self, repository: dict[str, Any]) -> dict[str, Any]:
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

    def _should_process_file(
        self,
        item: dict[str, Any],
        query: ConnectorQuery,
        *,
        commit: dict[str, Any],
    ) -> bool:
        """Apply query/config filters to one GitHub tree or content item."""
        path = normalize_repo_path(str(item.get("path") or ""))
        size = int(item.get("size") or 0)
        extension = file_extension(path)
        mime_type = guess_mime_type(path)

        if query.updated_after:
            updated_at = commit_timestamp(commit)
            if updated_at and updated_at <= query.updated_after:
                return False
        if not path_matches_query(path, query.pattern):
            return False

        allowed_extensions = self._extension_filter(query, "allowed_extensions")
        if allowed_extensions and extension not in allowed_extensions:
            return False
        excluded_extensions = self._extension_filter(query, "excluded_extensions")
        if extension in excluded_extensions:
            return False

        include_paths = self._path_filter(query, "include_paths")
        if include_paths and not any(
            path_in_scope(path, value, recursive=True) for value in include_paths
        ):
            return False
        exclude_paths = self._path_filter(query, "exclude_paths")
        if any(path_in_scope(path, value, recursive=True) for value in exclude_paths):
            return False

        include_globs = self._path_filter(query, "include_globs")
        if include_globs and not path_matches_patterns(path, include_globs):
            return False
        exclude_globs = self._path_filter(query, "exclude_globs")
        if path_matches_patterns(path, exclude_globs):
            return False

        if self.config.max_file_size_bytes is not None:
            if size > self.config.max_file_size_bytes:
                logger.debug("Skipping oversized GitHub file %s", path)
                return False

        if self.config.process_file_callback:
            try:
                should_process, reason = self.config.process_file_callback(
                    path,
                    size,
                    mime_type,
                )
            except Exception:
                if self.config.fail_on_error:
                    raise
                logger.exception("GitHub file callback failed for %s", path)
                return False
            if not should_process:
                logger.debug("Skipping GitHub file %s: %s", path, reason)
                return False
        return True

    def _enforce_size_limit(self, label: str, size: int) -> None:
        """Prevent large blobs from being materialized by direct loads."""
        if self.config.max_file_size_bytes is None or not size:
            return
        if size > self.config.max_file_size_bytes:
            raise DocumentProcessingError(
                f"GitHub file {label!r} size {size} exceeds "
                f"max_file_size_bytes {self.config.max_file_size_bytes}"
            )

    def _extension_filter(self, query: ConnectorQuery, key: str) -> set[str]:
        values = query.filters.get(key)
        if values is None and key == "allowed_extensions":
            values = query.filters.get("extensions")
        if values is None:
            return set(getattr(self.config, key))
        if isinstance(values, str):
            values = [values]
        return {
            str(value).lower().strip()
            if str(value).startswith(".")
            else f".{str(value).lower().strip()}"
            for value in values
        }

    def _path_filter(self, query: ConnectorQuery, key: str) -> list[str]:
        values = query.filters.get(key)
        if values is None:
            return list(getattr(self.config, key))
        if isinstance(values, str):
            return [normalize_repo_path(values)]
        return [normalize_repo_path(str(value)) for value in values]

    @staticmethod
    def _file_paths_from_query(query: ConnectorQuery) -> list[str]:
        values = (
            query.filters.get("file_paths")
            or query.filters.get("paths")
            or query.filters.get("files")
        )
        if values is None:
            return []
        if isinstance(values, str):
            return [normalize_repo_path(values)]
        return [normalize_repo_path(str(value)) for value in values]


class _RequestsGitHubClient:
    """Authenticated, rate-limited GitHub REST client."""

    def __init__(self, config: GitHubRepositoryConfig) -> None:
        self.config = config
        self.session = requests.Session()
        self.session.headers.update(
            {
                "Accept": "application/vnd.github+json",
                "X-GitHub-Api-Version": config.api_version,
            }
        )
        if config.token:
            self.session.headers.update({"Authorization": f"Bearer {config.token}"})
        self._min_interval = 60.0 / config.requests_per_minute
        self._last_request_at = 0.0

    def get_json(
        self,
        endpoint: str,
        *,
        params: dict[str, Any] | None = None,
    ) -> dict[str, Any] | list[dict[str, Any]]:
        """GET a GitHub API endpoint and decode its JSON body."""
        response = self._request("GET", self._api_url(endpoint), params=params)
        try:
            payload = response.json()
        except ValueError as exc:
            raise FetchError(f"GitHub returned non-JSON response for {endpoint}") from exc
        return payload

    def _request(self, method: str, url: str, **kwargs: Any) -> requests.Response:
        """Send one HTTP request with local rate limiting and retry handling."""
        last_error: Exception | None = None
        for attempt in range(self.config.max_retries + 1):
            self._acquire()
            try:
                response = self.session.request(
                    method,
                    url,
                    timeout=self.config.request_timeout_seconds,
                    **kwargs,
                )
            except requests.RequestException as exc:
                last_error = exc
                if attempt == self.config.max_retries:
                    raise FetchError(str(exc)) from exc
                self._sleep(attempt, exc)
                continue

            if response.status_code in (401,):
                raise AuthenticationError(safe_error_detail(response.text))
            if self._rate_limited(response):
                if attempt == self.config.max_retries:
                    raise RateLimitError(safe_error_detail(response.text))
                last_error = RateLimitError(safe_error_detail(response.text))
                self._sleep(attempt, last_error, response.headers)
                continue
            if response.status_code == 403:
                raise AuthenticationError(safe_error_detail(response.text))
            if (
                response.status_code not in _RETRYABLE_STATUS
                or attempt == self.config.max_retries
            ):
                if response.status_code >= 400:
                    raise FetchError(
                        f"GitHub request failed with HTTP "
                        f"{response.status_code}: {safe_error_detail(response.text)}"
                    )
                return response

            last_error = FetchError(f"GitHub request returned HTTP {response.status_code}")
            self._sleep(attempt, last_error, response.headers)

        raise FetchError(str(last_error))

    def _api_url(self, endpoint: str) -> str:
        """Build a GitHub API URL while rejecting cross-origin absolute URLs."""
        if endpoint.startswith(("http://", "https://")):
            try:
                return require_same_origin_url(
                    endpoint,
                    self.config.api_url,
                    label="GitHub API",
                )
            except ValueError as exc:
                raise FetchError(str(exc)) from exc
        return f"{self.config.api_url}/{endpoint.lstrip('/')}"

    @staticmethod
    def _rate_limited(response: requests.Response) -> bool:
        if response.status_code == 429:
            return True
        if response.status_code != 403:
            return False
        # Primary limit: remaining budget exhausted. Secondary/abuse limits
        # instead return 403 with a Retry-After header (and usually non-zero
        # remaining), so both must be treated as throttling rather than auth
        # failure — otherwise a transient throttle aborts the whole sync.
        if response.headers.get("X-RateLimit-Remaining") == "0":
            return True
        if response.headers.get("Retry-After"):
            return True
        body = (response.text or "").lower()
        return "secondary rate limit" in body or "abuse detection" in body

    def _acquire(self) -> None:
        """Throttle requests according to the configured per-minute budget."""
        now = time.monotonic()
        wait = self._min_interval - (now - self._last_request_at)
        if wait > 0:
            time.sleep(wait)
        self._last_request_at = time.monotonic()

    def _sleep(self, attempt: int, error: Exception, headers: Any = None) -> None:
        """Sleep before retrying, honoring provider retry headers when present."""
        fallback_delay = self.config.backoff_factor * (2**attempt)
        delay = retry_delay_seconds(headers, fallback_delay)
        logger.warning(
            "Retrying GitHub request after error, attempt %d/%d: %s",
            attempt + 1,
            self.config.max_retries,
            error,
        )
        if delay > 0:
            time.sleep(delay)
