from __future__ import annotations

import time
from datetime import UTC, datetime

import pytest
import requests

from harborrag_adapters.connectors.confluence.config import (
    ConfluenceDeploymentType,
    ConfluenceSpaceConfig,
)
from harborrag_adapters.connectors.confluence.connector import ConfluenceConnector
from harborrag_adapters.connectors.confluence.pagination import build_cloud_search_params
from harborrag_adapters.connectors.exceptions import DocumentProcessingError
from harborrag_adapters.connectors.schemas import ConnectorQuery
from harborrag_adapters.connectors.shared.http.errors import HTTPRequestError
from harborrag_adapters.connectors.shared.http.sync_client import (
    SyncRateLimiter,
    request_with_retries_sync,
)

CLOUD_BASE = "https://example.atlassian.net/wiki"
DC_BASE = "https://confluence.mycompany.com"


def cloud_config(**overrides) -> ConfluenceSpaceConfig:
    kwargs = {
        "space_key": "ENG",
        "base_url": CLOUD_BASE,
        "token": "tok",
        "email": "a@b.com",
        "requests_per_minute": 1000,
    }
    kwargs.update(overrides)
    return ConfluenceSpaceConfig(**kwargs)


def dc_config(**overrides) -> ConfluenceSpaceConfig:
    kwargs = {
        "space_key": "ENG",
        "base_url": DC_BASE,
        "token": "pat",
        "requests_per_minute": 1000,
    }
    kwargs.update(overrides)
    return ConfluenceSpaceConfig(**kwargs)


def light_result(content_id: str, title: str, labels: list[str] | None = None) -> dict:
    return {
        "id": content_id,
        "title": title,
        "type": "page",
        "metadata": {"labels": {"results": [{"name": n} for n in (labels or [])]}},
    }


def full_content(content_id: str = "1") -> dict:
    return {
        "id": content_id,
        "title": "Page One",
        "type": "page",
        "space": {"key": "ENG"},
        "version": {
            "number": 3,
            "when": "2024-05-24T20:57:56.130Z",
            "by": {"displayName": "Bob"},
        },
        "history": {
            "createdBy": {"displayName": "Alice"},
            "createdDate": "2023-01-01T00:00:00.000Z",
        },
        "metadata": {"labels": {"results": [{"name": "runbook"}]}},
        "body": {"storage": {"value": "<p>Hello <b>World</b></p>"}},
        "ancestors": [{"id": "0", "title": "Root", "type": "page"}],
        "children": {"page": {"results": [{"id": "9", "title": "Child", "type": "page"}]}},
        "_links": {},
    }


# --- config -----------------------------------------------------------------


def test_config_auto_detects_cloud_from_url():
    config = ConfluenceSpaceConfig(
        space_key="ENG", base_url=CLOUD_BASE, token="tok", email="a@b.com"
    )
    assert config.deployment_type == ConfluenceDeploymentType.CLOUD


def test_config_auto_detects_datacenter_from_url():
    """Regression test: deployment_type used to silently default to CLOUD
    because the auto-detect heuristic was never wired up from config."""
    config = ConfluenceSpaceConfig(space_key="ENG", base_url=DC_BASE, token="pat")
    assert config.deployment_type == ConfluenceDeploymentType.DATACENTER


def test_config_requires_email_for_cloud():
    with pytest.raises(ValueError, match="Email is required"):
        ConfluenceSpaceConfig(space_key="ENG", base_url=CLOUD_BASE, token="tok")


def test_config_rejects_invalid_content_type():
    with pytest.raises(ValueError, match="Content type"):
        cloud_config(content_types=["page", "not-a-real-type"])


# --- pagination ---------------------------------------------------------------


def test_build_cloud_search_params_appends_lastmodified_for_incremental_sync():
    params = build_cloud_search_params(
        "ENG", ["page"], None, updated_after=datetime(2024, 1, 2, 3, 4, tzinfo=UTC)
    )
    assert 'lastmodified >= "2024/01/02 03:04"' in params["cql"]


def test_build_cloud_search_params_rejects_unsafe_space_key():
    with pytest.raises(ValueError):
        build_cloud_search_params('ENG" OR space = "OTHER', None, None)


# --- discover -----------------------------------------------------------------


def test_discover_cloud_paginates_via_cursor_and_filters_labels(requests_mock):
    config = cloud_config(exclude_labels=["archived"])
    connector = ConfluenceConnector(config)

    requests_mock.get(
        f"{CLOUD_BASE}/rest/api/content/search",
        [
            {
                "json": {
                    "results": [
                        light_result("1", "Keep Me"),
                        light_result("2", "Drop Me", labels=["archived"]),
                    ],
                    "_links": {"next": "/rest/api/content/search?cursor=abc&limit=25"},
                }
            },
            {"json": {"results": [light_result("3", "Also Keep")], "_links": {}}},
        ],
    )

    records = list(connector.discover())

    assert [r.metadata["content_id"] for r in records] == ["1", "3"]
    assert records[0].id == "confluence://ENG/1"
    assert records[0].locator == f"{CLOUD_BASE}/rest/api/content/1"
    assert records[0].source_type == "text/html"


