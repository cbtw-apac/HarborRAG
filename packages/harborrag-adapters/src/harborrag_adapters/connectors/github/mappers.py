"""Mappings from GitHub API payloads to Harbor domain objects."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from harborrag_core.domain.source import SourceRecord

from .repository_paths import guess_mime_type, normalize_repo_path
from .schemas import GitHubCommitIdentity, GitHubMetadata


def parse_timestamp(value: str | None) -> datetime | None:
    """Parse GitHub ISO timestamp strings into datetimes."""
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None


def file_path_from_record(record: SourceRecord) -> str:
    """Recover and normalize the repository path from a source record."""
    path = record.metadata.get("path") or record.locator
    if not path:
        raise ValueError(f"SourceRecord {record.id!r} does not contain path")
    return normalize_repo_path(str(path))


def file_sha_from_record(record: SourceRecord) -> str | None:
    """Recover the blob SHA from metadata or the source checksum field."""
    sha = record.metadata.get("sha") or record.checksum
    return str(sha) if sha else None


def commit_timestamp(commit: dict[str, Any]) -> datetime | None:
    """Return the best timestamp for a resolved commit."""
    return parse_timestamp(
        commit.get("commit", {}).get("committer", {}).get("date")
        or commit.get("commit", {}).get("author", {}).get("date")
    )


def tree_sha_from_commit(commit: dict[str, Any]) -> str:
    """Return the root tree SHA from a commit response."""
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
    """Convert a GitHub tree/content item into a source record."""
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
    """Build parsed provenance metadata for a loaded GitHub file."""
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
    """Return a normalized GitHub item path or reject malformed items."""

    path = normalize_repo_path(str(item.get("path") or ""))
    if not path:
        raise ValueError("GitHub item path must not be empty")
    return path


def _commit_identity(
    commit: dict[str, Any],
    key: str,
) -> GitHubCommitIdentity | None:
    value = commit.get("commit", {}).get(key)
    if not isinstance(value, dict):
        return None
    return GitHubCommitIdentity(
        name=value.get("name"),
        email=value.get("email"),
        date=parse_timestamp(value.get("date")),
    )
