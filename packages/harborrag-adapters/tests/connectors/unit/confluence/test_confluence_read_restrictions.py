"""Confluence page and ancestor read restrictions are captured and fail closed."""

from __future__ import annotations

import pytest
from confluence_test_helpers import (
    CLOUD_BASE,
    FakeConfluenceClient,
    cloud_config,
    dc_config,
    full_content,
    light_content,
    unrestricted_read,
)

from harborrag_adapters.connectors.acl import SOURCE_ACL_METADATA_KEY
from harborrag_adapters.connectors.confluence import ConfluenceConnector
from harborrag_adapters.connectors.confluence.access import (
    ConfluenceAccessResolver,
    read_restriction_principals,
)
from harborrag_adapters.connectors.confluence.content import ConfluenceContentAPI
from harborrag_adapters.connectors.confluence.query import (
    CONTENT_EXPAND,
    DESCRIPTOR_EXPAND,
    READ_RESTRICTIONS_EXPAND,
)
from harborrag_adapters.connectors.exceptions import AuthorizationError, SourceRestrictedError
from harborrag_adapters.connectors.schemas import (
    SKIP_REASON_ACCESS_RESTRICTED,
    SKIP_REASON_ACCESS_UNRESOLVED,
    ConnectorQuery,
)
from harborrag_core.domain.source import SourceRecord

pytestmark = [pytest.mark.unit, pytest.mark.graybox]


def read_restricted_to(*, users: tuple[str, ...] = (), groups: tuple[str, ...] = ()) -> dict:
    return {
        "read": {
            "operation": "read",
            "restrictions": {
                "user": {
                    "results": [
                        {"type": "known", "accountId": user, "displayName": user.title()}
                        for user in users
                    ],
                    "start": 0,
                    "limit": 200,
                    "size": len(users),
                },
                "group": {
                    "results": [
                        {"type": "group", "name": group, "id": f"id-{group}"} for group in groups
                    ],
                    "start": 0,
                    "limit": 200,
                    "size": len(groups),
                },
            },
        }
    }


def restricted_page(content_id: str = "2", **who: tuple[str, ...]) -> dict:
    page = light_content(content_id, "Secret")
    page["restrictions"] = read_restricted_to(**who)
    return page


def resolver(client: FakeConfluenceClient | None = None) -> ConfluenceAccessResolver:
    return ConfluenceAccessResolver(
        ConfluenceContentAPI(client or FakeConfluenceClient(), cloud_config())
    )


def page_record(content_id: str) -> SourceRecord:
    return SourceRecord(
        id=f"confluence://ENG/{content_id}",
        source_type="page",
        locator=content_id,
        metadata={"content_id": content_id},
    )


def test_expansions_request_read_restrictions():
    assert READ_RESTRICTIONS_EXPAND in DESCRIPTOR_EXPAND
    assert READ_RESTRICTIONS_EXPAND in CONTENT_EXPAND


def test_unrestricted_page_without_ancestors_is_inherited():
    assert resolver().page_acl(light_content("1", "Open")).status == "inherited"


def test_page_restriction_captures_users_and_groups():
    acl = resolver().page_acl(restricted_page(users=("user-a",), groups=("group-a",)))

    assert acl.status == "restricted"
    assert acl.reason == "confluence_read_restriction"
    assert [principal.key for principal in acl.levels[0]] == ["user:user-a", "group:id-group-a"]


def test_datacenter_restriction_shape_is_captured():
    page = light_content("5", "DC page")
    page["restrictions"] = read_restricted_to()
    inner = page["restrictions"]["read"]["restrictions"]
    inner["user"]["results"] = [{"type": "known", "username": "user-b", "userKey": "user-key-b"}]
    inner["group"]["results"] = [{"type": "group", "name": "group-c"}]
    inner["user"]["size"] = inner["group"]["size"] = 1

    acl = resolver().page_acl(page)

    assert [principal.key for principal in acl.levels[0]] == [
        "user:user-key-b",
        "group:group-c",
    ]


def test_ancestor_restriction_is_fetched_once_and_becomes_its_own_level():
    client = FakeConfluenceClient()
    client.add("content/77", {"id": "77", "restrictions": read_restricted_to(groups=("group-b",))})
    access = resolver(client)
    first = light_content("1", "Child A")
    first["ancestors"] = [{"id": "77", "title": "Parent"}]
    second = light_content("2", "Child B")
    second["ancestors"] = [{"id": "77", "title": "Parent"}]
    second["restrictions"] = read_restricted_to(users=("user-a",))

    first_acl = access.page_acl(first)
    second_acl = access.page_acl(second)

    assert [[p.key for p in level] for level in first_acl.levels] == [["group:id-group-b"]]
    assert [[p.key for p in level] for level in second_acl.levels] == [
        ["user:user-a"],
        ["group:id-group-b"],
    ]
    assert client.calls == [("content/77", {"expand": READ_RESTRICTIONS_EXPAND})]


