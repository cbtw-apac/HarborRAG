"""Mappings from GitHub API payloads to source records and metadata.

Handles:
- Source records and document metadata
- Commit timestamps, tree SHAs and identities
- Path and SHA recovery from records
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from harborrag_core.domain.source import SourceRecord

from .repository_paths import (
    guess_mime_type,
    normalize_remote_path,
    normalize_repo_path,
)
from .schemas import GitHubCommitIdentity, GitHubMetadata


def parse_timestamp(value: str | None) -> datetime | None:
    """Parse a GitHub ISO timestamp. Returns ``None`` if empty or invalid."""
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None


def file_path_from_record(record: SourceRecord) -> str:
    """Return the normalized repository path from a record's metadata or locator.

    Raises:
        ValueError: If the record has no path or the path contains a ``..`` segment.
    """
    path = record.metadata.get("path") or record.locator
    if not path:
        raise ValueError(f"SourceRecord {record.id!r} does not contain path")
    return normalize_repo_path(str(path))


def file_sha_from_record(record: SourceRecord) -> str | None:
    """Return the blob SHA from record metadata or checksum, or ``None``."""
    sha = record.metadata.get("sha") or record.checksum
    return str(sha) if sha else None


def commit_timestamp(commit: dict[str, Any]) -> datetime | None:
    """Return the commit's committer date, else its author date, else ``None``."""
    return parse_timestamp(
        commit.get("commit", {}).get("committer", {}).get("date")
        or commit.get("commit", {}).get("author", {}).get("date")
    )


def tree_sha_from_commit(commit: dict[str, Any]) -> str:
    """Return the root tree SHA from a commit response, or an empty string."""
    return str(commit.get("commit", {}).get("tree", {}).get("sha") or "")


def build_source_record(
    item: dict[str, Any],
    *,
    owner: str,
    repo: str,
    ref: str,
    commit_sha: str,
    commit: dict[str, Any],
) -> SourceRecord:
    """Build the source record for a tree or content item.

    Args:
        item: Item with ``path``, ``sha``, ``mode`` and ``size``.
        owner: Repository owner.
        repo: Repository name.
        ref: Ref the commit was resolved from.
        commit_sha: SHA of the anchoring commit.
        commit: Commit payload used for timestamps and the tree SHA.

    Raises:
        ValueError: If the item path is empty.
    """
    path = _required_repo_path(item)
    file_sha = str(item.get("sha") or "")
    mime_type = guess_mime_type(path)
    updated_at = commit_timestamp(commit)

    return SourceRecord(
        id=f"github://{owner}/{repo}/{path}",
        source_type=mime_type,
        locator=path,
        updated_at=updated_at,
        checksum=file_sha,
        metadata={
            "source_system": "github",
            "owner": owner,
            "repo": repo,
            "ref": ref,
            "commit_sha": commit_sha,
            "tree_sha": tree_sha_from_commit(commit),
            "path": path,
            "sha": file_sha,
            "mode": item.get("mode"),
            "size": int(item.get("size") or 0),
        },
    )


def build_document_metadata(
    item: dict[str, Any],
    *,
    owner: str,
    repo: str,
    ref: str,
    commit: dict[str, Any],
    repository: dict[str, Any],
) -> GitHubMetadata:
    """Build the metadata for a loaded file from its item, commit and repository payloads.

    Raises:
        ValueError: If the item path is empty.
    """
    path = _required_repo_path(item)
    commit_sha = str(commit.get("sha") or "")
    return GitHubMetadata(
        record_id=path,
        title=path.rsplit("/", 1)[-1],
        checksum=str(item.get("sha") or "") or None,
        updated_at=commit_timestamp(commit),
        owner=owner,
        repo=repo,
        repository_id=repository.get("id"),
        repository_private=repository.get("private"),
        default_branch=repository.get("default_branch"),
        ref=ref,
        commit_sha=commit_sha,
        commit_message=commit.get("commit", {}).get("message"),
        commit_author=_commit_identity(commit, "author"),
        commit_committer=_commit_identity(commit, "committer"),
        tree_sha=tree_sha_from_commit(commit),
        path=path,
        sha=item.get("sha"),
        mode=item.get("mode"),
        size=int(item.get("size") or 0),
    )


def _required_repo_path(item: dict[str, Any]) -> str:
    """Return the item's normalized path. Raises ``ValueError`` if it is empty."""

    path = normalize_remote_path(str(item.get("path") or ""))
    if not path:
        raise ValueError("GitHub item path must not be empty")
    return path


def _commit_identity(
    commit: dict[str, Any],
    key: str,
) -> GitHubCommitIdentity | None:
    """Return the commit's author or committer identity, or ``None`` if absent."""
    value = commit.get("commit", {}).get(key)
    if not isinstance(value, dict):
        return None
    return GitHubCommitIdentity(
        name=value.get("name"),
        email=value.get("email"),
        date=parse_timestamp(value.get("date")),
    )
