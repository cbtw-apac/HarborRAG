"""Structured metadata emitted by the GitHub connector."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any, ClassVar

from harborrag_adapters.connectors.schemas import ConnectorMetadata


@dataclass(slots=True)
class GitHubCommitIdentity:
    """Author or committer identity from a resolved GitHub commit."""

    name: Any
    email: Any
    date: datetime | None


@dataclass(slots=True, kw_only=True)
class GitHubMetadata(ConnectorMetadata):
    """Structured metadata for one loaded GitHub repository file."""

    source_system: ClassVar[str] = "github"

    owner: str
    repo: str
    repository_id: Any
    repository_private: Any
    default_branch: Any
    ref: str
    commit_sha: str
    commit_message: Any
    commit_author: GitHubCommitIdentity | None
    commit_committer: GitHubCommitIdentity | None
    tree_sha: str
    path: str
    sha: Any
    mode: Any
    size: int