def test_missing_truncated_or_unidentified_restrictions_fail_closed():
    access = resolver()
    no_block = light_content("1", "x")
    del no_block["restrictions"]
    no_ancestors = light_content("1", "x")
    del no_ancestors["ancestors"]
    truncated = restricted_page(users=("user-a",))
    truncated["restrictions"]["read"]["restrictions"]["user"]["_links"] = {"next": "/next"}
    full_page = restricted_page(users=("user-a",))
    full_page["restrictions"]["read"]["restrictions"]["user"]["limit"] = 1
    anonymous = restricted_page(users=("user-a",))
    anonymous["restrictions"]["read"]["restrictions"]["user"]["results"] = [{"type": "known"}]

    for content in (no_block, no_ancestors, truncated, full_page, anonymous):
        assert access.page_acl(content).status == "unknown"
    assert read_restriction_principals({}) is None


def test_unreadable_or_malformed_ancestor_fails_closed():
    class DenyingClient(FakeConfluenceClient):
        def get_json(self, endpoint, *, params=None):
            if endpoint == "content/77":
                raise AuthorizationError("forbidden", status_code=403)
            return super().get_json(endpoint, params=params)

    page = light_content("1", "x")
    page["ancestors"] = [{"id": "77", "title": "Parent"}]
    bad_id = light_content("1", "x")
    bad_id["ancestors"] = [{"id": "77/../1", "title": "Parent"}]

    assert resolver(DenyingClient()).page_acl(page).status == "unknown"
    assert resolver().page_acl(bad_id).status == "unknown"


def test_discover_withholds_restricted_and_unresolved_pages():
    client = FakeConfluenceClient()
    unresolved = light_content("3", "Unknown")
    del unresolved["restrictions"]
    client.add(
        "content/search",
        {
            "results": [
                light_content("1", "Open"),
                restricted_page("2", users=("user-a",)),
                unresolved,
            ],
            "_links": {},
        },
    )
    connector = ConfluenceConnector(cloud_config(page_size=10), client=client)

    records = list(connector.discover())

    assert [record.metadata["content_id"] for record in records] == ["1"]
    assert [(skip.path, skip.reason) for skip in connector.skipped] == [
        ("2", SKIP_REASON_ACCESS_RESTRICTED),
        ("3", SKIP_REASON_ACCESS_UNRESOLVED),
    ]


def test_explicit_ids_are_gated_and_limit_counts_admitted_pages():
    client = FakeConfluenceClient()
    client.add("content/2", restricted_page("2", users=("user-a",)))
    client.add("content/1", light_content("1", "Open"))
    connector = ConfluenceConnector(cloud_config(), client=client)

    records = list(connector.discover(ConnectorQuery(limit=1, filters={"content_ids": ["2", "1"]})))

    assert [record.metadata["content_id"] for record in records] == ["1"]
    assert [skip.path for skip in connector.skipped] == ["2"]


def test_describe_rejects_restricted_page_under_skip_policy():
    client = FakeConfluenceClient()
    client.add("content/2", restricted_page("2", users=("user-a",)))
    connector = ConfluenceConnector(cloud_config(), client=client)

    with pytest.raises(SourceRestrictedError, match="2"):
        connector.describe(page_record("2"))


def test_load_rejects_restricted_page_under_skip_policy():
    client = FakeConfluenceClient()
    page = full_content("1")
    page["restrictions"] = read_restricted_to(users=("user-a",))
    client.add("content/1", page)
    connector = ConfluenceConnector(cloud_config(), client=client)

    with pytest.raises(SourceRestrictedError, match="1"):
        connector.load(page_record("1"))


def test_include_policy_loads_restricted_page_with_acl_metadata():
    client = FakeConfluenceClient()
    page = full_content("1")
    page["restrictions"] = read_restricted_to(groups=("group-a",))
    client.add("content/1", page)
    connector = ConfluenceConnector(dc_config(restricted_content="include"), client=client)

    document = connector.load(page_record("1"))

    acl = document.metadata[SOURCE_ACL_METADATA_KEY]
    assert acl["status"] == "restricted"
    assert acl["levels"][0][0]["id"] == "id-group-a"


def test_search_requests_read_restrictions():
    client = FakeConfluenceClient()
    client.add("content/search", {"results": [light_content("1", "Open")], "_links": {}})
    connector = ConfluenceConnector(cloud_config(), client=client)

    list(connector.discover())

    search_params = [params for endpoint, params in client.calls if endpoint == "content/search"]
    assert search_params
    assert READ_RESTRICTIONS_EXPAND in search_params[0]["expand"]


