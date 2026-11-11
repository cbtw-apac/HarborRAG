"""Pure path, URL, MIME and payload helpers for the GitHub connector.

Everything that turns a repository path, ref or git object id into a REST
endpoint or a browser URL lives here, with no I/O. The HTTP client resolves
``..`` segments before it sends a request, so no value spliced into an
endpoint may climb out of the configured repository: repository paths and
refs with a ``..`` segment, and git object ids that are not one URL path
segment, raise ValueError.

Entry points: normalize_repo_path, content_endpoint, path_in_scope.
"""

from __future__ import annotations

import mimetypes
import re
from collections.abc import Sequence
from fnmatch import fnmatch
from pathlib import PurePosixPath
from typing import Any
from urllib.parse import quote, unquote, urlparse

DEFAULT_GITHUB_API_URL = "https://api.github.com"
DEFAULT_GITHUB_WEB_URL = "https://github.com"
DEFAULT_GITHUB_API_VERSION = "2022-11-28"
GITHUB_BLOB_LIMIT_BYTES = 100 * 1024 * 1024

_PARENT_SEGMENT = ".."
_GIT_OBJECT_ID = re.compile(r"[A-Za-z0-9_-]+")
_GIT_OBJECT_ID_ERROR = (
    "GitHub git object ids must be one URL path segment of letters, digits, '-' or '_'"
)


def parse_github_repository_url(url: str) -> tuple[str, str]:
    """Parse an HTTPS, SSH or git-style repository URL into owner and repository.

    Args:
        url: Repository URL such as ``https://github.com/acme/harbor-rag.git``
            or ``git@github.com:acme/harbor-rag.git``.

    Returns:
        The owner and the repository name without its ``.git`` suffix.

    Raises:
        ValueError: The URL is not an absolute http, https or ssh URL, or its
            path lacks an owner or repository segment.
    """
    if url.startswith("git@"):
        return _parse_ssh_url(url)

    parsed = urlparse(url)
    if parsed.scheme not in {"http", "https", "ssh"} or not parsed.netloc:
        raise ValueError("repository_url must be an absolute GitHub repository URL")

    parts = [part for part in unquote(parsed.path).strip("/").split("/") if part]
    if len(parts) < 2:
        raise ValueError("repository_url must include owner and repository")
    return parts[0], _strip_git_suffix(parts[1])


def repo_endpoint(owner: str, repo: str) -> str:
    """Return the REST endpoint for repository metadata."""
    return f"repos/{owner}/{repo}"


def commit_endpoint(owner: str, repo: str, ref: str) -> str:
    """Return the REST endpoint that resolves a ref to a commit.

    The ref is percent-encoded with ``/`` kept, so branch names such as
    ``feature/x`` stay intact.

    Args:
        owner: Repository owner from the connector config.
        repo: Repository name from the connector config.
        ref: Branch, tag or commit SHA; valid git ref names never contain ``..``.

    Returns:
        The endpoint path relative to the API URL.

    Raises:
        ValueError: The ref has a ``..`` segment.
    """
    if _has_parent_segment(ref):
        raise ValueError("GitHub refs must not contain '..' segments")
    encoded_ref = quote(ref, safe="/")
    return f"repos/{owner}/{repo}/commits/{encoded_ref}"


def tree_endpoint(owner: str, repo: str, tree_sha: str) -> str:
    """Return the REST endpoint for a Git tree object.

    Args:
        owner: Repository owner from the connector config.
        repo: Repository name from the connector config.
        tree_sha: Tree object id from a commit or a parent tree.

    Returns:
        The endpoint path relative to the API URL.

    Raises:
        ValueError: ``tree_sha`` is not one URL path segment of letters,
            digits, ``-`` or ``_``.
    """
    if not _is_git_object_id(tree_sha):
        raise ValueError(_GIT_OBJECT_ID_ERROR)
    return f"repos/{owner}/{repo}/git/trees/{tree_sha}"


