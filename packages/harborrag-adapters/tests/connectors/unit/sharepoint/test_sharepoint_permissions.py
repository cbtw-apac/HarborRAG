"""SharePoint item permissions are compared with the library and fail closed."""

from __future__ import annotations

import pytest
import responses
from sharepoint_test_helpers import (
    SITE_URL,
    FakeGraphClient,
    add_site_and_default_drive,
    config,
    file_item,
    inherited_item_permissions,
    library_permissions,
)

from harborrag_adapters.connectors import SharePointConnector, SharePointSiteConfig
from harborrag_adapters.connectors.acl import SOURCE_ACL_METADATA_KEY, SourceAcl, SourcePrincipal
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
    entra_realm,
    sharepoint_realm,
    site_realm,
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
REALM = entra_realm("t1")
SCOPE = "sharepoint-drive:drive1"
SITE_KEY = "contoso.sharepoint.com,site-guid,web-guid"


def classify(
    permissions: list[dict],
    root_ids: frozenset[str] = ROOT_IDS,
    *,
    site_key: str = SITE_KEY,
) -> SourceAcl:
    return classify_item_permissions(
        permissions, root_ids, realm=REALM, scope=SCOPE, site_key=site_key
    )


def make_resolver(client: FakeGraphClient) -> SharePointPermissionResolver:
    return SharePointPermissionResolver(client, realm=REALM)


def inherited(permission: dict, source: str = "root") -> dict:
    return {**permission, "inheritedFrom": {"driveId": "drive1", "id": source}}


SITE_USER = {
    "id": "perm-site-user",
    "roles": ["read"],
    "grantedToV2": {"siteUser": {"id": "7", "displayName": "Ada (site)"}},
}
ENTRA_GROUP = {
    "id": "perm-entra-group",
    "roles": ["read"],
    "grantedToV2": {"group": {"id": "9f1c-entra-group", "displayName": "Engineers"}},
}


def test_site_local_principals_are_qualified_with_the_site_they_were_read_from():
    acl = classify([MEMBERS, SITE_USER], site_key="siteA")

    assert [(p.kind, p.id) for p in acl.levels[0]] == [
        ("site_group", "siteA/3"),
        ("site_user", "siteA/7"),
    ]
    assert [p.key for p in acl.levels[0]] == [
        "entra:t1|site_group:siteA/3",
        "entra:t1|site_user:siteA/7",
    ]
    assert [p.display_name for p in acl.levels[0]] == ["Members", "Ada (site)"]


def test_the_same_site_group_id_on_two_sites_is_two_principals():
    on_a = classify([MEMBERS], site_key="siteA").levels[0][0]
    on_b = classify([MEMBERS], site_key="siteB").levels[0][0]

    assert on_a != on_b
    assert on_a.key != on_b.key
    assert (on_a.realm, on_a.kind) == (on_b.realm, on_b.kind)
    assert len({on_a, on_b}) == 2


def test_entra_principals_are_tenant_wide_and_not_site_qualified():
    on_a = classify([ENTRA_GROUP, ADA], site_key="siteA").levels[0]
    on_b = classify([ENTRA_GROUP, ADA], site_key="siteB").levels[0]

    assert on_a == on_b
    assert [(p.kind, p.id) for p in on_a] == [("group", "9f1c-entra-group"), ("user", "aad-ada")]


@pytest.mark.parametrize("site_key", ["", "   "])
def test_a_blank_site_key_is_rejected_instead_of_leaving_site_ids_unqualified(site_key: str):
    with pytest.raises(ValueError, match="site"):
        classify([MEMBERS], site_key=site_key)


def test_resolver_never_serves_a_cached_acl_read_for_another_site():
    client = FakeGraphClient()
    client.add("drives/drive1/items/f1/permissions", {"value": [MEMBERS]}, {"value": [MEMBERS]})
    resolver = make_resolver(client)

    on_a = resolver.item_acl("drive1", "f1", site_key="siteA", version="ctag-1")
    on_b = resolver.item_acl("drive1", "f1", site_key="siteB", version="ctag-1")
    on_a_again = resolver.item_acl("drive1", "f1", site_key="siteA", version="ctag-1")

    assert on_a.levels[0][0].id == "siteA/3"
    assert on_b.levels[0][0].id == "siteB/3"
    assert on_a_again is on_a


