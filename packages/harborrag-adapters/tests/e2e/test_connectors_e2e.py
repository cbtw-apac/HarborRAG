from __future__ import annotations

import base64
from pathlib import Path
from typing import Any

import pytest

from harborrag_adapters.connectors import (
    ConfluenceConnector,
    ConfluenceSpaceConfig,
    GitHubConnector,
    GitHubRepositoryConfig,
    JiraConnector,
    JiraProjectConfig,
    LocalFileConfig,
    LocalFileConnector,
    SharePointConnector,
    SharePointSiteConfig,
)
from harborrag_adapters.connectors.mock import (
    DEFAULT_MOCK_TEXT,
    MockConnector,
    MockLocalTextFileConnector,
)
from harborrag_adapters.connectors.schemas import ConnectorQuery
from harborrag_core.domain.raw_document import RawDocument
from harborrag_core.domain.source import SourceRecord


# ---------------------------------------------------------------------------
# In-memory / on-disk connectors that need no fake client
# ---------------------------------------------------------------------------

def test_mock_connector_discover_load_round_trip():
    connector = MockConnector(count=2)

    records = list(connector.discover())
    assert [record.id for record in records] == [
        "mock://document/0",
        "mock://document/1",
    ]

    document = connector.load(records[0])
    assert isinstance(document, RawDocument)
    assert document.id == "mock://document/0"
    assert document.content == DEFAULT_MOCK_TEXT
    assert document.content_type == "text/markdown"

    # discover -> load convenience stream produces the same documents.
    streamed = list(connector.load_raw_documents())
    assert [doc.id for doc in streamed] == [r.id for r in records]


def test_mock_local_text_file_connector_round_trip(tmp_path: Path):
    (tmp_path / "a.md").write_text("# A\n\nalpha", encoding="utf-8")
    (tmp_path / "sub").mkdir()
    (tmp_path / "sub" / "b.md").write_text("# B\n\nbeta", encoding="utf-8")

    connector = MockLocalTextFileConnector(tmp_path)

    records = list(connector.discover())
    assert [Path(r.metadata["relative_path"]).as_posix() for r in records] == [
        "a.md",
        "sub/b.md",
    ]

    document = connector.load(records[0])
    assert isinstance(document, RawDocument)
    assert document.content == "# A\n\nalpha"
    assert document.content_type == "text/markdown"
    # Stable IDs are file URIs derived from the resolved path.
    assert document.id.startswith("file:")


def test_local_file_connector_end_to_end(tmp_path: Path):
    (tmp_path / "README.md").write_text("# Hello", encoding="utf-8")
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "app.py").write_text("print('hi')", encoding="utf-8")

    connector = LocalFileConnector(
        LocalFileConfig(
            source_path=tmp_path,
            allowed_extensions={".md", ".py"},
            checksum_mode="sha256",
        )
    )

    records = list(connector.discover(ConnectorQuery(pattern="*.*")))
    rel_paths = [record.metadata["relative_path"] for record in records]
    assert rel_paths == ["README.md", "src/app.py"]

    documents = [connector.load(record) for record in records]
    readme = documents[0]
    assert isinstance(readme, RawDocument)
    assert readme.content == b"# Hello"
    assert readme.id == records[0].id  # stable id preserved across load
    assert readme.id == (tmp_path / "README.md").resolve().as_uri()
    assert len(readme.metadata["checksum"]) == 64


# ---------------------------------------------------------------------------
# Fake clients for the network-backed providers (patterns copied from the
# existing per-connector unit tests).
# ---------------------------------------------------------------------------

class FakeConfluenceClient:
    def __init__(self) -> None:
        self.responses: dict[str, list[dict[str, Any]]] = {}
        self.downloads: dict[str, bytes] = {}

    def add(self, endpoint: str, *responses: dict[str, Any]) -> None:
        self.responses.setdefault(endpoint, []).extend(responses)

    def get_json(self, endpoint: str, *, params: dict[str, Any] | None = None):
        values = self.responses.get(endpoint)
        if not values:
            raise AssertionError(f"Unexpected Confluence endpoint: {endpoint}")
        return values.pop(0)

    def download_bytes(self, url: str) -> bytes | None:
        return self.downloads.get(url)


class FakeGitHubClient:
    def __init__(self) -> None:
        self.responses: dict[str, list[Any]] = {}

    def add(self, endpoint: str, *responses: Any) -> None:
        self.responses.setdefault(endpoint, []).extend(responses)

    def get_json(self, endpoint: str, *, params: dict[str, Any] | None = None):
        values = self.responses.get(endpoint)
        if not values:
            raise AssertionError(f"Unexpected GitHub endpoint: {endpoint}")
        return values.pop(0)