def blob_endpoint(owner: str, repo: str, file_sha: str) -> str:
    """Return the REST endpoint for a Git blob object.

    Args:
        owner: Repository owner from the connector config.
        repo: Repository name from the connector config.
        file_sha: Blob object id from a tree item, a contents response or a
            source record.

    Returns:
        The endpoint path relative to the API URL.

    Raises:
        ValueError: ``file_sha`` is not one URL path segment of letters,
            digits, ``-`` or ``_``.
    """
    if not _is_git_object_id(file_sha):
        raise ValueError(_GIT_OBJECT_ID_ERROR)
    return f"repos/{owner}/{repo}/git/blobs/{file_sha}"


def content_endpoint(owner: str, repo: str, path: str) -> str:
    """Return the contents API endpoint for one repository path.

    The path is normalized by normalize_repo_path, whose ValueError for a
    ``..`` segment passes through, then percent-encoded with ``/`` kept.

    Args:
        owner: Repository owner from the connector config.
        repo: Repository name from the connector config.
        path: Repository-relative file path.

    Returns:
        The endpoint path relative to the API URL.
    """
    normalized = normalize_repo_path(path)
    encoded_path = quote(normalized, safe="/")
    return f"repos/{owner}/{repo}/contents/{encoded_path}"


def normalize_repo_path(path: str | None) -> str:
    """Normalize a repository path to GitHub's POSIX style.

    Backslashes become ``/``, leading and trailing slashes are dropped, and
    ``.`` segments and repeated slashes collapse. ``None``, ``""``, ``"/"`` and
    ``"."`` all mean the repository root.

    Args:
        path: Repository-relative path from config, a query or a GitHub payload.

    Returns:
        The normalized path, or ``""`` for the repository root.

    Raises:
        ValueError: The path has a ``..`` segment.
    """
    if not path:
        return ""
    normalized = str(path).replace("\\", "/").strip("/")
    if not normalized:
        return ""
    posix_path = str(PurePosixPath(normalized))
    if _has_parent_segment(posix_path):
        raise ValueError("GitHub repository paths must not contain '..' segments")
    return "" if posix_path == "." else posix_path


def file_extension(path: str) -> str:
    """Return the lowercased extension of a repository path, or ``""`` without one.

    A ``..`` segment in ``path`` raises ValueError from normalize_repo_path.
    """
    name = normalize_repo_path(path)
    if "." not in PurePosixPath(name).name:
        return ""
    return PurePosixPath(name).suffix.lower()


def guess_mime_type(path: str) -> str:
    """Infer a MIME type from a repository path, defaulting to ``application/octet-stream``."""
    mime_type, _ = mimetypes.guess_type(path)
    return mime_type or "application/octet-stream"


def is_blob(item: dict[str, Any]) -> bool:
    """Return whether a tree or contents API item is a file."""
    return item.get("type") == "blob" or item.get("type") == "file"


def is_tree(item: dict[str, Any]) -> bool:
    """Return whether a tree or contents API item is a directory."""
    return item.get("type") == "tree" or item.get("type") == "dir"


def path_in_scope(path: str, root_path: str | None, *, recursive: bool) -> bool:
    """Return whether a repository path is the root itself or inside it.

    Both values are normalized by normalize_repo_path, whose ValueError for a
    ``..`` segment passes through.

    Args:
        path: Repository-relative path to test.
        root_path: Root to test against; empty or ``None`` means the
            repository root.
        recursive: Accept paths at any depth below the root, not only its
            direct children.

    Returns:
        True when ``path`` equals the root or lies inside it at an accepted depth.
    """
    normalized = normalize_repo_path(path)
    root = normalize_repo_path(root_path)
    if not root:
        return recursive or "/" not in normalized
    if normalized == root:
        return True
    if not normalized.startswith(f"{root}/"):
        return False
    if recursive:
        return True
    relative = normalized[len(root) + 1 :]
    return "/" not in relative