def test_acls_carry_the_realm_and_the_drive_scope():
    inherited_acl = classify([inherited(MEMBERS), inherited(VISITORS)])
    restricted_acl = classify([MEMBERS, ADA])

    assert inherited_acl == SourceAcl.inherited(realm=REALM, scope=SCOPE)
    assert (restricted_acl.realm, restricted_acl.scope) == ("entra:t1", "sharepoint-drive:drive1")
    assert restricted_acl.levels == (
        (
            SourcePrincipal(realm=REALM, kind="site_group", id=f"{SITE_KEY}/3"),
            SourcePrincipal(realm=REALM, kind="user", id="aad-ada"),
        ),
    )
    assert restricted_acl.levels[0][1].display_name == "Ada"


def test_unknown_classification_keeps_the_realm_and_scope():
    acl = classify([])

    assert (acl.status, acl.realm, acl.scope) == ("unknown", REALM, SCOPE)


def test_entra_realm_names_the_tenant_verbatim():
    assert entra_realm("72f988bf-86f1-41af-91ab-2d7cd011db47") == (
        "entra:72f988bf-86f1-41af-91ab-2d7cd011db47"
    )


@pytest.mark.parametrize("value", ["", "   "])
def test_entra_realm_rejects_a_blank_tenant(value: str):
    with pytest.raises(ValueError, match="tenant"):
        entra_realm(value)


def test_sharepoint_realm_uses_the_lowercased_host():
    assert sharepoint_realm("Contoso.SharePoint.com") == "sharepoint:contoso.sharepoint.com"


@pytest.mark.parametrize("value", ["", "   "])
def test_sharepoint_realm_rejects_a_blank_host(value: str):
    with pytest.raises(ValueError, match="host"):
        sharepoint_realm(value)


def test_site_realm_is_the_entra_tenant_when_one_is_configured(monkeypatch):
    monkeypatch.delenv("MICROSOFT_TENANT_ID", raising=False)

    assert site_realm(config(tenant_id="t1")) == "entra:t1"


def test_site_realm_reads_the_tenant_from_the_environment_fallback(monkeypatch):
    monkeypatch.setenv("MICROSOFT_TENANT_ID", "env-tenant")

    assert site_realm(config()) == "entra:env-tenant"


def test_site_realm_is_a_sharepoint_host_placeholder_without_a_tenant(monkeypatch):
    monkeypatch.delenv("MICROSOFT_TENANT_ID", raising=False)

    assert site_realm(config(site_url=SITE_URL)) == "sharepoint:contoso.sharepoint.com"
    assert site_realm(
        config(site_url=None, hostname="Contoso.SharePoint.com", site_path="sites/Eng")
    ) == ("sharepoint:contoso.sharepoint.com")


def test_site_realm_reads_the_host_from_a_graph_site_id(monkeypatch):
    monkeypatch.delenv("MICROSOFT_TENANT_ID", raising=False)
    token_only = {"access_token": "token", "site_url": None}

    graph_id = "Contoso.sharepoint.com,5f0b8b0e-0000-0000-0000-000000000001,9a1c-2"
    assert site_realm(SharePointSiteConfig(site_id=graph_id, **token_only)) == (
        "sharepoint:contoso.sharepoint.com"
    )
    assert site_realm(SharePointSiteConfig(site_id="site1", **token_only)) == "sharepoint:site1"


def test_a_blank_tenant_is_treated_as_unconfigured(monkeypatch):
    monkeypatch.delenv("MICROSOFT_TENANT_ID", raising=False)

    assert site_realm(config(tenant_id="  ")) == "sharepoint:contoso.sharepoint.com"


def test_the_site_host_wins_over_the_site_id_host(monkeypatch):
    monkeypatch.delenv("MICROSOFT_TENANT_ID", raising=False)

    value = config(site_id="other.sharepoint.com,a,b")

    assert site_realm(value) == "sharepoint:contoso.sharepoint.com"