def test_include_policy_carries_acl_onto_attachment_records_and_documents():
    client = FakeConfluenceClient()
    page = full_content("1")
    page["restrictions"] = read_restricted_to(groups=("group-a",))
    client.add("content/1", page)
    client.add(
        "content/1/child/attachment",
        {
            "results": [
                {
                    "id": "a1",
                    "title": "notes.md",
                    "metadata": {"mediaType": "text/markdown"},
                    "extensions": {"fileSize": 5},
                    "_links": {"download": "/download/attachments/1/notes.md"},
                    "version": {"number": 1},
                }
            ],
            "size": 1,
        },
    )
    client.downloads[f"{CLOUD_BASE}/download/attachments/1/notes.md"] = b"notes"
    connector = ConfluenceConnector(
        cloud_config(include_attachments=True, restricted_content="include"), client=client
    )

    descriptor = connector.describe(page_record("1"))

    assert descriptor.source.metadata[SOURCE_ACL_METADATA_KEY]["status"] == "restricted"
    (attachment_record,) = descriptor.bound_records
    assert attachment_record.metadata[SOURCE_ACL_METADATA_KEY]["status"] == "restricted"
    attachment = connector.load(attachment_record)
    assert attachment.metadata[SOURCE_ACL_METADATA_KEY]["levels"][0][0]["id"] == "id-group-a"


def _child_of_parent_restricted_between_scans() -> FakeConfluenceClient:
    """A child page whose parent gains a read restriction between two scans."""

    client = FakeConfluenceClient()
    child = light_content("1", "Child")
    child["ancestors"] = [{"id": "77", "title": "Parent"}]
    search = {"results": [child], "_links": {}}
    client.add("content/search", search, search)
    client.add(
        "content/77",
        {"id": "77", "restrictions": unrestricted_read()},
        {"id": "77", "restrictions": read_restricted_to(groups=("group-b",))},
    )
    return client


def test_new_discover_scan_rechecks_a_parent_restricted_after_first_sight():
    connector = ConfluenceConnector(
        cloud_config(page_size=10), client=_child_of_parent_restricted_between_scans()
    )

    assert [record.metadata["content_id"] for record in connector.discover()] == ["1"]
    assert list(connector.discover()) == []
    assert [(skip.path, skip.reason) for skip in connector.skipped] == [
        ("1", SKIP_REASON_ACCESS_RESTRICTED)
    ]


def test_new_paged_scan_rechecks_a_parent_restricted_after_first_sight():
    connector = ConfluenceConnector(
        cloud_config(page_size=10), client=_child_of_parent_restricted_between_scans()
    )

    first = connector.discover_page(None, cursor=None, page_size=10)
    second = connector.discover_page(None, cursor=None, page_size=10)

    assert [record.metadata["content_id"] for record in first.records] == ["1"]
    assert second.records == ()


def test_ancestor_fetch_failure_is_not_cached():
    class FlakyClient(FakeConfluenceClient):
        failures = 1

        def get_json(self, endpoint, *, params=None):
            if endpoint == "content/77" and self.failures:
                self.failures -= 1
                raise AuthorizationError("forbidden", status_code=403)
            return super().get_json(endpoint, params=params)

    client = FlakyClient()
    client.add("content/77", {"id": "77", "restrictions": unrestricted_read()})
    access = resolver(client)
    page = light_content("1", "Child")
    page["ancestors"] = [{"id": "77", "title": "Parent"}]

    assert access.page_acl(page).status == "unknown"
    assert access.page_acl(page).status == "inherited"


def test_explicit_id_paged_replay_reuses_ancestors_within_one_scan():
    client = FakeConfluenceClient()
    pages = []
    for content_id in ("1", "2"):
        page = light_content(content_id, f"Child {content_id}")
        page["ancestors"] = [{"id": "77", "title": "Parent"}]
        pages.append(page)
    client.add("content/1", pages[0], pages[0])
    client.add("content/2", pages[1])
    client.add("content/77", {"id": "77", "restrictions": unrestricted_read()})
    connector = ConfluenceConnector(cloud_config(), client=client)
    query = ConnectorQuery(filters={"content_ids": ["1", "2"]})

    first = connector.discover_page(query, cursor=None, page_size=1)
    second = connector.discover_page(query, cursor=first.next_cursor, page_size=1)

    assert [r.metadata["content_id"] for r in (*first.records, *second.records)] == ["1", "2"]
    assert [endpoint for endpoint, _ in client.calls].count("content/77") == 1
