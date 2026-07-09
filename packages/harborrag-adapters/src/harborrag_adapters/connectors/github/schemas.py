from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import datetime
from typing import Any


@dataclass(slots=True)
class GitHubCommitIdentity:
    """Author or committer identity from a resolved GitHub commit."""

    name: Any
    email: Any
    date: datetime | None


@dataclass(slots=True)
class GitHubMetadata:
    """Structured metadata for one loaded GitHub repository file."""

    source_system: str
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

    def to_dict(self) -> dict[str, Any]:
        """Serialize metadata for ``RawDocument.metadata``."""
        return asdict(self)
