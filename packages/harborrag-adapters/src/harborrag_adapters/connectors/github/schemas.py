"""Structured metadata emitted by the GitHub connector."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import ClassVar

from harborrag_adapters.connectors.schemas import ConnectorMetadata


@dataclass(slots=True)
class GitHubCommitIdentity:
    """Author or committer identity from a resolved GitHub commit."""

    name: str | None
    email: str | None
    date: datetime | None


@dataclass(slots=True, kw_only=True)
class GitHubMetadata(ConnectorMetadata):
    """Structured metadata for one loaded GitHub repository file."""

    source_system: ClassVar[str] = "github"

    owner: str
    repo: str
    repository_id: int | None
    repository_private: bool | None
    default_branch: str | None
    ref: str
    commit_sha: str
    commit_message: str | None
    commit_author: GitHubCommitIdentity | None
    commit_committer: GitHubCommitIdentity | None
    tree_sha: str
    path: str
    sha: str | None
    mode: str | None
    size: int
