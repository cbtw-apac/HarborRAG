from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import pytest
from harborrag_adapters.connectors.confluence import (
    ConfluenceConnector,
    ConfluenceDeploymentType,
    ConfluenceSpaceConfig,
)
from harborrag_adapters.connectors.confluence.content import ConfluenceContentAPI
from harborrag_adapters.connectors.confluence.mappers import (
    content_id_from_record,
    display_url,
)
from harborrag_adapters.connectors.confluence.mappers import (
    parse_timestamp as mapper_parse_timestamp,
)
from harborrag_adapters.connectors.confluence.utils import (
    build_cql,
    build_search_params,
    format_query_timestamp,
    is_cloud_hostname,
)
from harborrag_adapters.connectors.exceptions import DocumentProcessingError
from harborrag_adapters.connectors.schemas import ConnectorQuery
from harborrag_core.domain.parser import ParsedDocument
from harborrag_core.domain.source import SourceRecord

pytestmark = [pytest.mark.unit, pytest.mark.graybox]


CLOUD_BASE = "https://example.atlassian.net/wiki"
DC_BASE = "https://confluence.example.com"


class FakeConfluenceClient:
    def __init__(self) -> None:
        self.responses: dict[str, list[dict[str, Any]]] = {}
        self.downloads: dict[str, bytes] = {}
        self.calls: list[tuple[str, dict[str, Any] | None]] = []

    def add(self, endpoint: str, *responses: dict[str, Any]) -> None:
        self.responses.setdefault(endpoint, []).extend(responses)

    def get_json(
        self,
        endpoint: str,
        *,
        params: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        self.calls.append((endpoint, params))
        values = self.responses.get(endpoint)
        if not values:
            raise AssertionError(f"Unexpected Confluence endpoint: {endpoint}")
        return values.pop(0)

    def download_bytes(self, url: str) -> bytes | None:
        return self.downloads.get(url)


class FakeAttachmentParser:
    def parse(self, parse_input) -> ParsedDocument:
        return ParsedDocument(
            content=f"parsed:{parse_input.filename}",
            parser_name="fake",
        )


def cloud_config(**overrides: Any) -> ConfluenceSpaceConfig:
    values = {
        "space_key": "ENG",
        "base_url": CLOUD_BASE,
        "token": "token",
        "email": "me@example.com",
        "requests_per_minute": 6000,
        "page_size": 2,
    }
    values.update(overrides)
    return ConfluenceSpaceConfig(**values)


def dc_config(**overrides: Any) -> ConfluenceSpaceConfig:
    values = {
        "space_key": "ENG",
        "base_url": DC_BASE,
        "token": "pat",
        "requests_per_minute": 6000,
        "page_size": 2,
    }
    values.update(overrides)
    return ConfluenceSpaceConfig(**values)


def light_content(
    content_id: str,
    title: str,
    labels: list[str] | None = None,
    *,
    space_key: str = "ENG",
) -> dict:
    return {
        "id": content_id,
        "title": title,
        "type": "page",
        "space": {"key": space_key},
        "metadata": {"labels": {"results": [{"name": name} for name in labels or []]}},
        "version": {"when": "2024-05-24T20:57:56.130Z"},
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
        "body": {"export_view": {"value": "<p>Hello <b>World</b></p>"}},
        "ancestors": [{"id": "0", "title": "Root", "type": "page"}],
        "children": {"page": {"results": [{"id": "9", "title": "Child"}]}},
    }


def test_config_auto_detects_cloud_and_datacenter():
    assert cloud_config().deployment_type == ConfluenceDeploymentType.CLOUD
    assert dc_config().deployment_type == ConfluenceDeploymentType.DATACENTER


def test_config_requires_cloud_email_and_rejects_bad_content_type():
    with pytest.raises(ValueError, match="email is required"):
        ConfluenceSpaceConfig(space_key="ENG", base_url=CLOUD_BASE, token="token")
    with pytest.raises(ValueError, match="content_types"):
        cloud_config(content_types=["page", "comment"])


def test_build_cql_supports_incremental_sync_and_rejects_unsafe_tokens():
    cql = build_cql(
        space_key="ENG",
        content_types=["page"],
        labels=["runbook"],
        updated_after=datetime(2024, 1, 2, 3, 4, tzinfo=UTC),
    )

    assert 'space = "ENG"' in cql
    assert 'type in ("page")' in cql
    assert 'label in ("runbook")' in cql
    assert 'lastmodified >= "2024/01/02 03:04"' in cql
    with pytest.raises(ValueError, match="Invalid Confluence space key"):
        build_cql(space_key='ENG" OR space = "OTHER')


def test_discover_paginates_and_filters_excluded_labels():
    client = FakeConfluenceClient()
    client.add(
        "content/search",
        {
            "results": [
                light_content("1", "Keep"),
                light_content("2", "Skip", labels=["archived"]),
            ],
            "_links": {"next": "/rest/api/content/search?cursor=abc"},
        },
        {"results": [light_content("3", "Keep Also")], "_links": {}},
    )
    connector = ConfluenceConnector(
        cloud_config(exclude_labels=["archived"]),
        client=client,
    )

    records = list(connector.discover())

    assert [record.metadata["content_id"] for record in records] == ["1", "3"]
    assert records[0].id == "confluence://ENG/1"
    assert client.calls[1][1]["cursor"] == "abc"


def test_discover_supports_direct_content_ids_without_search():
    client = FakeConfluenceClient()
    client.add("content/1", light_content("1", "Direct"))
    connector = ConfluenceConnector(cloud_config(), client=client)

    records = list(
        connector.discover(ConnectorQuery(filters={"content_ids": ["1", "2"]}, limit=1))
    )

    assert [record.locator for record in records] == ["1"]
    assert records[0].id == "confluence://ENG/1"
    assert records[0].metadata["space_key"] == "ENG"


def test_discover_rejects_direct_content_ids_outside_configured_space():
    client = FakeConfluenceClient()
    client.add("content/1", light_content("1", "Direct", space_key="OPS"))
    connector = ConfluenceConnector(cloud_config(), client=client)

    with pytest.raises(DocumentProcessingError, match="outside configured space"):
        list(connector.discover(ConnectorQuery(filters={"content_ids": ["1"]})))


def test_discover_rejects_query_space_override():
    connector = ConfluenceConnector(cloud_config(), client=FakeConfluenceClient())

    with pytest.raises(ValueError, match="outside configured space"):
        list(connector.discover(ConnectorQuery(filters={"space_key": "OPS"})))


def test_discover_can_expand_child_pages_for_direct_ids():
    client = FakeConfluenceClient()
    client.add("content/1", light_content("1", "Parent"))
    client.add("content/2", light_content("2", "Child"))
    client.add(
        "content/1/child/page",
        {"results": [light_content("2", "Child")]},
    )
    client.add("content/2/child/page", {"results": []})
    connector = ConfluenceConnector(cloud_config(), client=client)

    records = list(
        connector.discover(
            ConnectorQuery(filters={"content_ids": ["1"], "include_children": True})
        )
    )

    assert [record.locator for record in records] == ["1", "2"]


def test_direct_id_discovery_stops_before_child_traversal_at_limit():
    client = FakeConfluenceClient()
    client.add("content/1", light_content("1", "Parent"))
    connector = ConfluenceConnector(cloud_config(), client=client)

    records = list(
        connector.discover(
            ConnectorQuery(
                filters={"content_ids": ["1"], "include_children": True},
                recursive=True,
                limit=1,
            )
        )
    )

    assert [record.locator for record in records] == ["1"]
    assert [endpoint for endpoint, _params in client.calls] == ["content/1"]


def test_load_builds_raw_document_metadata_comments_and_attachments():
    client = FakeConfluenceClient()
    client.add("content/1", full_content())
    client.add(
        "content/1/child/comment",
        {
            "results": [
                {
                    "id": "c1",
                    "body": {"storage": {"value": "<p>Nice</p>"}},
                    "history": {"createdBy": {"displayName": "Carol"}},
                }
            ]
        },
    )
    client.add(
        "content/1/child/attachment",
        {
            "results": [
                {
                    "id": "a1",
                    "title": "notes.md",
                    "metadata": {"mediaType": "text/markdown"},
                    "extensions": {"fileSize": 12},
                    "_links": {"download": "/download/a1"},
                }
            ]
        },
    )
    client.downloads[f"{CLOUD_BASE}/download/a1"] = b"# Notes"
    connector = ConfluenceConnector(
        cloud_config(include_comments=True, include_attachments=True),
        client=client,
        parser=FakeAttachmentParser(),
    )

    document = connector.load(SourceRecord("confluence://ENG/1", "text/html", "1"))

    assert document.id == "confluence://ENG/1"
    assert document.content_type == "text/html"
    assert document.content == "<p>Hello <b>World</b></p>"
    assert document.source == f"{CLOUD_BASE}/spaces/ENG/pages/1"
    assert document.metadata["author"] == "Alice"
    assert document.metadata["breadcrumb"] == ["Root"]
    assert document.metadata["children"] == [
        {"id": "9", "title": "Child", "type": "page"}
    ]
    assert document.metadata["comments"][0]["author"] == "Carol"
    processed_attachments = [
        attachment
        for attachment in document.metadata["attachments"]
        if attachment["status"] == "processed"
    ]
    assert len(processed_attachments) == 1
    assert document.metadata["attachments"][0]["text"] == "parsed:notes.md"
    assert "attachments_summary" not in document.metadata
    assert "breadcrumb_text" not in document.metadata
    assert "canonical_url" not in document.metadata
    assert "display_url" not in document.metadata


def test_load_skips_cross_origin_attachment_download_urls():
    client = FakeConfluenceClient()
    client.add("content/1", full_content())
    client.add(
        "content/1/child/attachment",
        {
            "results": [
                {
                    "id": "a1",
                    "title": "notes.md",
                    "metadata": {"mediaType": "text/markdown"},
                    "extensions": {"fileSize": 12},
                    "_links": {"download": "https://evil.example/notes.md"},
                }
            ]
        },
    )
    connector = ConfluenceConnector(
        cloud_config(include_attachments=True),
        client=client,
        parser=FakeAttachmentParser(),
    )

    document = connector.load(SourceRecord("confluence://ENG/1", "text/html", "1"))

    skipped_attachments = [
        attachment
        for attachment in document.metadata["attachments"]
        if attachment["status"] == "skipped"
    ]
    assert len(skipped_attachments) == 1
    assert "outside trusted origin" in document.metadata["attachments"][0]["reason"]


def test_load_rejects_comment_pages_over_configured_limit():
    client = FakeConfluenceClient()
    client.add("content/1", full_content())
    client.add(
        "content/1/child/comment",
        {
            "results": [
                {"id": "c1", "body": {"storage": {"value": "one"}}},
                {"id": "c2", "body": {"storage": {"value": "two"}}},
            ]
        },
    )
    connector = ConfluenceConnector(
        cloud_config(include_comments=True, max_comments=1),
        client=client,
    )

    with pytest.raises(DocumentProcessingError, match="max_comments"):
        connector.load(SourceRecord("confluence://ENG/1", "text/html", "1"))


def test_load_rejects_attachment_pages_over_configured_limit():
    client = FakeConfluenceClient()
    client.add("content/1", full_content())
    client.add(
        "content/1/child/attachment",
        {
            "results": [
                {"id": "a1", "title": "one.md"},
                {"id": "a2", "title": "two.md"},
            ]
        },
    )
    connector = ConfluenceConnector(
        cloud_config(include_attachments=True, max_attachments=1),
        client=client,
    )

    with pytest.raises(DocumentProcessingError, match="max_attachments"):
        connector.load(SourceRecord("confluence://ENG/1", "text/html", "1"))


def test_load_raises_on_missing_required_fields():
    client = FakeConfluenceClient()
    content = full_content()
    del content["title"]
    client.add("content/1", content)
    connector = ConfluenceConnector(cloud_config(), client=client)

    with pytest.raises(DocumentProcessingError, match="title"):
        connector.load(SourceRecord("confluence://ENG/1", "text/html", "1"))


# --------------------------------------------------------------------------
# config.py validation


def test_config_rejects_missing_token():
    with pytest.raises(ValueError, match="token is required"):
        ConfluenceSpaceConfig(space_key="ENG", base_url=DC_BASE, token=None, email=None)


def test_config_accepts_deployment_type_enum_directly():
    cfg = ConfluenceSpaceConfig(
        space_key="ENG",
        base_url=DC_BASE,
        token="pat",
        deployment_type=ConfluenceDeploymentType.CLOUD,
        email="me@example.com",
    )
    assert cfg.deployment_type == ConfluenceDeploymentType.CLOUD


def test_config_rejects_out_of_range_requests_per_minute():
    with pytest.raises(ValueError, match="requests_per_minute must be between"):
        dc_config(requests_per_minute=0)


def test_config_rejects_out_of_range_page_size():
    with pytest.raises(ValueError, match="page_size must be between"):
        dc_config(page_size=0)


# --------------------------------------------------------------------------
# utils.py pure helpers


def test_is_cloud_hostname_handles_malformed_url():
    assert is_cloud_hostname("http://[invalid") is False


def test_format_query_timestamp_assumes_utc_for_naive_datetime():
    naive = datetime(2024, 1, 2, 3, 4)
    assert format_query_timestamp(naive) == "2024/01/02 03:04"


def test_build_cql_raw_cql_passthrough():
    assert build_cql(raw_cql="type = page") == "type = page"


def test_build_cql_without_space_key_or_content_types():
    cql = build_cql(labels=["runbook"])
    assert cql == 'label in ("runbook")'


def test_build_search_params_without_cursor_or_start():
    params = build_search_params(cql="type=page")
    assert "cursor" not in params
    assert "start" not in params


# --------------------------------------------------------------------------
# mappers.py edge cases


def test_parse_timestamp_handles_missing_and_invalid_values():
    assert mapper_parse_timestamp(None) is None
    assert mapper_parse_timestamp("not-a-timestamp") is None


def test_content_id_from_record_requires_a_content_id():
    record = SourceRecord("confluence://ENG/x", "text/html", "")
    record.metadata.pop("content_id", None)
    with pytest.raises(ValueError, match="does not contain content_id"):
        content_id_from_record(record)


def test_content_id_from_record_rejects_path_fragments():
    record = SourceRecord("confluence://ENG/1", "text/html", "1/child/page")

    with pytest.raises(ValueError, match="content ID"):
        content_id_from_record(record)


def test_display_url_datacenter_uses_display_path():
    url = display_url(
        DC_BASE, ConfluenceDeploymentType.DATACENTER, "ENG", "1", "Page One"
    )
    assert url == f"{DC_BASE}/display/ENG/Page+One"


# --------------------------------------------------------------------------
# content.py pagination edge cases


def test_search_returns_immediately_on_empty_first_page():
    client = FakeConfluenceClient()
    client.add("content/search", {"results": []})
    api = ConfluenceContentAPI(client, cloud_config())

    assert list(api.search("type=page")) == []


def test_search_falls_back_to_offset_when_first_full_page_has_no_next_link():
    client = FakeConfluenceClient()
    client.add(
        "content/search",
        {
            "results": [light_content("1", "A"), light_content("2", "B")],
            "_links": {},
        },
        {"results": [light_content("3", "C")], "_links": {}},
    )
    api = ConfluenceContentAPI(client, cloud_config(page_size=2))

    ids = [item["id"] for item in api.search("type=page")]

    assert ids == ["1", "2", "3"]
    assert client.calls[1][1]["start"] == 2


def test_cursor_search_stops_after_exact_full_final_page_without_next_link():
    client = FakeConfluenceClient()
    client.add(
        "content/search",
        {
            "results": [light_content("1", "A"), light_content("2", "B")],
            "_links": {"next": "/rest/api/content/search?cursor=next-page"},
        },
        {
            "results": [light_content("3", "C"), light_content("4", "D")],
            "_links": {},
        },
    )
    api = ConfluenceContentAPI(client, cloud_config(page_size=2))

    ids = [item["id"] for item in api.search("type=page")]

    assert ids == ["1", "2", "3", "4"]
    assert len(client.calls) == 2
    assert client.calls[1][1]["cursor"] == "next-page"


def test_fetch_comments_stops_on_short_final_page():
    client = FakeConfluenceClient()
    client.add(
        "content/1/child/comment",
        {"results": [{"id": "c1"}]},
    )
    api = ConfluenceContentAPI(client, cloud_config(page_size=2))

    comments = api.fetch_comments("1")

    assert [c["id"] for c in comments] == ["c1"]


def test_fetch_comments_paginates_across_multiple_pages():
    client = FakeConfluenceClient()
    client.add(
        "content/1/child/comment",
        {"results": [{"id": "c1"}, {"id": "c2"}]},
        {"results": [{"id": "c3"}]},
    )
    api = ConfluenceContentAPI(client, cloud_config(page_size=2))

    comments = api.fetch_comments("1")

    assert [c["id"] for c in comments] == ["c1", "c2", "c3"]


def test_fetch_comments_honors_server_clamped_page_limit():
    client = FakeConfluenceClient()
    client.add(
        "content/1/child/comment",
        {
            "results": [{"id": "c1"}, {"id": "c2"}],
            "size": 2,
            "limit": 2,
            "_links": {},
        },
        {"results": [{"id": "c3"}], "size": 1, "limit": 2, "_links": {}},
    )
    api = ConfluenceContentAPI(client, cloud_config(page_size=100))

    comments = api.fetch_comments("1")

    assert [comment["id"] for comment in comments] == ["c1", "c2", "c3"]
    assert client.calls[1][1]["start"] == 2


def test_list_attachments_stops_on_short_final_page():
    client = FakeConfluenceClient()
    client.add(
        "content/1/child/attachment",
        {"results": [{"id": "a1"}]},
    )
    api = ConfluenceContentAPI(client, cloud_config(page_size=2))

    attachments = api.list_attachments("1")

    assert [a["id"] for a in attachments] == ["a1"]


def test_list_attachments_paginates_across_multiple_pages():
    client = FakeConfluenceClient()
    client.add(
        "content/1/child/attachment",
        {"results": [{"id": "a1"}, {"id": "a2"}]},
        {"results": [{"id": "a3"}]},
    )
    api = ConfluenceContentAPI(client, cloud_config(page_size=2))

    attachments = api.list_attachments("1")

    assert [a["id"] for a in attachments] == ["a1", "a2", "a3"]


def test_list_attachments_honors_server_clamped_page_limit():
    client = FakeConfluenceClient()
    client.add(
        "content/1/child/attachment",
        {
            "results": [{"id": "a1"}, {"id": "a2"}],
            "size": 2,
            "limit": 2,
            "_links": {"next": "/next"},
        },
        {"results": [{"id": "a3"}], "size": 1, "limit": 2, "_links": {}},
    )
    api = ConfluenceContentAPI(client, cloud_config(page_size=100))

    attachments = api.list_attachments("1")

    assert [attachment["id"] for attachment in attachments] == ["a1", "a2", "a3"]
    assert client.calls[1][1]["start"] == 2


def test_with_children_skips_duplicate_ids():
    api = ConfluenceContentAPI(FakeConfluenceClient(), cloud_config())

    ids = list(api.with_children(["1", "1", "2"], ConnectorQuery()))

    assert ids == ["1", "2"]


def test_child_page_ids_skips_already_seen_and_missing_ids():
    client = FakeConfluenceClient()
    client.add(
        "content/1/child/page",
        {"results": [{"id": "2"}, {}, {"id": "2"}]},
    )
    api = ConfluenceContentAPI(client, cloud_config(page_size=10))

    ids = list(api.child_page_ids("1", recursive=False, seen={"1"}))

    assert ids == ["2"]


def test_child_page_ids_non_recursive_does_not_descend():
    client = FakeConfluenceClient()
    client.add(
        "content/1/child/page",
        {"results": [{"id": "2"}]},
    )
    api = ConfluenceContentAPI(client, cloud_config())

    ids = list(api.child_page_ids("1", recursive=False, seen=set()))

    assert ids == ["2"]
    assert ("content/2/child/page", None) not in [
        (c[0], c[1]) for c in client.calls if "start" not in (c[1] or {})
    ]


def test_child_page_ids_paginates_across_multiple_pages():
    client = FakeConfluenceClient()
    client.add(
        "content/1/child/page",
        {"results": [{"id": "2"}, {"id": "3"}]},
        {"results": [{"id": "4"}]},
    )
    api = ConfluenceContentAPI(client, cloud_config(page_size=2))

    ids = list(api.child_page_ids("1", recursive=False, seen=set()))

    assert ids == ["2", "3", "4"]


def test_child_page_ids_honors_server_clamped_page_limit():
    client = FakeConfluenceClient()
    client.add(
        "content/1/child/page",
        {
            "results": [{"id": "2"}, {"id": "3"}],
            "size": 2,
            "limit": 2,
            "_links": {"next": "/next"},
        },
        {"results": [{"id": "4"}], "size": 1, "limit": 2, "_links": {}},
    )
    api = ConfluenceContentAPI(client, cloud_config(page_size=100))

    ids = list(api.child_page_ids("1", recursive=False, seen=set()))

    assert ids == ["2", "3", "4"]
    assert client.calls[1][1]["start"] == 2


# --------------------------------------------------------------------------
# connector.py discover/load edge cases


def test_discover_stops_at_limit_during_search():
    client = FakeConfluenceClient()
    client.add(
        "content/search",
        {
            "results": [light_content("1", "A"), light_content("2", "B")],
            "_links": {},
        },
    )
    connector = ConfluenceConnector(cloud_config(), client=client)

    records = list(connector.discover(ConnectorQuery(limit=1)))

    assert [r.metadata["content_id"] for r in records] == ["1"]


def test_load_raises_when_content_filtered_out_by_labels():
    client = FakeConfluenceClient()
    content = full_content()
    content["metadata"] = {"labels": {"results": [{"name": "archived"}]}}
    client.add("content/1", content)
    connector = ConfluenceConnector(
        cloud_config(exclude_labels=["archived"]), client=client
    )

    with pytest.raises(DocumentProcessingError, match="does not match label filters"):
        connector.load(SourceRecord("confluence://ENG/1", "text/html", "1"))


def test_load_by_ids_loads_each_content_id():
    client = FakeConfluenceClient()
    client.add("content/1", light_content("1", "Page One"), full_content())
    connector = ConfluenceConnector(cloud_config(), client=client)

    documents = list(connector.load_by_ids(["1"]))

    assert [d.id for d in documents] == ["confluence://ENG/1"]


def test_content_ids_from_query_accepts_bare_string():
    client = FakeConfluenceClient()
    client.add("content/1", light_content("1", "Direct"))
    connector = ConfluenceConnector(cloud_config(), client=client)

    records = list(connector.discover(ConnectorQuery(filters={"content_ids": "1"})))

    assert [r.locator for r in records] == ["1"]


@pytest.mark.parametrize("content_id", ["1/child/page", "1?expand=body", "#1"])
def test_discover_rejects_unsafe_content_ids(content_id):
    connector = ConfluenceConnector(cloud_config(), client=FakeConfluenceClient())

    with pytest.raises(ValueError, match="content ID"):
        list(connector.discover(ConnectorQuery(filters={"content_ids": [content_id]})))


def test_discover_content_types_filter_accepts_list_value():
    client = FakeConfluenceClient()
    client.add(
        "content/search",
        {"results": [light_content("1", "A")], "_links": {}},
    )
    connector = ConfluenceConnector(cloud_config(page_size=10), client=client)

    records = list(
        connector.discover(
            ConnectorQuery(filters={"content_types": ["page", "blogpost"]})
        )
    )

    assert [r.metadata["content_id"] for r in records] == ["1"]


def test_discover_labels_filter_uses_label_alias_and_matches_include_labels():
    client = FakeConfluenceClient()
    client.add(
        "content/search",
        {
            "results": [
                light_content("1", "Match", labels=["runbook"]),
                light_content("2", "NoMatch", labels=["draft"]),
            ],
            "_links": {},
        },
    )
    connector = ConfluenceConnector(
        cloud_config(include_labels=["runbook"], page_size=10), client=client
    )

    records = list(connector.discover(ConnectorQuery(filters={"label": "runbook"})))

    assert [r.metadata["content_id"] for r in records] == ["1"]
