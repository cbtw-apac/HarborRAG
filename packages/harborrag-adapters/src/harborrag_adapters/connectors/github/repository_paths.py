"""Path, URL and MIME helpers for GitHub.

Handles:
- Repository URL parsing
- REST endpoint builders
- Path normalization, scoping and glob matching
- Blob URLs and MIME guessing
"""

from __future__ import annotations

import mimetypes
from fnmatch import fnmatch
from pathlib import PurePosixPath
from typing import Any
from urllib.parse import quote, unquote, urlparse

DEFAULT_GITHUB_API_URL = "https://api.github.com"
DEFAULT_GITHUB_WEB_URL = "https://github.com"
DEFAULT_GITHUB_API_VERSION = "2022-11-28"
GITHUB_BLOB_LIMIT_BYTES = 100 * 1024 * 1024


def parse_github_repository_url(url: str) -> tuple[str, str]:
    """Return ``(owner, repo)`` from an HTTPS, SSH or ``git@`` repository URL.

    Raises:
        ValueError: If the URL is not absolute or lacks an owner and repository.
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
    """Return the repository metadata endpoint."""
    return f"repos/{owner}/{repo}"


def commit_endpoint(owner: str, repo: str, ref: str) -> str:
    """Return the commit endpoint for a ref."""
    encoded_ref = quote(ref, safe="/")
    return f"repos/{owner}/{repo}/commits/{encoded_ref}"


def tree_endpoint(owner: str, repo: str, tree_sha: str) -> str:
    """Return the Git tree endpoint."""
    return f"repos/{owner}/{repo}/git/trees/{tree_sha}"


def blob_endpoint(owner: str, repo: str, file_sha: str) -> str:
    """Return the Git blob endpoint."""
    return f"repos/{owner}/{repo}/git/blobs/{file_sha}"


def content_endpoint(owner: str, repo: str, path: str) -> str:
    """Return the contents API endpoint for a path. Raises ``ValueError`` on ``..`` segments."""
    normalized = normalize_repo_path(path)
    encoded_path = quote(normalized, safe="/")
    return f"repos/{owner}/{repo}/contents/{encoded_path}"


def normalize_repo_path(path: str | None) -> str:
    """Normalize a caller-supplied path to a POSIX repository path.

    ``..`` segments, literal or percent-encoded, are rejected because the path is
    interpolated into REST endpoints. Paths reported by GitHub use ``normalize_remote_path``.

    Raises:
        ValueError: If the path contains a ``..`` segment.
    """
    if path and has_parent_segment(str(path)):
        raise ValueError(f"repository path must not contain '..' segments: {path!r}")
    return normalize_remote_path(path)


def normalize_remote_path(path: str | None) -> str:
    """Normalize a path reported by GitHub to POSIX style, accepting any segment."""
    if not path:
        return ""
    normalized = str(path).replace("\\", "/").strip("/")
    return str(PurePosixPath(normalized)) if normalized else ""


def has_parent_segment(path: str) -> bool:
    """Return whether a path has a ``..`` segment, checking the raw and 3 percent-decoded forms."""
    candidate = path
    for _ in range(4):
        if ".." in candidate.replace("\\", "/").split("/"):
            return True
        decoded = unquote(candidate)
        if decoded == candidate:
            return False
        candidate = decoded
    return False


def file_extension(path: str) -> str:
    """Return the lowercased extension of a path, or an empty string."""
    name = normalize_remote_path(path)
    if "." not in PurePosixPath(name).name:
        return ""
    return PurePosixPath(name).suffix.lower()


def guess_mime_type(path: str) -> str:
    """Return the guessed MIME type of a path, defaulting to ``application/octet-stream``."""
    mime_type, _ = mimetypes.guess_type(path)
    return mime_type or "application/octet-stream"


def is_blob(item: dict[str, Any]) -> bool:
    """Return whether an API item is a file."""
    return item.get("type") == "blob" or item.get("type") == "file"


def is_tree(item: dict[str, Any]) -> bool:
    """Return whether an API item is a directory."""
    return item.get("type") == "tree" or item.get("type") == "dir"


def path_in_scope(path: str, root_path: str | None, *, recursive: bool) -> bool:
    """Return whether a path is at or under ``root_path``.

    Args:
        path: Repository path.
        root_path: Root directory, or empty for the repository root.
        recursive: Include nested descendants; otherwise only direct children.
    """
    normalized = normalize_remote_path(path)
    root = normalize_remote_path(root_path)
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


def scoped_root_path(configured: str | None, requested: str | None) -> str:
    """Return the traversal root; a query path may narrow the config root, never widen it.

    Args:
        configured: ``root_path`` from the config.
        requested: Path from the query.

    Raises:
        ValueError: If ``requested`` is outside ``configured``.
    """
    root = normalize_repo_path(configured)
    if not requested:
        return root
    candidate = normalize_repo_path(requested)
    if not path_in_scope(candidate, root, recursive=True):
        raise ValueError(f"query path {requested!r} is outside configured root_path {configured!r}")
    return candidate


def path_matches_patterns(path: str, patterns: list[str]) -> bool:
    """Return whether a path matches any of the globs."""
    # TODO: support ``**`` and stop ``*`` from crossing ``/`` (``fnmatch`` semantics).
    normalized = normalize_remote_path(path)
    return any(fnmatch(normalized, normalize_remote_path(pattern)) for pattern in patterns)


def path_matches_query(path: str, pattern: str | None) -> bool:
    """Return whether a path matches a query glob, or contains the pattern as a substring."""
    if not pattern:
        return True
    normalized_path = normalize_remote_path(path).lower()
    normalized_pattern = normalize_remote_path(pattern).lower()
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
    """Return the browser URL of a file at a ref."""
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
    """Return the raw-content URL of a file at a ref."""
    encoded_path = quote(normalize_repo_path(path), safe="/")
    if web_url.rstrip("/") == DEFAULT_GITHUB_WEB_URL:
        return f"https://raw.githubusercontent.com/{owner}/{repo}/{ref}/{encoded_path}"
    return f"{web_url.rstrip('/')}/{owner}/{repo}/raw/{ref}/{encoded_path}"


def _parse_ssh_url(url: str) -> tuple[str, str]:
    """Return ``(owner, repo)`` from a ``git@host:owner/repo`` URL."""
    _, _, path = url.partition(":")
    parts = [part for part in path.strip("/").split("/") if part]
    if len(parts) < 2:
        raise ValueError("repository_url must include owner and repository")
    return parts[0], _strip_git_suffix(parts[1])


def _strip_git_suffix(value: str) -> str:
    return value[:-4] if value.endswith(".git") else value
