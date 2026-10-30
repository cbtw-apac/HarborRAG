"""Unit tests for SharePoint crawl scope, allowlists, skips, and fail-fast rules."""

from __future__ import annotations

import base64
import json
from typing import Any

import pytest
from sharepoint_test_helpers import (
    SITE_URL,
    FakeGraphClient,
    add_site_and_default_drive,
    config,
    drive,
    file_item,
    folder_item,
    site,
)

from harborrag_adapters.connectors import SharePointConnector
from harborrag_adapters.connectors.exceptions import (
    AuthenticationError,
    AuthorizationError,
    FetchError,
    RateLimitError,
)
from harborrag_adapters.connectors.limits import DEFAULT_ALLOWED_EXTENSIONS
from harborrag_adapters.connectors.schemas import ConnectorQuery
from harborrag_adapters.connectors.sharepoint.permissions import reject_broad_permissions
from harborrag_adapters.connectors.sharepoint.skips import SharePointItemSkippedError
from harborrag_core.domain.source import SourceRecord
from harborrag_core.ingestion import (
    SourceItemNotFoundError,
    SourceUnavailableError,
    UnsupportedDocumentError,
)

pytestmark = [pytest.mark.unit, pytest.mark.graybox]


class RaisingGraphClient(FakeGraphClient):
    """Fake client that raises a configured error for chosen endpoints."""

    def __init__(self) -> None:
        super().__init__()
        self.errors: dict[str, Exception] = {}

    def get_json(self, endpoint: str, *, params: dict[str, Any] | None = None) -> dict[str, Any]:
        if endpoint in self.errors:
            self.calls.append((endpoint, params))
            raise self.errors[endpoint]
        return super().get_json(endpoint, params=params)

    def get_bytes(self, endpoint: str) -> bytes:
        if endpoint in self.errors:
            self.byte_calls.append(endpoint)
            raise self.errors[endpoint]
        return super().get_bytes(endpoint)


def _file_record(item_id: str = "file1", drive_id: str = "drive1") -> SourceRecord:
    return SourceRecord(
        f"sharepoint://site1/{drive_id}/{item_id}",
        "application/octet-stream",
        item_id,
        metadata={"drive_id": drive_id, "item_id": item_id},
    )


def _jwt(claims: dict[str, Any]) -> str:
    payload = base64.urlsafe_b64encode(json.dumps(claims).encode()).decode().rstrip("=")
    return f"header.{payload}.signature"


# --- Config (AC10) ------------------------------------------------------------


def test_default_extensions_are_the_shared_document_and_image_set():
    assert config().allowed_extensions == set(DEFAULT_ALLOWED_EXTENSIONS)
    assert config().max_file_size_bytes == 20 * 1024 * 1024


@pytest.mark.parametrize(
    ("overrides", "message"),
    [
        ({"max_file_size_bytes": 0}, "greater than 0"),
        ({"max_file_size_bytes": -1}, "greater than or equal to 0"),
        ({"allowed_extensions": set()}, "at least one extension"),
        ({"allowed_extensions": {".docx", ".exe"}}, "unsupported extensions: .exe"),
        ({"root_path": "Docs/../Secrets"}, "'..' segments"),
        ({"root_path": "Docs\\Secrets"}, "'/'-separated"),
        ({"site_url": "http://contoso.sharepoint.com/sites/x"}, "https://"),
        ({"item_ids": []}, "at least one item"),
        ({"item_ids": ["a"], "root_path": "Docs"}, "separate scopes"),
        ({"crawl_all_drives": True, "drive_name": "Documents"}, "whole site"),
        ({"allowed_sites": []}, "at least one entry"),
        ({"allowed_tenant_ids": ["https://login.example.com"]}, "IDs, not URLs"),
        ({"allowed_path_prefixes": ["Docs/../x"]}, "'..' segments"),
    ],
)
def test_config_rejects_invalid_settings_at_startup(overrides, message):
    with pytest.raises(ValueError, match=message):
        config(**overrides)


def test_config_normalizes_allowlists():
    cfg = config(
        allowed_sites=["https://Contoso.sharepoint.com/sites/Engineering/"],
        allowed_tenant_ids=["TENANT-1"],
        allowed_path_prefixes=["/Docs/Policies/", "/"],
    )

    assert cfg.allowed_sites == {"https://contoso.sharepoint.com/sites/engineering"}
    assert cfg.allowed_tenant_ids == {"tenant-1"}
    assert cfg.allowed_path_prefixes == ["docs/policies", ""]


