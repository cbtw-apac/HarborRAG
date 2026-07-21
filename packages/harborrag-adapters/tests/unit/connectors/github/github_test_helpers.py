"""Shared fake client and fixture builders for GitHub connector tests."""

from __future__ import annotations

from typing import Any

from harborrag_adapters.connectors import GitHubRepositoryConfig


class FakeGitHubClient:
    def __init__(self) -> None:
        self.responses: dict[str, list[Any]] = {}
        self.calls: list[tuple[str, dict[str, Any] | None]] = []

    def add(self, endpoint: str, *responses: Any) -> None:
        self.responses.setdefault(endpoint, []).extend(responses)

    def get_json(
        self,
        endpoint: str,
        *,
        params: dict[str, Any] | None = None,
    ) -> dict[str, Any] | list[dict[str, Any]]:
        self.calls.append((endpoint, params))
        values = self.responses.get(endpoint)
        if not values:
            raise AssertionError(f"Unexpected GitHub endpoint: {endpoint}")
        return values.pop(0)


def config(**overrides: Any) -> GitHubRepositoryConfig:
    values = {
        "repository_url": "https://github.com/acme/harbor-rag.git",
        "requests_per_minute": 6000,
    }
    values.update(overrides)
    return GitHubRepositoryConfig(**values)


def repo() -> dict[str, Any]:
    return {
        "id": 42,
        "full_name": "acme/harbor-rag",
        "private": False,
        "default_branch": "main",
    }


def commit(ref: str = "commit1", tree_sha: str = "tree-root") -> dict[str, Any]:
    return {
        "sha": ref,
        "html_url": f"https://github.com/acme/harbor-rag/commit/{ref}",
        "commit": {
            "message": "Update docs",
            "author": {
                "name": "Ada",
                "email": "ada@example.com",
                "date": "2024-05-24T20:57:56Z",
            },
            "committer": {
                "name": "Grace",
                "email": "grace@example.com",
                "date": "2024-05-24T21:00:00Z",
            },
            "tree": {"sha": tree_sha},
        },
    }


def tree_item(path: str, sha: str, size: int = 10) -> dict[str, Any]:
    return {
        "path": path,
        "mode": "100644",
        "type": "blob",
        "sha": sha,
        "size": size,
    }


def add_repo_and_commit(client: FakeGitHubClient) -> None:
    client.add("repos/acme/harbor-rag", repo())
    client.add("repos/acme/harbor-rag/commits/main", commit())