def test_discover_datacenter_paginates_via_start_offset(requests_mock):
    # Real Confluence only returns a second page once the first is full
    # (limit=25), so simulate 30 total items across two pages to exercise
    # the start+=25 pagination loop rather than the single-page case.
    config = dc_config()
    connector = ConfluenceConnector(config)

    page_one = [light_result(str(i), f"Page {i}") for i in range(25)]
    page_two = [light_result(str(i), f"Page {i}") for i in range(25, 30)]

    requests_mock.get(
        f"{DC_BASE}/rest/api/content/search",
        [
            {"json": {"results": page_one, "totalSize": 30}},
            {"json": {"results": page_two, "totalSize": 30}},
        ],
    )

    records = list(connector.discover())
    assert [r.metadata["content_id"] for r in records] == [str(i) for i in range(30)]
    assert requests_mock.call_count == 2


def test_discover_respects_query_limit(requests_mock):
    config = cloud_config()
    connector = ConfluenceConnector(config)

    requests_mock.get(
        f"{CLOUD_BASE}/rest/api/content/search",
        json={
            "results": [light_result(str(i), f"Page {i}") for i in range(5)],
            "_links": {},
        },
    )

    records = list(connector.discover(ConnectorQuery(limit=2)))
    assert len(records) == 2


# --- load -----------------------------------------------------------------


def test_load_builds_rich_metadata_for_future_graph_mapping(requests_mock):
    config = cloud_config()
    connector = ConfluenceConnector(config)
    requests_mock.get(f"{CLOUD_BASE}/rest/api/content/1", json=full_content())

    record = connector._to_source_record(light_result("1", "Page One"))
    doc = connector.load(record)

    assert doc.content_type == "text/html"
    assert doc.content == "<p>Hello <b>World</b></p>"
    assert doc.source == f"{CLOUD_BASE}/spaces/ENG/pages/1"

    meta = doc.metadata
    assert meta["space_key"] == "ENG"
    assert meta["author"] == "Alice"
    assert meta["updated_at"] == datetime.fromisoformat("2024-05-24T20:57:56.130+00:00")
    assert meta["labels"] == ["runbook"]
    assert meta["parent_id"] == "0"
    assert meta["breadcrumb_text"] == "Root"
    assert meta["children"] == [{"id": "9", "title": "Child", "type": "page"}]
    assert meta["body_missing"] is False


def test_load_raises_when_source_record_missing_content_id():
    connector = ConfluenceConnector(cloud_config())
    from harborrag_core.domain.source import SourceRecord

    bad_record = SourceRecord(id="x", source_type="text/html", locator="l", metadata={})
    with pytest.raises(DocumentProcessingError):
        connector.load(bad_record)


def test_load_raises_on_missing_required_fields(requests_mock):
    connector = ConfluenceConnector(cloud_config())
    incomplete = full_content()
    del incomplete["title"]
    requests_mock.get(f"{CLOUD_BASE}/rest/api/content/1", json=incomplete)

    record = connector._to_source_record(light_result("1", "Page One"))
    with pytest.raises(DocumentProcessingError, match="title"):
        connector.load(record)


# --- retry --------------------------------------------------------------------


def test_request_with_retries_sync_retries_on_503_then_succeeds(requests_mock):
    requests_mock.get(
        "https://api.test/thing",
        [{"status_code": 503}, {"status_code": 200, "json": {"ok": True}}],
    )
    session = requests.Session()
    response = request_with_retries_sync(
        session, "GET", "https://api.test/thing", retries=3, backoff_factor=0.0
    )
    assert response.status_code == 200
    assert requests_mock.call_count == 2


def test_request_with_retries_sync_raises_after_exhausting_retries(requests_mock):
    requests_mock.get("https://api.test/thing", status_code=503)
    session = requests.Session()
    with pytest.raises(HTTPRequestError):
        request_with_retries_sync(
            session, "GET", "https://api.test/thing", retries=2, backoff_factor=0.0
        )
    assert requests_mock.call_count == 3  # initial attempt + 2 retries


def test_sync_rate_limiter_enforces_minimum_interval():
    limiter = SyncRateLimiter(requests_per_interval=20, interval_seconds=1.0)
    start = time.monotonic()
    limiter.acquire()
    limiter.acquire()
    elapsed = time.monotonic() - start
    assert elapsed >= 0.04  # ~1/20s minimum interval, allow scheduling slack
