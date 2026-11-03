"""Every connector reads the same file types and document size unless a connection says otherwise."""

from __future__ import annotations

from pathlib import Path

import pytest

from harborrag_adapters.connectors import LocalFileConnector
from harborrag_adapters.connectors.confluence import ConfluenceSpaceConfig
from harborrag_adapters.connectors.github import GitHubRepositoryConfig
from harborrag_adapters.connectors.github import filters as github_filters
from harborrag_adapters.connectors.jira import JiraProjectConfig
from harborrag_adapters.connectors.limits import DEFAULT_MAX_DOCUMENT_BYTES
from harborrag_adapters.connectors.local import LocalFileConfig
from harborrag_adapters.connectors.schemas import ConnectorQuery

pytestmark = [pytest.mark.unit, pytest.mark.graybox]

_ATLASSIAN = {"base_url": "https://acme.atlassian.net", "token": "t", "email": "bot@acme.com"}


def test_the_default_document_size_is_20_mib(tmp_path: Path) -> None:
    sizes = {
        "jira": JiraProjectConfig(**_ATLASSIAN).max_attachment_size_bytes,
        "confluence": ConfluenceSpaceConfig(
            space_key="ENG", **_ATLASSIAN
        ).max_attachment_size_bytes,
        "github": GitHubRepositoryConfig(owner="acme", repo="docs").max_file_size_bytes,
        "local": LocalFileConfig(source_path=tmp_path).max_file_size_bytes,
    }

    assert DEFAULT_MAX_DOCUMENT_BYTES == 20_971_520
    assert sizes == dict.fromkeys(sizes, DEFAULT_MAX_DOCUMENT_BYTES)


@pytest.mark.parametrize(
    "config",
    [
        JiraProjectConfig(**_ATLASSIAN),
        ConfluenceSpaceConfig(space_key="ENG", **_ATLASSIAN),
    ],
)
def test_attachments_keep_document_and_image_types_by_default(config: object) -> None:
    attachments = config.attachment_filter()  # type: ignore[attr-defined]

    assert attachments.rejection(".epub") is None
    assert attachments.rejection(".png") is None
    assert attachments.rejection(".exe") == "extension .exe is not in allowed_extensions"


def test_local_files_keep_document_and_image_types_by_default(tmp_path: Path) -> None:
    for name in ("notes.MD", "book.epub", "setup.exe", "script.py"):
        (tmp_path / name).write_text("x", encoding="utf-8")

    kept = [
        record.metadata["relative_path"]
        for record in LocalFileConnector(LocalFileConfig(source_path=tmp_path)).discover()
    ]
    every = LocalFileConnector(
        LocalFileConfig(source_path=tmp_path, allowed_extensions=set())
    ).discover()

    assert sorted(kept) == ["book.epub", "notes.MD"]
    assert len(list(every)) == 4


def test_github_keeps_document_and_image_types_by_default() -> None:
    config = GitHubRepositoryConfig(owner="acme", repo="docs")
    query = ConnectorQuery()

    assert github_filters._extension_allowed(config, query, ".md")
    assert not github_filters._extension_allowed(config, query, ".py")