# --- Crawl scope (AC1, AC2) -----------------------------------------------------


def test_recursive_setting_off_stays_in_the_root_folder():
    client = FakeGraphClient()
    add_site_and_default_drive(client)
    client.add(
        "drives/drive1/root/children",
        {"value": [folder_item(), file_item("file1", "A.docx")]},
    )
    connector = SharePointConnector(config(recursive=False), client=client)

    assert [record.locator for record in connector.discover()] == ["file1"]
    assert all(endpoint != "drives/drive1/items/folder1/children" for endpoint, _ in client.calls)


def test_configured_item_ids_scope_to_single_files():
    client = FakeGraphClient()
    add_site_and_default_drive(client)
    client.add("drives/drive1/items/file2", file_item("file2", "B.docx"))
    connector = SharePointConnector(config(item_ids=["file2"]), client=client)

    assert [record.locator for record in connector.discover()] == ["file2"]


def test_crawl_all_drives_walks_every_document_library():
    client = FakeGraphClient()
    client.add("sites/contoso.sharepoint.com:/sites/Engineering", site())
    client.add(
        "sites/site1/drives",
        {
            "value": [
                drive("drive1", "Documents"),
                drive("drive2", "Policies"),
                {"id": "drive3", "name": "OneDrive", "driveType": "business"},
            ]
        },
    )
    client.add("drives/drive1/root/children", {"value": [file_item("file1", "A.docx")]})
    client.add("drives/drive2/root/children", {"value": [file_item("file2", "B.pdf")]})
    connector = SharePointConnector(config(crawl_all_drives=True), client=client)

    records = list(connector.discover())

    assert [record.id for record in records] == [
        "sharepoint://site1/drive1/file1",
        "sharepoint://site1/drive2/file2",
    ]


def test_crawl_all_drives_loads_a_file_from_any_library_in_scope():
    client = FakeGraphClient()
    client.add("sites/contoso.sharepoint.com:/sites/Engineering", site())
    client.add(
        "sites/site1/drives",
        {"value": [drive("drive1", "Documents"), drive("drive2", "Policies")]},
    )
    client.add("drives/drive2/items/file2", file_item("file2", "B.docx"))
    client.downloads["drives/drive2/items/file2/content"] = b"bytes"
    connector = SharePointConnector(config(crawl_all_drives=True), client=client)

    document = connector.load(_file_record("file2", drive_id="drive2"))

    assert document.content == b"bytes"
    assert document.metadata["drive_id"] == "drive2"


# --- Allowlists (AC3) -------------------------------------------------------------


def test_site_not_in_allowlist_is_never_crawled():
    client = FakeGraphClient()
    add_site_and_default_drive(client)
    connector = SharePointConnector(
        config(allowed_sites=["https://contoso.sharepoint.com/sites/Finance"]),
        client=client,
    )

    assert list(connector.discover()) == []
    (skip,) = connector.skipped
    assert skip.reason == "NOT_IN_ALLOWLIST"
    assert "sites/Engineering" in skip.detail
    assert all("children" not in endpoint for endpoint, _ in client.calls)


def test_site_allowlist_accepts_the_site_id_or_its_guid_part():
    for entry in ("site1", "https://contoso.sharepoint.com/sites/Engineering"):
        client = FakeGraphClient()
        add_site_and_default_drive(client)
        client.add("drives/drive1/root/children", {"value": [file_item("file1", "A.docx")]})
        connector = SharePointConnector(config(allowed_sites=[entry]), client=client)

        assert [record.locator for record in connector.discover()] == ["file1"], entry


def test_tenant_not_in_allowlist_is_never_crawled():
    client = FakeGraphClient()
    connector = SharePointConnector(
        config(tenant_id="tenant-a", allowed_tenant_ids=["tenant-b"]),
        client=client,
    )
    client.add("sites/contoso.sharepoint.com:/sites/Engineering", site())

    assert list(connector.discover()) == []
    assert [(skip.reason, skip.path) for skip in connector.skipped] == [
        ("NOT_IN_ALLOWLIST", "tenant-a")
    ]


