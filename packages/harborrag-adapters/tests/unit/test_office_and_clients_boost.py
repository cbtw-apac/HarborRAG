"""Targeted tests for the legacy .xls path and the real connector HTTP clients.

These exercise branches the fake-client tests can't reach: the office xls
reader, and each ``_Requests*Client``'s URL building / JSON decoding / origin
checks (driven through an injected fake ``requests.Session``).
"""
from __future__ import annotations

import io

import pytest

from harbor_test_builders import FakeResponse, FakeSession
from harborrag_core.domain.parser import ParseInput


# --------------------------------------------------------------------------- #
# office.py — legacy .xls via a real xlwt workbook + import-error branch.
# --------------------------------------------------------------------------- #
def _xls_bytes() -> bytes:
    import xlwt

    book = xlwt.Workbook()
    sheet = book.add_sheet("Data")
    for r, row in enumerate([["name", "score"], ["Ada", 42], ["Bob", 3.5]]):
        for c, value in enumerate(row):
            sheet.write(r, c, value)
    buffer = io.BytesIO()
    book.save(buffer)
    return buffer.getvalue()


def test_excel_parser_reads_legacy_xls() -> None:
    from harborrag_adapters.parsers.office import ExcelParser

    doc = ExcelParser().parse(ParseInput(content=_xls_bytes(), filename="legacy.xls"))
    assert "Data" in doc.content
    assert "Ada" in doc.content and "42" in doc.content
    assert doc.metadata["sheets"] == ["Data"]


def test_excel_parser_missing_dependency_raises_parse_error(monkeypatch) -> None:
    import builtins

    from harborrag_adapters.parsers.exceptions import ParseError
    from harborrag_adapters.parsers.office import ExcelParser

    real_import = builtins.__import__

    def fake_import(name, *args, **kwargs):
        if name == "openpyxl":
            raise ImportError("no openpyxl")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", fake_import)
    with pytest.raises(ParseError, match="openpyxl"):
        ExcelParser().parse(ParseInput(content=b"PK\x03\x04", filename="x.xlsx"))


# --------------------------------------------------------------------------- #
# Real connector HTTP clients: URL building, JSON decode, origin checks.
# --------------------------------------------------------------------------- #
@pytest.fixture(autouse=True)
def _no_sleep(monkeypatch):
    import time

    monkeypatch.setattr(time, "sleep", lambda *_a, **_k: None)


def _confluence_client():
    from harborrag_adapters.connectors.confluence.config import ConfluenceSpaceConfig
    from harborrag_adapters.connectors.confluence.connector import (
        _RequestsConfluenceClient,
    )

    cfg = ConfluenceSpaceConfig(
        space_key="ENG",
        base_url="https://ex.atlassian.net/wiki",
        token="t",
        email="a@b.c",
        requests_per_minute=6000,
    )
    return _RequestsConfluenceClient(cfg)


def test_confluence_get_json_decodes_and_rejects_non_json():
    from harborrag_adapters.connectors.exceptions import FetchError

    client = _confluence_client()
    client.session = FakeSession(
        responses=[
            FakeResponse(status_code=200, _json={"ok": True}),
            FakeResponse(status_code=200, text="<html>not json</html>"),
        ]
    )
    assert client.get_json("content/search") == {"ok": True}
    with pytest.raises(FetchError, match="non-JSON"):
        client.get_json("content/search")


def test_confluence_download_bytes_rejects_cross_origin():
    from harborrag_adapters.connectors.exceptions import FetchError

    client = _confluence_client()
    with pytest.raises(FetchError, match="origin|scheme"):
        client.download_bytes("https://evil.example.com/secret")


def test_confluence_download_bytes_streams_capped_body():
    client = _confluence_client()
    client.config.max_attachment_size_bytes = 1024
    client.session = FakeSession(
        responses=[FakeResponse(status_code=200, _chunks=[b"hello ", b"world"])]
    )
    assert client.download_bytes("https://ex.atlassian.net/wiki/download/a") == b"hello world"


def test_github_api_url_rejects_cross_origin_absolute():
    from harborrag_adapters.connectors.exceptions import FetchError
    from harborrag_adapters.connectors.github.config import GitHubRepositoryConfig
    from harborrag_adapters.connectors.github.connector import _RequestsGitHubClient

    cfg = GitHubRepositoryConfig(owner="o", repo="r", token="t")
    client = _RequestsGitHubClient(cfg)
    assert client._api_url("repos/o/r/git/trees/main").startswith(cfg.api_url)
    with pytest.raises(FetchError):
        client._api_url("https://evil.example.com/x")


def test_sharepoint_api_url_and_non_json():
    from harborrag_adapters.connectors.exceptions import FetchError
    from harborrag_adapters.connectors.sharepoint.config import SharePointSiteConfig
    from harborrag_adapters.connectors.sharepoint.connector import _RequestsGraphClient

    cfg = SharePointSiteConfig(
        site_url="https://ex.sharepoint.com/sites/s",
        access_token="tok",
        max_file_size_bytes=1024,
    )
    client = _RequestsGraphClient(cfg)
    client.session = FakeSession(
        responses=[FakeResponse(status_code=200, text="not json")]
    )
    with pytest.raises(FetchError, match="non-JSON"):
        client.get_json("sites/x")


def test_jira_api_version_by_deployment():
    from harborrag_adapters.connectors.jira.config import (
        JiraDeploymentType,
        JiraProjectConfig,
    )
    from harborrag_adapters.connectors.jira.connector import _RequestsJiraClient

    cloud = _RequestsJiraClient(
        JiraProjectConfig(
            base_url="https://ex.atlassian.net",
            email="a@b.c",
            token="t",
            deployment_type=JiraDeploymentType.CLOUD,
            requests_per_minute=6000,
        )
    )
    dc = _RequestsJiraClient(
        JiraProjectConfig(
            base_url="https://jira.local",
            token="pat",
            deployment_type=JiraDeploymentType.DATACENTER,
            requests_per_minute=6000,
        )
    )
    assert cloud.api_version == "3"
    assert dc.api_version == "2"
    assert cloud._api_url("search/jql").endswith("/rest/api/3/search/jql")
    assert dc._api_url("search").endswith("/rest/api/2/search")
