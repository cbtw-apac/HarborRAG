"""Pure path, URL, MIME, and payload helpers for GitHub."""

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
    """Parse HTTPS, SSH, or git-style repository URLs into owner/repo."""
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
    """Return the REST endpoint for resolving a ref to a commit."""
    encoded_ref = quote(ref, safe="/")
    return f"repos/{owner}/{repo}/commits/{encoded_ref}"


def tree_endpoint(owner: str, repo: str, tree_sha: str) -> str:
    """Return the REST endpoint for a Git tree object."""
    return f"repos/{owner}/{repo}/git/trees/{tree_sha}"


def blob_endpoint(owner: str, repo: str, file_sha: str) -> str:
    """Return the REST endpoint for a Git blob object."""
    return f"repos/{owner}/{repo}/git/blobs/{file_sha}"


def content_endpoint(owner: str, repo: str, path: str) -> str:
    """Return the contents API endpoint for one repository path."""
    normalized = normalize_repo_path(path)
    encoded_path = quote(normalized, safe="/")
    return f"repos/{owner}/{repo}/contents/{encoded_path}"


def normalize_repo_path(path: str | None) -> str:
    """Normalize repository paths to GitHub's POSIX path style."""
    if not path:
        return ""
    normalized = str(path).replace("\\", "/").strip("/")
    return str(PurePosixPath(normalized)) if normalized else ""


def file_extension(path: str) -> str:
    """Return a lowercased file extension from a repository path."""
    name = normalize_repo_path(path)
    if "." not in PurePosixPath(name).name:
        return ""
    return PurePosixPath(name).suffix.lower()


def guess_mime_type(path: str) -> str:
    """Infer a MIME type from a repository path."""
    mime_type, _ = mimetypes.guess_type(path)
    return mime_type or "application/octet-stream"


def is_blob(item: dict[str, Any]) -> bool:
    """Return whether a tree/content API item represents a file blob."""
    return item.get("type") == "blob" or item.get("type") == "file"


def is_tree(item: dict[str, Any]) -> bool:
    """Return whether a tree/content API item represents a directory."""
    return item.get("type") == "tree" or item.get("type") == "dir"


def path_in_scope(path: str, root_path: str | None, *, recursive: bool) -> bool:
    """Return whether a repository path is inside the requested root."""
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


def path_matches_patterns(path: str, patterns: list[str]) -> bool:
    """Return whether a repository path matches any include/exclude glob."""
    normalized = normalize_repo_path(path)
    return any(
        fnmatch(normalized, normalize_repo_path(pattern)) for pattern in patterns
    )


def path_matches_query(path: str, pattern: str | None) -> bool:
    """Return whether a repository path matches a query pattern or substring."""
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
    """Build a browser URL for a repository file at a resolved ref."""
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
    """Build a raw-content URL for metadata provenance."""
    encoded_path = quote(normalize_repo_path(path), safe="/")
    if web_url.rstrip("/") == DEFAULT_GITHUB_WEB_URL:
        return f"https://raw.githubusercontent.com/{owner}/{repo}/{ref}/{encoded_path}"
    return f"{web_url.rstrip('/')}/{owner}/{repo}/raw/{ref}/{encoded_path}"


def _parse_ssh_url(url: str) -> tuple[str, str]:
    _, _, path = url.partition(":")
    parts = [part for part in path.strip("/").split("/") if part]
    if len(parts) < 2:
        raise ValueError("repository_url must include owner and repository")
    return parts[0], _strip_git_suffix(parts[1])


def _strip_git_suffix(value: str) -> str:
    return value[:-4] if value.endswith(".git") else value