def test_resolver_scopes_each_item_acl_to_the_drive_it_was_read_from():
    client = FakeGraphClient()
    client.add("drives/drive7/items/f1/permissions", {"value": [ADA]})
    resolver = make_resolver(client)

    acl = resolver.item_acl("drive7", "f1", site_key=SITE_KEY)

    assert (acl.status, acl.realm, acl.scope) == ("restricted", REALM, "sharepoint-drive:drive7")


def test_unavailable_permissions_stay_unknown_but_name_the_realm_and_scope():
    class FailingClient(FakeGraphClient):
        def get_json(self, endpoint, *, params=None):
            raise FetchError("Graph unavailable", status_code=503)

    acl = make_resolver(FailingClient()).item_acl("drive1", "f1", site_key=SITE_KEY)

    assert (acl.status, acl.reason) == ("unknown", "item_permissions_unavailable")
    assert (acl.realm, acl.scope) == (REALM, SCOPE)


def test_item_with_the_library_grants_is_inherited():
    acl = classify([inherited(MEMBERS), inherited(VISITORS)])

    assert acl.status == "inherited"


def test_sharing_links_do_not_change_the_classification():
    link = {"id": "link-1", "roles": ["read"], "link": {"scope": "organization", "type": "view"}}

    acl = classify([inherited(MEMBERS), inherited(VISITORS), link])

    assert acl.status == "inherited"


def test_unique_permissions_are_restricted_with_their_grantees():
    acl = classify([MEMBERS, ADA])

    assert acl.status == "restricted"
    assert acl.reason == "sharepoint_item_permissions"
    assert [principal.key for principal in acl.levels[0]] == [
        f"entra:t1|site_group:{SITE_KEY}/3",
        "entra:t1|user:aad-ada",
    ]


def test_folder_that_dropped_a_library_grant_restricts_its_children():
    acl = classify([inherited(MEMBERS, source="folder-9")])

    assert acl.status == "restricted"


def test_empty_or_unidentifiable_permissions_fail_closed():
    nobody = {"id": "perm-x", "roles": ["read"], "grantedToV2": {}}

    assert classify([]).status == "unknown"
    assert classify([inherited(MEMBERS)], frozenset()).status == "unknown"
    assert classify([nobody]).status == "unknown"


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
    resolver = make_resolver(client)

    assert resolver.item_acl("drive1", "f1", site_key=SITE_KEY).status == "inherited"
    assert resolver.item_acl("drive1", "f2", site_key=SITE_KEY).status == "inherited"
    endpoints = [endpoint for endpoint, _ in client.calls]
    assert endpoints.count("drives/drive1/root/permissions") == 1


def test_resolver_caches_items_by_version_only():
    client = FakeGraphClient()
    resolver = make_resolver(client)

    resolver.item_acl("drive1", "f1", site_key=SITE_KEY, version="ctag-1")
    resolver.item_acl("drive1", "f1", site_key=SITE_KEY, version="ctag-1")
    resolver.item_acl("drive1", "f1", site_key=SITE_KEY)

    endpoints = [endpoint for endpoint, _ in client.calls]
    assert endpoints.count("drives/drive1/items/f1/permissions") == 2


