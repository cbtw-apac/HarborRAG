"""SharePoint item permissions are compared with the library and fail closed."""

from __future__ import annotations

import pytest
import responses
from sharepoint_test_helpers import (
    FakeGraphClient,
    add_site_and_default_drive,
    config,
    file_item,
    inherited_item_permissions,
    library_permissions,
)

from harborrag_adapters.connectors import SharePointConnector, SharePointSiteConfig
from harborrag_adapters.connectors.acl import SOURCE_ACL_METADATA_KEY
from harborrag_adapters.connectors.exceptions import (
    AuthenticationError,
    AuthorizationError,
    FetchError,
    SourceRestrictedError,
)
from harborrag_adapters.connectors.schemas import (
    SKIP_REASON_ACCESS_RESTRICTED,
    SKIP_REASON_ACCESS_UNRESOLVED,
)
from harborrag_adapters.connectors.sharepoint.permissions import (
    SharePointPermissionResolver,
    classify_item_permissions,
)
from harborrag_core.domain.source import SourceRecord

pytestmark = [pytest.mark.unit, pytest.mark.graybox]

MEMBERS = {
    "id": "perm-members",
    "roles": ["write"],
    "grantedToV2": {"siteGroup": {"id": "3", "displayName": "Members"}},
}
VISITORS = {
    "id": "perm-visitors",
    "roles": ["read"],
    "grantedToV2": {"siteGroup": {"id": "4", "displayName": "Visitors"}},
}
ADA = {
    "id": "perm-ada",
    "roles": ["read"],
    "grantedToV2": {"user": {"id": "aad-ada", "displayName": "Ada"}},
}
ROOT_IDS = frozenset({"perm-members", "perm-visitors"})


def inherited(permission: dict, source: str = "root") -> dict:
    return {**permission, "inheritedFrom": {"driveId": "drive1", "id": source}}


def test_item_with_the_library_grants_is_inherited():
    acl = classify_item_permissions([inherited(MEMBERS), inherited(VISITORS)], ROOT_IDS)

    assert acl.status == "inherited"


def test_sharing_links_do_not_change_the_classification():
    link = {"id": "link-1", "roles": ["read"], "link": {"scope": "organization", "type": "view"}}

    acl = classify_item_permissions([inherited(MEMBERS), inherited(VISITORS), link], ROOT_IDS)

    assert acl.status == "inherited"


def test_unique_permissions_are_restricted_with_their_grantees():
    acl = classify_item_permissions([MEMBERS, ADA], ROOT_IDS)

    assert acl.status == "restricted"
    assert acl.reason == "sharepoint_item_permissions"
    assert [principal.key for principal in acl.levels[0]] == ["site_group:3", "user:aad-ada"]


def test_folder_that_dropped_a_library_grant_restricts_its_children():
    acl = classify_item_permissions([inherited(MEMBERS, source="folder-9")], ROOT_IDS)

    assert acl.status == "restricted"


def test_empty_or_unidentifiable_permissions_fail_closed():
    nobody = {"id": "perm-x", "roles": ["read"], "grantedToV2": {}}

    assert classify_item_permissions([], ROOT_IDS).status == "unknown"
    assert classify_item_permissions([inherited(MEMBERS)], frozenset()).status == "unknown"
    assert classify_item_permissions([nobody], ROOT_IDS).status == "unknown"


def test_resolver_follows_pagination_and_caches_the_library_root():
    client = FakeGraphClient()
    client.add(
        "drives/drive1/root/permissions",
        {"value": [MEMBERS], "@odata.nextLink": "https://graph.microsoft.com/v1.0/rootperms2"},
    )
    client.add("https://graph.microsoft.com/v1.0/rootperms2", {"value": [VISITORS]})
    both = {"value": [inherited(MEMBERS), inherited(VISITORS)]}
    client.add("drives/drive1/items/f1/permissions", both)
    client.add("drives/drive1/items/f2/permissions", both)
    resolver = SharePointPermissionResolver(client)

    assert resolver.item_acl("drive1", "f1").status == "inherited"
    assert resolver.item_acl("drive1", "f2").status == "inherited"
    endpoints = [endpoint for endpoint, _ in client.calls]
    assert endpoints.count("drives/drive1/root/permissions") == 1


def test_resolver_caches_items_by_version_only():
    client = FakeGraphClient()
    resolver = SharePointPermissionResolver(client)

    resolver.item_acl("drive1", "f1", version="ctag-1")
    resolver.item_acl("drive1", "f1", version="ctag-1")
    resolver.item_acl("drive1", "f1")

    endpoints = [endpoint for endpoint, _ in client.calls]
    assert endpoints.count("drives/drive1/items/f1/permissions") == 2


