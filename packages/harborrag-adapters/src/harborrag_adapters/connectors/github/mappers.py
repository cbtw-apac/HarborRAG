from __future__ import annotations

from datetime import datetime
from typing import Any

from harborrag_core.domain.source import SourceRecord

from .utils import github_blob_url, github_raw_url, guess_mime_type, normalize_repo_path


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
    web_url: str,
    ref: str,
    commit_sha: str,
    commit: dict[str, Any],
) -> SourceRecord:
    """Convert a GitHub tree/content item into a source record."""
    path = normalize_repo_path(str(item.get("path") or ""))
    file_sha = str(item.get("sha") or "")
    mime_type = guess_mime_type(path)
    updated_at = commit_timestamp(commit)

    return SourceRecord(
        # Commit-independent, path-stable ID so a downstream index keyed on it
        # is not invalidated for every file on every commit (which would force a
        # full-repo re-ingest). The commit/blob SHA lives in metadata/checksum
        # and drives change detection.
        id=f"github://{owner}/{repo}/{path}",
        source_type=mime_type,
        locator=path,
        updated_at=updated_at,
        checksum=file_sha,
        metadata={
            "source_system": "github",
            "owner": owner,
            "repo": repo,
            "repository": f"{owner}/{repo}",
            "ref": ref,
            "commit_sha": commit_sha,
            "tree_sha": tree_sha_from_commit(commit),
            "path": path,
            "sha": file_sha,
            "mode": item.get("mode"),
            "size": int(item.get("size") or 0),
            "mime_type": mime_type,
            "html_url": github_blob_url(
                web_url=web_url,
                owner=owner,
                repo=repo,
                ref=ref,
                path=path,
            ),
            "raw_url": github_raw_url(
                web_url=web_url,
                owner=owner,
                repo=repo,
                ref=ref,
                path=path,
            ),
            "commit_url": commit.get("html_url"),
            "updated_at": updated_at,
        },
    )


def build_document_metadata(
    item: dict[str, Any],
    *,
    owner: str,
    repo: str,
    web_url: str,
    ref: str,
    commit: dict[str, Any],
    repository: dict[str, Any],
) -> dict[str, Any]:
    """Build parsed provenance metadata for a loaded GitHub file."""
    path = normalize_repo_path(str(item.get("path") or ""))
    commit_sha = str(commit.get("sha") or "")
    return {
        "source_system": "github",
        "owner": owner,
        "repo": repo,
        "repository": f"{owner}/{repo}",
        "repository_id": repository.get("id"),
        "repository_full_name": repository.get("full_name"),
        "repository_private": repository.get("private"),
        "default_branch": repository.get("default_branch"),
        "ref": ref,
        "commit_sha": commit_sha,
        "commit_message": commit.get("commit", {}).get("message"),
        "commit_author": _commit_identity(commit, "author"),
        "commit_committer": _commit_identity(commit, "committer"),
        "commit_url": commit.get("html_url"),
        "tree_sha": tree_sha_from_commit(commit),
        "path": path,
        "sha": item.get("sha"),
        "mode": item.get("mode"),
        "size": int(item.get("size") or 0),
        "mime_type": guess_mime_type(path),
        "html_url": github_blob_url(
            web_url=web_url,
            owner=owner,
            repo=repo,
            ref=ref,
            path=path,
        ),
        "raw_url": github_raw_url(
            web_url=web_url,
            owner=owner,
            repo=repo,
            ref=ref,
            path=path,
        ),
        "updated_at": commit_timestamp(commit),
    }


def _commit_identity(commit: dict[str, Any], key: str) -> dict[str, Any] | None:
    value = commit.get("commit", {}).get(key)
    if not isinstance(value, dict):
        return None
    return {
        "name": value.get("name"),
        "email": value.get("email"),
        "date": parse_timestamp(value.get("date")),
    }