class FakeJiraClient:
    def __init__(self) -> None:
        self.get_responses: dict[str, list[dict[str, Any]]] = {}
        self.post_responses: dict[str, list[dict[str, Any]]] = {}
        self.downloads: dict[str, bytes] = {}

    def add_get(self, endpoint: str, *responses: dict[str, Any]) -> None:
        self.get_responses.setdefault(endpoint, []).extend(responses)

    def add_post(self, endpoint: str, *responses: dict[str, Any]) -> None:
        self.post_responses.setdefault(endpoint, []).extend(responses)

    def get_json(self, endpoint: str, *, params: dict[str, Any] | None = None):
        values = self.get_responses.get(endpoint)
        if not values:
            raise AssertionError(f"Unexpected JIRA GET endpoint: {endpoint}")
        return values.pop(0)

    def post_json(self, endpoint: str, *, json: dict[str, Any]):
        values = self.post_responses.get(endpoint)
        if not values:
            raise AssertionError(f"Unexpected JIRA POST endpoint: {endpoint}")
        return values.pop(0)

    def download_bytes(self, url: str) -> bytes | None:
        return self.downloads.get(url)


class FakeGraphClient:
    def __init__(self) -> None:
        self.responses: dict[str, list[dict[str, Any]]] = {}
        self.downloads: dict[str, bytes] = {}

    def add(self, endpoint: str, *responses: dict[str, Any]) -> None:
        self.responses.setdefault(endpoint, []).extend(responses)

    def get_json(self, endpoint: str, *, params: dict[str, Any] | None = None):
        values = self.responses.get(endpoint)
        if not values:
            raise AssertionError(f"Unexpected Microsoft Graph endpoint: {endpoint}")
        return values.pop(0)

    def get_bytes(self, endpoint: str) -> bytes:
        try:
            return self.downloads[endpoint]
        except KeyError as exc:
            raise AssertionError(f"Unexpected Graph bytes: {endpoint}") from exc


# ---------------------------------------------------------------------------
# One happy-path e2e per network-backed connector
# ---------------------------------------------------------------------------

CONFLUENCE_BASE = "https://example.atlassian.net/wiki"


def test_confluence_connector_end_to_end():
    client = FakeConfluenceClient()
    client.add(
        "content/search",
        {
            "results": [
                {
                    "id": "1",
                    "title": "Page One",
                    "type": "page",
                    "space": {"key": "ENG"},
                    "metadata": {"labels": {"results": []}},
                    "version": {"when": "2024-05-24T20:57:56.130Z"},
                }
            ],
            "_links": {},
        },
    )
    client.add(
        "content/1",
        {
            "id": "1",
            "title": "Page One",
            "type": "page",
            "space": {"key": "ENG"},
            "version": {"number": 1, "when": "2024-05-24T20:57:56.130Z"},
            "history": {
                "createdBy": {"displayName": "Alice"},
                "createdDate": "2023-01-01T00:00:00.000Z",
            },
            "metadata": {"labels": {"results": []}},
            "body": {"export_view": {"value": "<p>Hello World</p>"}},
        },
    )
    connector = ConfluenceConnector(
        ConfluenceSpaceConfig(
            space_key="ENG",
            base_url=CONFLUENCE_BASE,
            token="token",
            email="me@example.com",
            requests_per_minute=6000,
        ),
        client=client,
    )

    records = list(connector.discover())
    assert [r.id for r in records] == ["confluence://ENG/1"]

    document = connector.load(records[0])
    assert isinstance(document, RawDocument)
    assert document.id == "confluence://ENG/1"
    assert document.content == "<p>Hello World</p>"


def test_github_connector_end_to_end():
    client = FakeGitHubClient()
    client.add(
        "repos/acme/harbor-rag",
        {"id": 42, "full_name": "acme/harbor-rag", "private": False, "default_branch": "main"},
    )
    client.add(
        "repos/acme/harbor-rag/commits/main",
        {
            "sha": "commit1",
            "html_url": "https://github.com/acme/harbor-rag/commit/commit1",
            "commit": {
                "message": "Update docs",
                "author": {"name": "Ada", "email": "ada@example.com", "date": "2024-05-24T20:57:56Z"},
                "committer": {"name": "Grace", "email": "grace@example.com", "date": "2024-05-24T21:00:00Z"},
                "tree": {"sha": "tree-root"},
            },
        },
    )
    client.add(
        "repos/acme/harbor-rag/git/trees/tree-root",
        {
            "sha": "tree-root",
            "truncated": False,
            "tree": [
                {"path": "README.md", "mode": "100644", "type": "blob", "sha": "sha-readme", "size": 7},
            ],
        },
    )
    client.add(
        "repos/acme/harbor-rag/git/blobs/sha-readme",
        {
            "sha": "sha-readme",
            "size": 7,
            "encoding": "base64",
            "content": base64.b64encode(b"# Hello").decode("ascii"),
        },
    )
    connector = GitHubConnector(
        GitHubRepositoryConfig(
            repository_url="https://github.com/acme/harbor-rag.git",
            requests_per_minute=6000,
        ),
        client=client,
    )

    records = list(connector.discover())
    assert [r.id for r in records] == ["github://acme/harbor-rag/README.md"]
    assert records[0].checksum == "sha-readme"

    document = connector.load(records[0])
    assert isinstance(document, RawDocument)
    assert document.content == b"# Hello"
    assert document.metadata["repository"] == "acme/harbor-rag"