def test_tenant_allowlist_fails_closed_without_a_tenant_id():
    client = FakeGraphClient()
    client.add("sites/contoso.sharepoint.com:/sites/Engineering", site())
    connector = SharePointConnector(config(allowed_tenant_ids=["tenant-b"]), client=client)

    assert list(connector.discover()) == []
    assert connector.skipped[0].reason == "NOT_IN_ALLOWLIST"


def test_path_allowlist_filters_files_and_prunes_unrelated_folders():
    client = FakeGraphClient()
    add_site_and_default_drive(client)
    policies = folder_item("policies", "Policies")
    archive = folder_item("archive", "Archive")
    client.add(
        "drives/drive1/root/children",
        {"value": [policies, archive, _at_root(file_item("root-file", "Root.docx"))]},
    )
    client.add(
        "drives/drive1/items/policies/children",
        {"value": [_under("Policies", file_item("policy", "Policy.docx"))]},
    )
    connector = SharePointConnector(
        config(allowed_path_prefixes=["Policies"]),
        client=client,
    )

    records = list(connector.discover())

    assert [record.locator for record in records] == ["policy"]
    assert all(endpoint != "drives/drive1/items/archive/children" for endpoint, _ in client.calls)
    assert [(skip.reason, skip.item_id) for skip in connector.skipped] == [
        ("NOT_IN_ALLOWLIST", "root-file")
    ]


def test_load_refuses_a_file_outside_the_path_allowlist_before_download():
    client = FakeGraphClient()
    add_site_and_default_drive(client)
    client.add("drives/drive1/items/file1", file_item("file1", "Guide.docx"))
    connector = SharePointConnector(config(allowed_path_prefixes=["Policies"]), client=client)

    with pytest.raises(SharePointItemSkippedError) as excinfo:
        connector.load(_file_record())

    assert excinfo.value.skip_reason == "NOT_IN_ALLOWLIST"
    assert client.byte_calls == []


def test_load_refuses_any_record_when_the_site_is_not_allowlisted():
    client = FakeGraphClient()
    add_site_and_default_drive(client)
    connector = SharePointConnector(config(allowed_sites=["other-site"]), client=client)

    with pytest.raises(SharePointItemSkippedError, match="NOT_IN_ALLOWLIST"):
        connector.load(_file_record())
    assert client.byte_calls == []


# --- Skips (AC6, AC7, AC9) -------------------------------------------------------


def test_discovery_reports_unsupported_and_oversized_files_with_item_id_and_url():
    client = FakeGraphClient()
    add_site_and_default_drive(client)
    client.add(
        "drives/drive1/root/children",
        {
            "value": [
                file_item("file1", "A.docx"),
                file_item("zip1", "Bundle.zip"),
                file_item("big1", "Huge.pdf", size=500),
            ]
        },
    )
    connector = SharePointConnector(config(max_file_size_bytes=100), client=client)

    records = list(connector.discover())

    assert [record.locator for record in records] == ["file1"]
    skips = {skip.item_id: skip for skip in connector.skipped}
    assert skips["zip1"].reason == "UNSUPPORTED_EXTENSION"
    assert skips["big1"].reason == "EXCEEDS_SIZE_LIMIT"
    assert skips["zip1"].url.endswith("Bundle.zip")
    assert skips["big1"].to_dict()["item_id"] == "big1"


def test_skipped_resets_between_discoveries():
    client = FakeGraphClient()
    add_site_and_default_drive(client)
    client.add("drives/drive1/root/children", {"value": [file_item("zip1", "Bundle.zip")]})
    client.add("drives/drive1/root/children", {"value": []})
    connector = SharePointConnector(config(), client=client)

    list(connector.discover())
    assert len(connector.skipped) == 1
    list(connector.discover())
    assert connector.skipped == ()