def test_permission_endpoint_denial_fails_closed():
    class DenyingClient(FakeGraphClient):
        def get_json(self, endpoint, *, params=None):
            if endpoint.endswith("/items/f1/permissions"):
                raise AuthorizationError("Graph denied", status_code=403)
            return super().get_json(endpoint, params=params)

    assert SharePointPermissionResolver(DenyingClient()).item_acl("drive1", "f1").status == (
        "unknown"
    )


def test_discover_withholds_items_with_unique_permissions():
    client = FakeGraphClient()
    add_site_and_default_drive(client)
    client.add(
        "drives/drive1/root/children",
        {"value": [file_item("open", "Open.docx"), file_item("secret", "Secret.docx")]},
    )
    client.add("drives/drive1/items/secret/permissions", {"value": [ADA]})
    connector = SharePointConnector(config(), client=client)

    records = list(connector.discover())

    assert [record.metadata["item_id"] for record in records] == ["open"]
    assert [(skip.path, skip.reason) for skip in connector.skipped] == [
        ("Docs/Secret.docx", SKIP_REASON_ACCESS_RESTRICTED)
    ]


def test_include_policy_labels_restricted_items():
    client = FakeGraphClient()
    add_site_and_default_drive(client)
    client.add("drives/drive1/root/children", {"value": [file_item("secret", "Secret.docx")]})
    client.add("drives/drive1/items/secret/permissions", {"value": [ADA]})
    connector = SharePointConnector(config(restricted_content="include"), client=client)

    [record] = list(connector.discover())

    assert record.metadata[SOURCE_ACL_METADATA_KEY]["levels"][0][0]["id"] == "aad-ada"


def test_load_rejects_restricted_item_before_download():
    client = FakeGraphClient()
    client.add("drives/drive1/items/secret", file_item("secret", "Secret.docx"))
    client.add("drives/drive1/items/secret/permissions", {"value": [ADA]})
    connector = SharePointConnector(
        SharePointSiteConfig(
            site_id="site1", drive_id="drive1", access_token="token", requests_per_minute=6000
        ),
        client=client,
    )

    with pytest.raises(SourceRestrictedError, match="secret"):
        connector.load(
            SourceRecord(
                "sharepoint://site1/drive1/secret",
                "application/octet-stream",
                "secret",
                metadata={"drive_id": "drive1", "item_id": "secret"},
            )
        )
    assert client.byte_calls == []


def test_invalid_permission_value_list_fails_closed():
    client = FakeGraphClient()
    client.add("drives/drive1/items/f1/permissions", {"value": "not-a-list"})

    assert SharePointPermissionResolver(client).item_acl("drive1", "f1").status == "unknown"


def test_library_root_fetch_failure_is_unknown_and_not_cached():
    class FlakyRootClient(FakeGraphClient):
        def get_json(self, endpoint, *, params=None):
            if endpoint.endswith("/root/permissions") and self.root_failures:
                self.root_failures -= 1
                self.calls.append((endpoint, params))
                raise FetchError("Graph unavailable", status_code=503)
            return super().get_json(endpoint, params=params)

    client = FlakyRootClient()
    client.root_failures = 1
    resolver = SharePointPermissionResolver(client)

    assert resolver.item_acl("drive1", "f1", version="ctag-1").status == "unknown"
    assert resolver.item_acl("drive1", "f1", version="ctag-1").status == "inherited"
    endpoints = [endpoint for endpoint, _ in client.calls]
    assert endpoints.count("drives/drive1/root/permissions") == 2


GRAPH = "https://graph.microsoft.com/v1.0"


def _http_connector() -> SharePointConnector:
    return SharePointConnector(
        SharePointSiteConfig(
            site_id="site1",
            drive_id="drive1",
            access_token="token",
            requests_per_minute=6000,
            max_retries=0,
        )
    )


def _register_children(*items: dict) -> None:
    responses.add(
        responses.GET, f"{GRAPH}/drives/drive1/root/children", json={"value": list(items)}
    )


@responses.activate
def test_real_client_item_403_withholds_that_item_and_continues():
    _register_children(file_item("locked", "Locked.docx"), file_item("open", "Open.docx"))
    responses.add(
        responses.GET, f"{GRAPH}/drives/drive1/root/permissions", json=library_permissions()
    )
    responses.add(
        responses.GET,
        f"{GRAPH}/drives/drive1/items/locked/permissions",
        json={"error": {"code": "accessDenied"}},
        status=403,
    )
    responses.add(
        responses.GET,
        f"{GRAPH}/drives/drive1/items/open/permissions",
        json=inherited_item_permissions(),
    )
    connector = _http_connector()

    records = list(connector.discover())

    assert [record.metadata["item_id"] for record in records] == ["open"]
    assert [(skip.path, skip.reason) for skip in connector.skipped] == [
        ("Docs/Locked.docx", SKIP_REASON_ACCESS_UNRESOLVED)
    ]


