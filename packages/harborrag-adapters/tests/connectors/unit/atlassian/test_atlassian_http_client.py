"""Unit tests for HTTP behavior shared by the Atlassian (Confluence/Jira) clients."""

from __future__ import annotations

import pytest
from harbor_test_builders import FakeResponse, FakeSession

pytestmark = [pytest.mark.unit, pytest.mark.graybox]


def confluence_client():
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


def jira_client():
    from harborrag_adapters.connectors.jira.config import (
        JiraDeploymentType,
        JiraProjectConfig,
    )
    from harborrag_adapters.connectors.jira.connector import _RequestsJiraClient

    cfg = JiraProjectConfig(
        base_url="https://ex.atlassian.net",
        email="a@b.c",
        token="t",
        deployment_type=JiraDeploymentType.CLOUD,
        requests_per_minute=6000,
        max_retries=1,
        backoff_factor=0.01,
    )
    return _RequestsJiraClient(cfg)


@pytest.fixture(autouse=True)
def _no_sleep(monkeypatch):
    import time

    monkeypatch.setattr(time, "sleep", lambda *_a, **_k: None)


@pytest.mark.parametrize(
    "factory,path",
    [(confluence_client, "/wiki/download/a"), (jira_client, "/secure/a")],
)
def test_atlassian_downloads_refuse_redirects(factory, path):
    from harborrag_adapters.connectors.exceptions import FetchError

    client = factory()
    client.session = FakeSession(
        responses=[
            FakeResponse(
                status_code=302,
                headers={"Location": "https://evil.example.com/payload"},
            )
        ]
    )

    with pytest.raises(FetchError, match="redirect"):
        client.download_bytes(f"https://ex.atlassian.net{path}")

    assert client.session.calls[0]["allow_redirects"] is False


@pytest.mark.parametrize(
    "factory,path",
    [(confluence_client, "/wiki/download/a"), (jira_client, "/secure/a")],
)
def test_retryable_response_is_closed_before_the_next_attempt(factory, path):
    client = factory()
    retried = FakeResponse(status_code=503, headers={})
    succeeded = FakeResponse(status_code=200, _chunks=[b"payload"])
    client.session = FakeSession(responses=[retried, succeeded])

    body = client.download_bytes(f"https://ex.atlassian.net{path}")

    assert body == b"payload"
    assert retried.closed is True
