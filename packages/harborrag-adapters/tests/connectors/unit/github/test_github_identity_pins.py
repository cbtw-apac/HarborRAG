"""GitHub output keeps its document identity and change-detection values."""

from __future__ import annotations

import base64

import pytest
from github_test_helpers import FakeGitHubClient, add_repo_and_commit, config, tree_item

from harborrag_adapters.connectors.github import GitHubConnector
from harborrag_core.chunking import ConnectorType

pytestmark = pytest.mark.unit

PINS: dict[str, tuple[str, str, str, str]] = {
    "github://acme/harbor-rag/README.md": (
        "document:500bfca7333ea958f314a8cc5da85c52c71b590ebfe901653ba82cb9913e30cc",
        "admission-change:66b9634b64414f124a9424d92001e6cd34cb74b9d4bc59381a2c5cc7e0f6cddb",
        "f8f0ddc3793bdc1ed0f10f533d3906102824b0e4e72d45cb72f1a2aa19a3cdba",
        "retrieval-metadata:eaab0183153bd1b2557c8b56d6679921f9a7707f9fc3f0a4110400e146c3f900",
    ),
    "github://acme/harbor-rag/src/app.py": (
        "document:6e504ddda70664ac68e07283cc419e55c70cf5151c0cf5efb83e9436f139bd77",
        "admission-change:cb53ee5cdf314a6131bf2d6c8ccdb1e4eec927a6558401772237faf9f34f0d37",
        "d62a7271bbc03ffa394b9bca2c29c14977779f29461ea9432cc1ce294d58e476",
        "retrieval-metadata:1e070b5b42f4814dfc514613628f5a334c86fdeeb93badde3eeabf740d1e2429",
    ),
}

FILES = {
    "README.md": b"# Harbor\n\nSee [the guide](docs/guide.md) and `src/app.py`.\n",
    "src/app.py": b'"""Berth planner."""\n\n\ndef plan(ships):\n    return sorted(ships)\n',
}


def test_github_files_keep_their_identity_and_hashes(pin_connector) -> None:
    client = FakeGitHubClient()
    for _ in range(3):
        add_repo_and_commit(client)
    client.add(
        "repos/acme/harbor-rag/git/trees/tree-root",
        {
            "sha": "tree-root",
            "truncated": False,
            "tree": [
                tree_item(path, f"sha-{index}", len(body))
                for index, (path, body) in enumerate(FILES.items())
            ],
        },
    )
    for index, body in enumerate(FILES.values()):
        client.add(
            f"repos/acme/harbor-rag/git/blobs/sha-{index}",
            {
                "sha": f"sha-{index}",
                "size": len(body),
                "encoding": "base64",
                "content": base64.b64encode(body).decode("ascii"),
            },
        )
    connector = GitHubConnector(config(allowed_extensions={".md", ".py"}), client=client)

    assert pin_connector(connector, connector_type=ConnectorType("github")) == PINS
