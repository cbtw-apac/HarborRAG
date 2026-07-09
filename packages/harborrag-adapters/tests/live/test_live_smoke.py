"""Live smoke tests against real external services.

These call real Confluence/JIRA/GitHub/SharePoint APIs (and can scan a real
local directory or parse real sample documents) using credentials read from
the environment — see ``.env.example`` at the repo root for the full variable
list and ``tests/live_env.py`` for how each config is built.

Marked ``@pytest.mark.integration`` per this repo's testing convention
(docs/developers/testing/README.md): tests that require cloud credentials or
live services must not run by default. Each test also skips itself with a
clear reason when its required variables are absent, so ``pytest -m
integration`` is safe to run with only some connectors configured.

Run:
    set -a && source .env && set +a
    pytest packages/harborrag-adapters/tests/live -m integration -v -s
"""
from __future__ import annotations

import pytest

import live_env
from harborrag_adapters.connectors.confluence.connector import ConfluenceConnector
from harborrag_adapters.connectors.github.connector import GitHubConnector
from harborrag_adapters.connectors.jira.connector import JiraConnector
from harborrag_adapters.connectors.local.connector import LocalFileConnector
from harborrag_adapters.connectors.schemas import ConnectorQuery
from harborrag_adapters.connectors.sharepoint.connector import SharePointConnector
from harborrag_adapters.parsers import HarborParser
from harborrag_core.domain.parser import ParseInput


pytestmark = pytest.mark.integration


def _summarize(label: str, records: list, connector) -> None:
    print(f"\n[{label}] discovered {len(records)} record(s)")
    for record in records:
        print(f"  - {record.id}  ({record.source_type})")
    if not records:
        return
    document = connector.load(records[0])
    print(
        f"  loaded {records[0].id}: {len(document.text())} chars, "
        f"content_type={document.content_type}"
    )
    assert document.id
    assert document.content is not None


_confluence_missing = live_env.confluence_missing_vars()


@pytest.mark.skipif(
    bool(_confluence_missing),
    reason=f"missing env vars: {_confluence_missing}",
)
def test_confluence_live_discovery() -> None:
    connector = ConfluenceConnector(live_env.confluence_config())
    records = list(connector.discover(ConnectorQuery(limit=3)))
    _summarize("confluence", records, connector)


_jira_missing = live_env.jira_missing_vars()


@pytest.mark.skipif(bool(_jira_missing), reason=f"missing env vars: {_jira_missing}")
def test_jira_live_discovery() -> None:
    connector = JiraConnector(live_env.jira_config())
    records = list(connector.discover(ConnectorQuery(limit=3)))
    _summarize("jira", records, connector)


_github_missing = live_env.github_missing_vars()


@pytest.mark.skipif(bool(_github_missing), reason=f"missing env vars: {_github_missing}")
def test_github_live_discovery() -> None:
    connector = GitHubConnector(live_env.github_config())
    records = list(connector.discover(ConnectorQuery(limit=3)))
    _summarize("github", records, connector)


_sharepoint_missing = live_env.sharepoint_missing_vars()


@pytest.mark.skipif(
    bool(_sharepoint_missing),
    reason=f"missing env vars: {_sharepoint_missing}",
)
def test_sharepoint_live_discovery() -> None:
    connector = SharePointConnector(live_env.sharepoint_config())
    records = list(connector.discover(ConnectorQuery(limit=3)))
    _summarize("sharepoint", records, connector)


_local_missing = live_env.local_missing_vars()


@pytest.mark.skipif(bool(_local_missing), reason=f"missing env vars: {_local_missing}")
def test_local_live_discovery() -> None:
    connector = LocalFileConnector(live_env.local_config())
    records = list(connector.discover(ConnectorQuery(limit=5)))
    _summarize("local", records, connector)


@pytest.mark.parametrize("kind", ["pdf", "docx", "pptx", "xlsx"])
def test_parser_live_sample_document(kind: str) -> None:
    path = live_env.sample_doc_path(kind)
    if path is None:
        pytest.skip(f"SAMPLE_{kind.upper()}_PATH not set or file missing")
    document = HarborParser().parse(ParseInput(path=path))
    print(
        f"\n[{kind}] parsed {path.name}: {len(document.content)} chars, "
        f"parser={document.parser_name}"
    )
    assert document.content is not None