def path_matches_patterns(path: str, patterns: Sequence[str]) -> bool:
    """Return whether a repository path matches any include or exclude glob.

    The path and every pattern are normalized by normalize_repo_path, whose
    ValueError for a ``..`` segment passes through.

    Args:
        path: Repository-relative path to test.
        patterns: fnmatch globs; an empty sequence matches nothing.

    Returns:
        True when at least one pattern matches the whole path.
    """
    normalized = normalize_repo_path(path)
    return any(fnmatch(normalized, normalize_repo_path(pattern)) for pattern in patterns)


def path_matches_query(path: str, pattern: str | None) -> bool:
    """Return whether a repository path matches a query glob or contains a query substring.

    The comparison ignores case. The path and the pattern are normalized by
    normalize_repo_path, whose ValueError for a ``..`` segment passes through.

    Args:
        path: Repository-relative path to test.
        pattern: Glob when it contains ``*``, ``?``, ``[`` or ``]``, otherwise
            a substring; empty or ``None`` matches every path.

    Returns:
        True when the path matches the glob or contains the substring.
    """
    if not pattern:
        return True
    normalized_path = normalize_repo_path(path).lower()
    normalized_pattern = normalize_repo_path(pattern).lower()
    if any(char in normalized_pattern for char in "*?[]"):
        return fnmatch(normalized_path, normalized_pattern)
    return normalized_pattern in normalized_path


def github_blob_url(
    *,
    web_url: str,
    owner: str,
    repo: str,
    ref: str,
    path: str,
) -> str:
    """Build the browser URL of a repository file at a resolved ref.

    The URL is provenance metadata and is never fetched. The path is
    normalized by normalize_repo_path, whose ValueError for a ``..`` segment
    passes through.

    Args:
        web_url: GitHub web URL from the connector config.
        owner: Repository owner from the connector config.
        repo: Repository name from the connector config.
        ref: Ref the run resolved, spliced as given.
        path: Repository-relative file path.

    Returns:
        The ``blob`` page URL of the file.
    """
    encoded_path = quote(normalize_repo_path(path), safe="/")
    return f"{web_url.rstrip('/')}/{owner}/{repo}/blob/{ref}/{encoded_path}"


def github_raw_url(
    *,
    web_url: str,
    owner: str,
    repo: str,
    ref: str,
    path: str,
) -> str:
    """Build the raw-content URL of a repository file for provenance metadata.

    The URL is never fetched. The path is normalized by normalize_repo_path,
    whose ValueError for a ``..`` segment passes through.

    Args:
        web_url: GitHub web URL from the connector config; the public
            ``https://github.com`` maps to ``raw.githubusercontent.com``.
        owner: Repository owner from the connector config.
        repo: Repository name from the connector config.
        ref: Ref the run resolved, spliced as given.
        path: Repository-relative file path.

    Returns:
        The raw-content URL of the file.
    """
    encoded_path = quote(normalize_repo_path(path), safe="/")
    if web_url.rstrip("/") == DEFAULT_GITHUB_WEB_URL:
        return f"https://raw.githubusercontent.com/{owner}/{repo}/{ref}/{encoded_path}"
    return f"{web_url.rstrip('/')}/{owner}/{repo}/raw/{ref}/{encoded_path}"


def _has_parent_segment(value: str) -> bool:
    """Return whether a ``/``-separated value has a ``..`` segment."""
    return _PARENT_SEGMENT in value.split("/")


def _is_git_object_id(value: str) -> bool:
    """Return whether a value is safe to splice as one URL path segment."""
    return _GIT_OBJECT_ID.fullmatch(value) is not None


def _parse_ssh_url(url: str) -> tuple[str, str]:
    _, _, path = url.partition(":")
    parts = [part for part in path.strip("/").split("/") if part]
    if len(parts) < 2:
        raise ValueError("repository_url must include owner and repository")
    return parts[0], _strip_git_suffix(parts[1])


def _strip_git_suffix(value: str) -> str:
    return value[:-4] if value.endswith(".git") else value