@responses.activate
def test_real_client_root_403_makes_every_item_unknown():
    _register_children(file_item("a", "A.docx"), file_item("b", "B.docx"))
    responses.add(
        responses.GET,
        f"{GRAPH}/drives/drive1/root/permissions",
        json={"error": {"code": "accessDenied"}},
        status=403,
    )
    for item_id in ("a", "b"):
        responses.add(
            responses.GET,
            f"{GRAPH}/drives/drive1/items/{item_id}/permissions",
            json=inherited_item_permissions(),
        )
    connector = _http_connector()

    assert list(connector.discover()) == []
    assert [skip.reason for skip in connector.skipped] == [SKIP_REASON_ACCESS_UNRESOLVED] * 2


@responses.activate
def test_real_client_401_on_permissions_aborts_discovery():
    _register_children(file_item("a", "A.docx"))
    responses.add(
        responses.GET,
        f"{GRAPH}/drives/drive1/items/a/permissions",
        json={"error": {"code": "InvalidAuthenticationToken"}},
        status=401,
    )
    connector = _http_connector()

    with pytest.raises(AuthenticationError) as error:
        list(connector.discover())
    assert error.value.status_code == 401


def _scan_connector(client: FakeGraphClient) -> SharePointConnector:
    return SharePointConnector(
        SharePointSiteConfig(
            site_id="site1", drive_id="drive1", access_token="token", requests_per_minute=6000
        ),
        client=client,
    )


def _narrowed_between_scans() -> FakeGraphClient:
    """One file whose grants narrow between two scans while its ``cTag`` stays put."""

    client = FakeGraphClient()
    listing = {"value": [file_item("doc", "Doc.docx")]}
    client.add("drives/drive1/root/children", listing, listing)
    client.add("drives/drive1/root/permissions", library_permissions(), library_permissions())
    client.add(
        "drives/drive1/items/doc/permissions", inherited_item_permissions(), {"value": [ADA]}
    )
    return client


def test_new_discover_scan_reclassifies_item_narrowed_without_ctag_change():
    connector = _scan_connector(_narrowed_between_scans())

    assert [record.metadata["item_id"] for record in connector.discover()] == ["doc"]
    assert list(connector.discover()) == []
    assert [skip.reason for skip in connector.skipped] == [SKIP_REASON_ACCESS_RESTRICTED]


def test_new_paged_scan_reclassifies_item_narrowed_without_ctag_change():
    connector = _scan_connector(_narrowed_between_scans())

    first = connector.discover_page(None, cursor=None, page_size=5)
    second = connector.discover_page(None, cursor=None, page_size=5)

    assert [record.metadata["item_id"] for record in first.records] == ["doc"]
    assert second.records == ()
    assert [skip.reason for skip in connector.skipped] == [SKIP_REASON_ACCESS_RESTRICTED]


def test_paged_replay_within_one_scan_reuses_cached_item_permissions():
    client = FakeGraphClient()
    listing = {"value": [file_item("a", "A.docx"), file_item("b", "B.docx")]}
    client.add("drives/drive1/root/children", listing, listing)
    connector = _scan_connector(client)

    first = connector.discover_page(None, cursor=None, page_size=1)
    second = connector.discover_page(None, cursor=first.next_cursor, page_size=1)

    assert [record.metadata["item_id"] for record in (*first.records, *second.records)] == [
        "a",
        "b",
    ]
    endpoints = [endpoint for endpoint, _ in client.calls]
    assert endpoints.count("drives/drive1/items/a/permissions") == 1
    assert endpoints.count("drives/drive1/root/permissions") == 1


def test_cache_reset_between_store_and_read_does_not_raise():
    class ResetAfterStore(dict):
        def __setitem__(self, key, value):
            super().__setitem__(key, value)
            self.clear()

    client = FakeGraphClient()
    client.add("drives/drive1/root/permissions", {"value": [MEMBERS]})
    client.add("drives/drive1/items/f1/permissions", {"value": [inherited(MEMBERS)]})
    resolver = SharePointPermissionResolver(client)
    resolver._root_grants = ResetAfterStore()
    resolver._items = ResetAfterStore()

    assert resolver.item_acl("drive1", "f1", version="ctag-1").status == "inherited"