JIRA_BASE = "https://example.atlassian.net"


def _jira_issue(key: str = "ENG-1") -> dict[str, Any]:
    return {
        "id": "10001",
        "key": key,
        "fields": {
            "summary": "Build parser",
            "description": {
                "type": "doc",
                "content": [
                    {"type": "paragraph", "content": [{"type": "text", "text": "ADF body"}]}
                ],
            },
            "issuetype": {"name": "Task"},
            "status": {"name": "In Progress", "statusCategory": {"name": "Doing"}},
            "priority": {"name": "High"},
            "assignee": {"displayName": "Ada"},
            "reporter": {"displayName": "Grace"},
            "creator": {"displayName": "Linus"},
            "labels": [],
            "project": {"key": "ENG", "name": "Engineering"},
            "components": [],
            "fixVersions": [],
            "versions": [],
            "created": "2024-01-01T00:00:00.000+0000",
            "updated": "2024-05-24T20:57:56.130+0000",
            "resolutiondate": None,
            "duedate": None,
            "subtasks": [],
            "issuelinks": [],
            "attachment": [],
        },
    }


def test_jira_connector_end_to_end():
    client = FakeJiraClient()
    client.add_post(
        "search/jql",
        {"issues": [_jira_issue("ENG-1")], "isLast": True},
    )
    client.add_get("issue/ENG-1", _jira_issue("ENG-1"))
    client.add_get("issue/ENG-1/comment", {"startAt": 0, "total": 0, "comments": []})
    connector = JiraConnector(
        JiraProjectConfig(
            base_url=JIRA_BASE,
            token="token",
            email="me@example.com",
            project_keys=["ENG"],
            requests_per_minute=6000,
        ),
        client=client,
    )

    records = list(connector.discover())
    assert [r.id for r in records] == ["jira://ENG/ENG-1"]

    document = connector.load(records[0])
    assert isinstance(document, RawDocument)
    assert document.id == "jira://ENG/ENG-1"
    assert "Build parser" in document.content
    assert "ADF body" in document.content


SHAREPOINT_SITE = "https://contoso.sharepoint.com/sites/Engineering"


def _sp_file_item(item_id: str = "file1", name: str = "Guide.docx") -> dict[str, Any]:
    return {
        "id": item_id,
        "name": name,
        "webUrl": f"{SHAREPOINT_SITE}/Shared%20Documents/{name}",
        "size": 12,
        "file": {
            "mimeType": (
                "application/vnd.openxmlformats-officedocument."
                "wordprocessingml.document"
            ),
            "hashes": {"quickXorHash": f"hash-{item_id}"},
        },
        "parentReference": {"driveId": "drive1", "id": "root", "path": "/drive/root:/Docs"},
        "createdDateTime": "2024-01-01T00:00:00Z",
        "lastModifiedDateTime": "2024-05-24T20:57:56Z",
        "createdBy": {"user": {"displayName": "Ada"}},
        "lastModifiedBy": {"user": {"displayName": "Grace"}},
        "eTag": f"etag-{item_id}",
        "cTag": f"ctag-{item_id}",
    }


def test_sharepoint_connector_end_to_end():
    client = FakeGraphClient()
    client.add("sites/contoso.sharepoint.com:/sites/Engineering", {
        "id": "site1", "name": "Engineering", "displayName": "Engineering", "webUrl": SHAREPOINT_SITE,
    })
    client.add("sites/site1/drive", {
        "id": "drive1", "name": "Documents", "driveType": "documentLibrary",
        "webUrl": f"{SHAREPOINT_SITE}/Documents",
    })
    client.add("drives/drive1/root/children", {"value": [_sp_file_item("file1", "Guide.docx")]})
    client.add("drives/drive1/items/file1", _sp_file_item("file1", "Guide.docx"))
    client.downloads["drives/drive1/items/file1/content"] = b"docx-bytes"
    connector = SharePointConnector(
        SharePointSiteConfig(
            site_url=SHAREPOINT_SITE,
            access_token="token",
            requests_per_minute=6000,
        ),
        client=client,
    )

    records = list(connector.discover())
    assert [r.id for r in records] == ["sharepoint://site1/drive1/file1"]
    assert records[0].checksum == "hash-file1"

    document = connector.load(records[0])
    assert isinstance(document, RawDocument)
    assert document.id == "sharepoint://site1/drive1/file1"
    assert document.content == b"docx-bytes"
    assert document.metadata["item_name"] == "Guide.docx"