def test_permission_endpoint_denial_fails_closed():
    class DenyingClient(FakeGraphClient):
        def get_json(self, endpoint, *, params=None):
            if endpoint.endswith("/items/f1/permissions"):
                raise AuthorizationError("Graph denied", status_code=403)
            return super().get_json(endpoint, params=params)

    assert make_resolver(DenyingClient()).item_acl("drive1", "f1", site_key=SITE_KEY).status == (
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


def _open_item_record(**overrides):
    client = FakeGraphClient()
    add_site_and_default_drive(client)
    client.add("drives/drive1/root/children", {"value": [file_item("open", "Open.docx")]})
    [record] = list(SharePointConnector(config(**overrides), client=client).discover())
    return record


def test_discovered_items_record_their_inherited_acl_with_the_connector_realm(monkeypatch):
    monkeypatch.delenv("MICROSOFT_TENANT_ID", raising=False)

    tenant_record = _open_item_record(tenant_id="t1")
    token_record = _open_item_record()

    assert (
        tenant_record.metadata[SOURCE_ACL_METADATA_KEY]
        == SourceAcl.inherited(realm="entra:t1", scope="sharepoint-drive:drive1").to_dict()
    )
    assert (
        token_record.metadata[SOURCE_ACL_METADATA_KEY]
        == SourceAcl.inherited(
            realm="sharepoint:contoso.sharepoint.com", scope="sharepoint-drive:drive1"
        ).to_dict()
    )


def test_discovered_site_group_principals_are_qualified_with_the_resolved_site_id():
    client = FakeGraphClient()
    add_site_and_default_drive(client)
    client.add("drives/drive1/root/children", {"value": [file_item("secret", "Secret.docx")]})
    client.add("drives/drive1/items/secret/permissions", {"value": [MEMBERS, ADA]})
    connector = SharePointConnector(
        config(tenant_id="t1", restricted_content="include"), client=client
    )

    [record] = list(connector.discover())

    [level] = record.metadata[SOURCE_ACL_METADATA_KEY]["levels"]
    assert [(p["kind"], p["id"]) for p in level] == [
        ("site_group", "site1/3"),
        ("user", "aad-ada"),
    ]


def test_loaded_site_group_principals_are_qualified_with_the_configured_site_id():
    client = FakeGraphClient()
    client.add("drives/drive1/items/secret", file_item("secret", "Secret.docx"))
    client.add("drives/drive1/items/secret/permissions", {"value": [MEMBERS]})
    client.downloads["drives/drive1/items/secret/content"] = b"bytes"
    connector = SharePointConnector(
        SharePointSiteConfig(
            site_id="contoso.sharepoint.com,site-guid,web-guid",
            drive_id="drive1",
            access_token="token",
            tenant_id="t1",
            restricted_content="include",
            requests_per_minute=6000,
        ),
        client=client,
    )

    document = connector.load(
        SourceRecord(
            "sharepoint://site/drive1/secret",
            "application/octet-stream",
            "secret",
            metadata={"drive_id": "drive1", "item_id": "secret"},
        )
    )

    [level] = document.metadata[SOURCE_ACL_METADATA_KEY]["levels"]
    assert [(p["kind"], p["id"]) for p in level] == [
        ("site_group", "contoso.sharepoint.com,site-guid,web-guid/3")
    ]


def test_loaded_document_records_the_acl_read_at_load_time(monkeypatch):
    monkeypatch.delenv("MICROSOFT_TENANT_ID", raising=False)
    client = FakeGraphClient()
    client.add("drives/drive1/items/open", file_item("open", "Open.docx"))
    client.downloads["drives/drive1/items/open/content"] = b"bytes"
    connector = SharePointConnector(
        SharePointSiteConfig(
            site_id="site1",
            drive_id="drive1",
            access_token="token",
            tenant_id="t1",
            requests_per_minute=6000,
        ),
        client=client,
    )

    document = connector.load(
        SourceRecord(
            "sharepoint://site1/drive1/open",
            "application/octet-stream",
            "open",
            metadata={"drive_id": "drive1", "item_id": "open"},
        )
    )

    assert (
        document.metadata[SOURCE_ACL_METADATA_KEY]
        == SourceAcl.inherited(realm="entra:t1", scope="sharepoint-drive:drive1").to_dict()
    )


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

    assert make_resolver(client).item_acl("drive1", "f1", site_key=SITE_KEY).status == "unknown"


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
    resolver = make_resolver(client)

    assert (
        resolver.item_acl("drive1", "f1", site_key=SITE_KEY, version="ctag-1").status == "unknown"
    )
    assert (
        resolver.item_acl("drive1", "f1", site_key=SITE_KEY, version="ctag-1").status == "inherited"
    )
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
    resolver = make_resolver(client)
    resolver._root_grants = ResetAfterStore()
    resolver._items = ResetAfterStore()

    assert (
        resolver.item_acl("drive1", "f1", site_key=SITE_KEY, version="ctag-1").status == "inherited"
    )