@pytest.mark.parametrize(
    ("error", "reason"),
    [
        (FetchError("gone", status_code=404), "DELETED"),
        (AuthorizationError("forbidden", status_code=403), "ACCESS_DENIED"),
        (RateLimitError("slow down", status_code=429), "TRANSIENT_ERROR_MAX_RETRIES"),
        (FetchError("boom", status_code=503), "TRANSIENT_ERROR_MAX_RETRIES"),
    ],
)
def test_direct_item_failures_are_skipped_and_the_run_continues(error, reason):
    client = RaisingGraphClient()
    add_site_and_default_drive(client)
    client.errors["drives/drive1/items/bad"] = error
    client.add("drives/drive1/items/file1", file_item("file1", "A.docx"))
    connector = SharePointConnector(config(), client=client)

    records = list(connector.discover(ConnectorQuery(filters={"item_ids": ["bad", "file1"]})))

    assert [record.locator for record in records] == ["file1"]
    assert [(skip.reason, skip.item_id) for skip in connector.skipped] == [(reason, "bad")]


def _subfolder_tree(client: RaisingGraphClient) -> None:
    """Root holds a file plus a "Locked" folder walked before an "Open" one."""
    locked = {**folder_item("locked", "Locked"), "webUrl": f"{SITE_URL}/Shared%20Documents/Locked"}
    client.add(
        "drives/drive1/root/children",
        {"value": [file_item("file1", "A.docx"), locked, folder_item("open", "Open")]},
    )
    client.add(
        "drives/drive1/items/open/children",
        {"value": [file_item("file2", "B.docx", parent_id="open")]},
    )


@pytest.mark.parametrize(
    ("error", "reason"),
    [
        (FetchError("gone", status_code=404), "DELETED"),
        (AuthorizationError("forbidden", status_code=403), "ACCESS_DENIED"),
        (RateLimitError("slow down", status_code=429), "TRANSIENT_ERROR_MAX_RETRIES"),
        (FetchError("boom", status_code=503), "TRANSIENT_ERROR_MAX_RETRIES"),
    ],
)
def test_a_failing_subfolder_is_skipped_and_its_siblings_still_crawl(error, reason):
    client = RaisingGraphClient()
    add_site_and_default_drive(client)
    _subfolder_tree(client)
    client.errors["drives/drive1/items/locked/children"] = error
    connector = SharePointConnector(config(), client=client)

    records = list(connector.discover())

    assert [record.locator for record in records] == ["file1", "file2"]
    assert [(skip.reason, skip.item_id, skip.url) for skip in connector.skipped] == [
        (reason, "locked", f"{SITE_URL}/Shared%20Documents/Locked")
    ]


def test_a_subfolder_failing_mid_pagination_keeps_the_pages_already_listed():
    client = RaisingGraphClient()
    add_site_and_default_drive(client)
    client.add("drives/drive1/root/children", {"value": [folder_item("big", "Big")]})
    client.add(
        "drives/drive1/items/big/children",
        {"value": [file_item("file1", "A.docx", parent_id="big")], "@odata.nextLink": "page2"},
    )
    client.errors["page2"] = FetchError("boom", status_code=503)
    connector = SharePointConnector(config(), client=client)

    records = list(connector.discover())

    assert [record.locator for record in records] == ["file1"]
    assert [(skip.reason, skip.item_id) for skip in connector.skipped] == [
        ("TRANSIENT_ERROR_MAX_RETRIES", "big")
    ]


@pytest.mark.parametrize(
    "error",
    [
        AuthorizationError("forbidden", status_code=403),
        FetchError("boom", status_code=503),
    ],
)
def test_a_failing_crawl_root_still_stops_the_run(error):
    client = RaisingGraphClient()
    add_site_and_default_drive(client)
    client.errors["drives/drive1/root:/Docs:/children"] = error
    connector = SharePointConnector(config(root_path="Docs"), client=client)

    with pytest.raises(type(error)):
        list(connector.discover())


def test_authentication_failure_in_a_subfolder_still_stops_the_run():
    client = RaisingGraphClient()
    add_site_and_default_drive(client)
    _subfolder_tree(client)
    client.errors["drives/drive1/items/locked/children"] = AuthenticationError("expired")
    connector = SharePointConnector(config(), client=client)

    with pytest.raises(AuthenticationError):
        list(connector.discover())


@pytest.mark.parametrize(
    ("endpoint", "error", "reason", "error_type"),
    [
        (
            "drives/drive1/items/file1",
            FetchError("gone", status_code=404),
            "DELETED",
            SourceItemNotFoundError,
        ),
        (
            "drives/drive1/items/file1",
            AuthorizationError("forbidden", status_code=403),
            "ACCESS_DENIED",
            UnsupportedDocumentError,
        ),
        (
            "drives/drive1/items/file1/content",
            FetchError("boom", status_code=503),
            "TRANSIENT_ERROR_MAX_RETRIES",
            SourceUnavailableError,
        ),
    ],
)
def test_load_turns_item_failures_into_non_fatal_skips(endpoint, error, reason, error_type):
    client = RaisingGraphClient()
    add_site_and_default_drive(client)
    client.add("drives/drive1/items/file1", file_item("file1", "Guide.docx"))
    client.errors[endpoint] = error
    connector = SharePointConnector(config(), client=client)

    with pytest.raises(error_type) as excinfo:
        connector.load(_file_record())

    assert excinfo.value.skip_reason == reason
    assert excinfo.value.item_id == "file1"
    # A per-item failure, never an auth failure: the engine records one failed item.
    # Only a decline is classified unsupported; a deleted item is missing, and
    # exhausted retries stay retryable.
    assert isinstance(excinfo.value, UnsupportedDocumentError) is (reason == "ACCESS_DENIED")
    assert not isinstance(excinfo.value, AuthenticationError)


def test_load_raw_documents_skips_a_forbidden_item_instead_of_stopping():
    client = RaisingGraphClient()
    add_site_and_default_drive(client)
    client.add(
        "drives/drive1/root/children",
        {"value": [file_item("locked", "Locked.docx"), file_item("file1", "A.docx")]},
    )
    client.errors["drives/drive1/items/locked"] = AuthorizationError("no", status_code=403)
    client.add("drives/drive1/items/file1", file_item("file1", "A.docx"))
    client.downloads["drives/drive1/items/file1/content"] = b"ok"
    connector = SharePointConnector(config(), client=client)

    documents = list(connector.load_raw_documents(on_error="skip"))

    assert [document.content for document in documents] == [b"ok"]


# --- Dedup (AC8) ------------------------------------------------------------------


def test_discover_yields_each_item_once_per_run():
    client = FakeGraphClient()
    add_site_and_default_drive(client)
    client.add("drives/drive1/items/file1", file_item("file1", "A.docx"))
    client.add("drives/drive1/items/file1", file_item("file1", "A.docx"))
    connector = SharePointConnector(config(), client=client)

    records = list(connector.discover(ConnectorQuery(filters={"item_ids": ["file1", "file1"]})))

    assert [record.id for record in records] == ["sharepoint://site1/drive1/file1"]


# --- Authentication and permissions (AC4, AC5) ----------------------------------------


def test_broad_graph_permissions_are_refused():
    with pytest.raises(AuthenticationError, match=r"Sites\.Read\.All"):
        reject_broad_permissions(_jwt({"roles": ["Sites.Selected", "Sites.Read.All"]}))


def test_sites_selected_token_and_opaque_tokens_are_accepted():
    reject_broad_permissions(_jwt({"roles": ["Sites.Selected"]}))
    reject_broad_permissions("opaque-token")


def test_connect_fails_fast_when_the_site_is_not_granted():
    client = RaisingGraphClient()
    client.errors["sites/contoso.sharepoint.com:/sites/Engineering"] = AuthorizationError(
        "forbidden", status_code=403
    )
    connector = SharePointConnector(config(), client=client)

    with pytest.raises(AuthorizationError, match="Sites.Selected"):
        connector.connect()


def test_authentication_failure_stops_discovery_before_any_content():
    client = RaisingGraphClient()
    client.errors["sites/contoso.sharepoint.com:/sites/Engineering"] = AuthenticationError(
        "bad credentials", status_code=401
    )
    connector = SharePointConnector(config(), client=client)

    with pytest.raises(AuthenticationError):
        list(connector.discover())
    assert client.byte_calls == []


def _at_root(item: dict[str, Any]) -> dict[str, Any]:
    item["parentReference"] = {"driveId": "drive1", "id": "root", "path": "/drive/root:"}
    return item


def _under(folder: str, item: dict[str, Any]) -> dict[str, Any]:
    item["parentReference"] = {
        "driveId": "drive1",
        "id": folder,
        "path": f"/drive/root:/{folder}",
    }
    return item
