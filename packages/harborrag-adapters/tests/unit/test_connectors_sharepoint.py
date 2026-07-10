from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import pytest
from harborrag_adapters.connectors import SharePointConnector, SharePointSiteConfig
from harborrag_adapters.connectors.exceptions import DocumentProcessingError
from harborrag_adapters.connectors.schemas import ConnectorQuery
from harborrag_adapters.connectors.sharepoint.drive import SharePointDriveAPI
from harborrag_adapters.connectors.sharepoint.mappers import (
    build_document_metadata,
    drive_item_id_from_record,
)
from harborrag_adapters.connectors.sharepoint.mappers import (
    parse_timestamp as mapper_parse_timestamp,
)
from harborrag_adapters.connectors.sharepoint.utils import (
    children_endpoint,
    item_extension,
    item_mime_type,
    item_path,
    matches_pattern,
    parse_sharepoint_site_url,
    site_path_endpoint,
)
from harborrag_core.domain.source import SourceRecord

pytestmark = [pytest.mark.unit, pytest.mark.graybox]


SITE_URL = "https://contoso.sharepoint.com/sites/Engineering"


class FakeGraphClient:
    def __init__(self) -> None:
        self.responses: dict[str, list[dict[str, Any]]] = {}
        self.downloads: dict[str, bytes] = {}
        self.calls: list[tuple[str, dict[str, Any] | None]] = []
        self.byte_calls: list[str] = []

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
            raise AssertionError(f"Unexpected Microsoft Graph endpoint: {endpoint}")
        return values.pop(0)

    def get_bytes(self, endpoint: str) -> bytes:
        self.byte_calls.append(endpoint)
        try:
            return self.downloads[endpoint]
        except KeyError as exc:
            raise AssertionError(f"Unexpected Microsoft Graph bytes: {endpoint}") from exc


def config(**overrides: Any) -> SharePointSiteConfig:
    values = {
        "site_url": SITE_URL,
        "access_token": "token",
        "requests_per_minute": 6000,
        "page_size": 2,
    }
    values.update(overrides)
    return SharePointSiteConfig(**values)


def site() -> dict[str, Any]:
    return {
        "id": "site1",
        "name": "Engineering",
        "displayName": "Engineering",
        "webUrl": SITE_URL,
    }


def drive(drive_id: str = "drive1", name: str = "Documents") -> dict[str, Any]:
    return {
        "id": drive_id,
        "name": name,
        "driveType": "documentLibrary",
        "webUrl": f"{SITE_URL}/{name}",
    }


def file_item(
    item_id: str = "file1",
    name: str = "Guide.docx",
    *,
    parent_id: str = "root",
    size: int = 12,
    updated: str = "2024-05-24T20:57:56Z",
    hidden: bool = False,
) -> dict[str, Any]:
    item = {
        "id": item_id,
        "name": name,
        "webUrl": f"{SITE_URL}/Shared%20Documents/{name}",
        "size": size,
        "file": {
            "mimeType": (
                "application/vnd.openxmlformats-officedocument."
                "wordprocessingml.document"
            ),
            "hashes": {"quickXorHash": f"hash-{item_id}"},
        },
        "parentReference": {
            "driveId": "drive1",
            "id": parent_id,
            "path": "/drive/root:/Docs",
        },
        "createdDateTime": "2024-01-01T00:00:00Z",
        "lastModifiedDateTime": updated,
        "createdBy": {"user": {"displayName": "Ada"}},
        "lastModifiedBy": {"user": {"displayName": "Grace"}},
        "eTag": f"etag-{item_id}",
        "cTag": f"ctag-{item_id}",
    }
    if hidden:
        item["hidden"] = {}
    return item


def folder_item(item_id: str = "folder1", name: str = "Runbooks") -> dict[str, Any]:
    return {
        "id": item_id,
        "name": name,
        "folder": {"childCount": 1},
        "parentReference": {"driveId": "drive1", "id": "root", "path": "/drive/root:"},
    }


def add_site_and_default_drive(client: FakeGraphClient) -> None:
    client.add("sites/contoso.sharepoint.com:/sites/Engineering", site())
    client.add("sites/site1/drive", drive())


def test_config_parses_site_url_and_normalizes_extensions():
    cfg = config(allowed_extensions={"docx", ".PDF"}, excluded_extensions={"tmp"})

    assert cfg.hostname == "contoso.sharepoint.com"
    assert cfg.site_path == "sites/Engineering"
    assert cfg.allowed_extensions == {".docx", ".pdf"}
    assert cfg.excluded_extensions == {".tmp"}
    assert parse_sharepoint_site_url(SITE_URL) == (
        "contoso.sharepoint.com",
        "sites/Engineering",
    )


def test_children_endpoint_supports_root_path_and_item_children():
    assert children_endpoint("drive1") == "drives/drive1/root/children"
    assert (
        children_endpoint("drive1", path="Shared Documents")
        == "drives/drive1/root:/Shared%20Documents:/children"
    )
    assert (
        children_endpoint("drive1", item_id="folder1")
        == "drives/drive1/items/folder1/children"
    )


def test_discover_recurses_pages_and_filters_files():
    client = FakeGraphClient()
    add_site_and_default_drive(client)
    client.add(
        "drives/drive1/root:/Docs:/children",
        {
            "value": [
                folder_item(),
                file_item("file1", "Guide.docx"),
                file_item("skip", "Draft.txt"),
            ],
            "@odata.nextLink": "https://graph.microsoft.com/v1.0/page2",
        },
    )
    client.add(
        "https://graph.microsoft.com/v1.0/page2",
        {"value": [file_item("hidden", "Hidden.docx", hidden=True)]},
    )
    client.add(
        "drives/drive1/items/folder1/children",
        {"value": [file_item("file2", "Nested.pdf", parent_id="folder1")]},
    )
    connector = SharePointConnector(
        config(root_path="Docs", allowed_extensions={".docx", ".pdf"}),
        client=client,
    )

    records = list(
        connector.discover(
            ConnectorQuery(
                updated_after=datetime(2024, 1, 1, tzinfo=UTC),
                pattern="*d*",
            )
        )
    )

    assert [record.metadata["item_id"] for record in records] == ["file1", "file2"]
    assert records[0].id == "sharepoint://site1/drive1/file1"
    assert records[0].source_type.endswith("wordprocessingml.document")
    assert records[0].checksum == "hash-file1"
    assert ("https://graph.microsoft.com/v1.0/page2", None) in client.calls


def test_discover_supports_direct_item_ids_and_named_drives():
    client = FakeGraphClient()
    client.add("sites/contoso.sharepoint.com:/sites/Engineering", site())
    client.add(
        "sites/site1/drives",
        {"value": [drive("drive0", "Assets"), drive("drive1", "Documents")]},
    )
    client.add("drives/drive1/items/file1", file_item("file1", "Guide.docx"))
    connector = SharePointConnector(config(drive_name="Documents"), client=client)

    records = list(
        connector.discover(ConnectorQuery(filters={"item_ids": ["file1"]}))
    )

    assert records[0].locator == "file1"
    assert records[0].metadata["drive_id"] == "drive1"


def test_load_downloads_file_content_and_builds_metadata():
    client = FakeGraphClient()
    client.add("drives/drive1/items/file1", file_item("file1", "Guide.docx"))
    client.downloads["drives/drive1/items/file1/content"] = b"docx-bytes"
    connector = SharePointConnector(
        SharePointSiteConfig(
            site_id="site1",
            drive_id="drive1",
            access_token="token",
            requests_per_minute=6000,
        ),
        client=client,
    )

    document = connector.load(
        SourceRecord(
            "sharepoint://site1/drive1/file1",
            "application/octet-stream",
            "file1",
            metadata={"drive_id": "drive1", "item_id": "file1"},
        )
    )

    assert document.id == "sharepoint://site1/drive1/file1"
    assert document.content == b"docx-bytes"
    assert document.content_type.endswith("wordprocessingml.document")
    assert document.metadata["source_system"] == "sharepoint"
    assert document.metadata["item_name"] == "Guide.docx"
    assert document.metadata["created_by"] == "Ada"
    assert document.source == f"{SITE_URL}/Shared%20Documents/Guide.docx"
    assert "web_url" not in document.metadata
    assert "site_web_url" not in document.metadata
    assert "drive_web_url" not in document.metadata
    assert "mime_type" not in document.metadata
    assert client.byte_calls == ["drives/drive1/items/file1/content"]


def test_load_rejects_oversized_files_before_download():
    client = FakeGraphClient()
    client.add("drives/drive1/items/file1", file_item("file1", "Guide.docx", size=99))
    connector = SharePointConnector(
        SharePointSiteConfig(
            site_id="site1",
            drive_id="drive1",
            access_token="token",
            max_file_size_bytes=10,
            requests_per_minute=6000,
        ),
        client=client,
    )

    with pytest.raises(DocumentProcessingError, match="max_file_size_bytes"):
        connector.load(
            SourceRecord(
                "sharepoint://site1/drive1/file1",
                "application/octet-stream",
                "file1",
                metadata={"drive_id": "drive1", "item_id": "file1"},
            )
        )
    assert client.byte_calls == []


def test_load_rejects_folders():
    client = FakeGraphClient()
    client.add("drives/drive1/items/folder1", folder_item())
    connector = SharePointConnector(
        SharePointSiteConfig(
            site_id="site1",
            drive_id="drive1",
            access_token="token",
            requests_per_minute=6000,
        ),
        client=client,
    )

    with pytest.raises(DocumentProcessingError, match="not a downloadable file"):
        connector.load(
            SourceRecord("sharepoint://site1/drive1/folder1", "folder", "folder1")
        )


# --------------------------------------------------------------------------
# config.py validation


def test_config_requires_site_id_or_site_url():
    with pytest.raises(ValueError, match="requires either site_id or site_url"):
        SharePointSiteConfig(access_token="token")


def test_config_requires_access_token_or_client_credentials():
    with pytest.raises(ValueError, match="access_token or client credentials"):
        SharePointSiteConfig(site_url=SITE_URL)


def test_config_rejects_out_of_range_requests_per_minute():
    with pytest.raises(ValueError, match="requests_per_minute must be between"):
        config(requests_per_minute=0)


def test_config_rejects_out_of_range_page_size():
    with pytest.raises(ValueError, match="page_size must be between"):
        config(page_size=0)


# --------------------------------------------------------------------------
# utils.py pure helpers


def test_parse_sharepoint_site_url_rejects_non_absolute_url():
    with pytest.raises(ValueError, match="absolute SharePoint URL"):
        parse_sharepoint_site_url("not-a-url")


def test_site_path_endpoint_without_path():
    assert site_path_endpoint("contoso.sharepoint.com", None) == (
        "sites/contoso.sharepoint.com:/"
    )
    assert site_path_endpoint("contoso.sharepoint.com", "") == (
        "sites/contoso.sharepoint.com:/"
    )


def test_item_extension_returns_empty_without_dot():
    assert item_extension({"name": "README"}) == ""


def test_item_mime_type_folder_and_default_fallbacks():
    assert item_mime_type(folder_item()) == "application/vnd.microsoft.graph.folder"
    assert item_mime_type({"id": "x"}) == "application/octet-stream"


def test_item_path_without_root_marker_returns_name():
    item = {"name": "Doc.txt", "parentReference": {"path": "/drive/other"}}
    assert item_path(item) == "Doc.txt"


def test_item_path_with_empty_folder_path_returns_name():
    item = {"name": "Doc.txt", "parentReference": {"path": "/drive/root:"}}
    assert item_path(item) == "Doc.txt"


def test_matches_pattern_plain_substring():
    assert matches_pattern({"name": "Guide.docx"}, "guide") is True
    assert matches_pattern({"name": "Guide.docx"}, "missing") is False


# --------------------------------------------------------------------------
# mappers.py edge cases


def test_parse_timestamp_handles_missing_and_invalid_values():
    assert mapper_parse_timestamp(None) is None
    assert mapper_parse_timestamp("not-a-timestamp") is None


def test_drive_item_id_from_record_requires_an_item_id():
    record = SourceRecord("sharepoint://site1/drive1/x", "application/octet-stream", "")
    record.metadata.pop("item_id", None)
    with pytest.raises(ValueError, match="does not contain item_id"):
        drive_item_id_from_record(record)


def test_build_document_metadata_handles_missing_file_and_identity_info():
    item = {
        "id": "file1",
        "name": "Notes.txt",
        "parentReference": {},
        "createdBy": {"nonDictValue": "oops", "application": {"id": "app-1"}},
        "lastModifiedBy": "not-a-dict",
    }
    metadata = build_document_metadata(
        item, site=site(), drive=drive(), checksum="etag-1"
    )
    assert metadata.sharepoint_hashes == {}
    assert metadata.created_by is None
    assert metadata.updated_by is None


# --------------------------------------------------------------------------
# drive.py traversal edge cases


def test_walk_children_stops_descending_when_not_recursive():
    client = FakeGraphClient()
    client.add(
        "drives/drive1/root/children",
        {"value": [folder_item(), file_item("file1", "Guide.docx")]},
    )
    api = SharePointDriveAPI(client, config())

    records = list(
        api.walk_children(site=site(), drive=drive(), query=ConnectorQuery(recursive=False))
    )

    assert [r.metadata["item_id"] for r in records] == ["file1"]
    assert "drives/drive1/items/folder1/children" not in [c[0] for c in client.calls]


def test_records_from_item_id_file_filtered_out_yields_nothing():
    client = FakeGraphClient()
    client.add("drives/drive1/items/file1", file_item("file1", "Draft.tmp"))
    api = SharePointDriveAPI(client, config(excluded_extensions={".tmp"}))

    records = list(
        api.records_from_item_id(
            "file1", site=site(), drive=drive(), query=ConnectorQuery()
        )
    )
    assert records == []


def test_walk_children_skips_folder_without_id():
    client = FakeGraphClient()
    client.add(
        "drives/drive1/root/children",
        {"value": [{"folder": {"childCount": 0}, "parentReference": {}}]},
    )
    api = SharePointDriveAPI(client, config())

    records = list(
        api.walk_children(site=site(), drive=drive(), query=ConnectorQuery())
    )
    assert records == []
    # No recursive call for the id-less folder means only the root page was fetched.
    assert len(client.calls) == 1


def test_records_from_item_id_for_file_and_folder_and_neither():
    client = FakeGraphClient()
    client.add("drives/drive1/items/file1", file_item("file1", "Guide.docx"))
    client.add("drives/drive1/items/folder1", folder_item())
    client.add("drives/drive1/items/folder1/children", {"value": []})
    client.add("drives/drive1/items/other1", {"id": "other1", "name": "weird"})
    api = SharePointDriveAPI(client, config())

    file_records = list(
        api.records_from_item_id(
            "file1", site=site(), drive=drive(), query=ConnectorQuery()
        )
    )
    assert [r.metadata["item_id"] for r in file_records] == ["file1"]

    folder_records = list(
        api.records_from_item_id(
            "folder1", site=site(), drive=drive(), query=ConnectorQuery()
        )
    )
    assert folder_records == []

    neither_records = list(
        api.records_from_item_id(
            "other1", site=site(), drive=drive(), query=ConnectorQuery()
        )
    )
    assert neither_records == []


def test_resolve_drive_by_name_not_found_raises():
    client = FakeGraphClient()
    client.add("sites/site1/drives", {"value": [drive("drive0", "Assets")]})
    api = SharePointDriveAPI(client, config(drive_name="Missing"))

    with pytest.raises(Exception, match="was not found"):
        api.resolve_drive(site())


def test_resolve_drive_default_missing_id_raises():
    from harborrag_adapters.connectors.exceptions import FetchError

    client = FakeGraphClient()
    client.add("sites/site1/drive", {})
    api = SharePointDriveAPI(client, config())

    with pytest.raises(FetchError, match="did not include id"):
        api.resolve_drive(site())


def test_resolve_site_missing_id_raises():
    from harborrag_adapters.connectors.exceptions import FetchError

    client = FakeGraphClient()
    client.add("sites/contoso.sharepoint.com:/sites/Engineering", {})
    api = SharePointDriveAPI(client, config())

    with pytest.raises(FetchError, match="did not include id"):
        api.resolve_site()


def test_should_process_file_hidden_allowed_when_include_hidden():
    api = SharePointDriveAPI(FakeGraphClient(), config(include_hidden=True))
    item = file_item("file1", "Guide.docx", hidden=True)
    assert api.should_process_file(item, ConnectorQuery()) is True


def test_should_process_file_excludes_disallowed_extension():
    api = SharePointDriveAPI(FakeGraphClient(), config(allowed_extensions={".pdf"}))
    item = file_item("file1", "Guide.docx")
    assert api.should_process_file(item, ConnectorQuery()) is False


def test_should_process_file_callback_reject_allow_and_exception_paths():
    calls: list[str] = []

    def reject(name, size, mime):
        calls.append(name)
        return False, "policy"

    api = SharePointDriveAPI(
        FakeGraphClient(), config(process_file_callback=reject)
    )
    item = file_item("file1", "Guide.docx")
    assert api.should_process_file(item, ConnectorQuery()) is False
    assert calls == ["Guide.docx"]

    def explode(name, size, mime):
        raise RuntimeError("boom")

    api_swallow = SharePointDriveAPI(
        FakeGraphClient(), config(process_file_callback=explode, fail_on_error=False)
    )
    assert api_swallow.should_process_file(item, ConnectorQuery()) is False

    api_raise = SharePointDriveAPI(
        FakeGraphClient(), config(process_file_callback=explode, fail_on_error=True)
    )
    with pytest.raises(RuntimeError):
        api_raise.should_process_file(item, ConnectorQuery())

    def allow(name, size, mime):
        return True, ""

    api_allow = SharePointDriveAPI(
        FakeGraphClient(), config(process_file_callback=allow)
    )
    assert api_allow.should_process_file(item, ConnectorQuery()) is True


def test_should_process_file_no_size_limit_configured():
    api = SharePointDriveAPI(FakeGraphClient(), config(max_file_size_bytes=None))
    item = file_item("file1", "Guide.docx", size=10_000_000)
    assert api.should_process_file(item, ConnectorQuery()) is True


def test_should_process_file_rejects_oversized_file():
    api = SharePointDriveAPI(FakeGraphClient(), config(max_file_size_bytes=5))
    item = file_item("file1", "Guide.docx", size=100)
    assert api.should_process_file(item, ConnectorQuery()) is False


def test_should_process_file_excludes_matching_extension():
    api = SharePointDriveAPI(FakeGraphClient(), config(excluded_extensions={".docx"}))
    item = file_item("file1", "Guide.docx")
    assert api.should_process_file(item, ConnectorQuery()) is False


def test_should_process_file_skips_when_older_than_updated_after():
    api = SharePointDriveAPI(FakeGraphClient(), config())
    item = file_item("file1", "Guide.docx", updated="2020-01-01T00:00:00Z")
    query = ConnectorQuery(updated_after=datetime(2024, 1, 1, tzinfo=UTC))
    assert api.should_process_file(item, query) is False


def test_should_process_file_rejects_pattern_mismatch():
    api = SharePointDriveAPI(FakeGraphClient(), config())
    item = file_item("file1", "Guide.docx")
    assert api.should_process_file(item, ConnectorQuery(pattern="*.pdf")) is False


def test_enforce_size_limit_noop_when_no_limit_configured():
    api = SharePointDriveAPI(FakeGraphClient(), config(max_file_size_bytes=None))
    api.enforce_size_limit(file_item("file1", "Guide.docx", size=10_000_000))


def test_enforce_size_limit_noop_when_size_is_zero():
    api = SharePointDriveAPI(FakeGraphClient(), config(max_file_size_bytes=5))
    api.enforce_size_limit(file_item("file1", "Guide.docx", size=0))


def test_enforce_size_limit_raises_when_exceeded():
    api = SharePointDriveAPI(FakeGraphClient(), config(max_file_size_bytes=5))
    with pytest.raises(DocumentProcessingError, match="exceeds"):
        api.enforce_size_limit(file_item("file1", "Guide.docx", size=100))


def test_enforce_size_limit_allows_within_bounds():
    api = SharePointDriveAPI(FakeGraphClient(), config(max_file_size_bytes=100))
    api.enforce_size_limit(file_item("file1", "Guide.docx", size=10))


def test_record_for_item_id_resolves_site_and_drive():
    client = FakeGraphClient()
    add_site_and_default_drive(client)
    client.add("drives/drive1/items/file1", file_item("file1", "Guide.docx"))
    api = SharePointDriveAPI(client, config())

    record = api.record_for_item_id("file1")
    assert record.metadata["item_id"] == "file1"


def test_iter_site_drives_follows_pagination():
    client = FakeGraphClient()
    client.add(
        "sites/site1/drives",
        {
            "value": [drive("drive0", "Assets")],
            "@odata.nextLink": "https://graph.microsoft.com/v1.0/drives-page2",
        },
    )
    client.add(
        "https://graph.microsoft.com/v1.0/drives-page2",
        {"value": [drive("drive1", "Documents")]},
    )
    api = SharePointDriveAPI(client, config())

    drives = list(api.iter_site_drives("site1"))
    assert [d["id"] for d in drives] == ["drive0", "drive1"]


# --------------------------------------------------------------------------
# connector.py discovery limit and item-id list edge cases


def test_discover_stops_at_limit_during_item_id_iteration():
    client = FakeGraphClient()
    add_site_and_default_drive(client)
    client.add("drives/drive1/items/file1", file_item("file1", "A.docx"))
    client.add("drives/drive1/items/file2", file_item("file2", "B.docx"))
    connector = SharePointConnector(config(), client=client)

    records = list(
        connector.discover(
            ConnectorQuery(limit=1, filters={"item_ids": ["file1", "file2"]})
        )
    )

    assert [r.metadata["item_id"] for r in records] == ["file1"]


def test_discover_stops_at_limit_during_folder_walk():
    client = FakeGraphClient()
    add_site_and_default_drive(client)
    client.add(
        "drives/drive1/root/children",
        {"value": [file_item("file1", "A.docx"), file_item("file2", "B.docx")]},
    )
    connector = SharePointConnector(config(), client=client)

    records = list(connector.discover(ConnectorQuery(limit=1)))

    assert [r.metadata["item_id"] for r in records] == ["file1"]


def test_item_ids_from_query_accepts_bare_string():
    client = FakeGraphClient()
    add_site_and_default_drive(client)
    client.add("drives/drive1/items/file1", file_item("file1", "A.docx"))
    connector = SharePointConnector(config(), client=client)

    records = list(connector.discover(ConnectorQuery(filters={"item_ids": "file1"})))
    assert [r.metadata["item_id"] for r in records] == ["file1"]


def test_item_ids_from_query_accepts_list_alias():
    client = FakeGraphClient()
    add_site_and_default_drive(client)
    client.add("drives/drive1/items/file1", file_item("file1", "A.docx"))
    connector = SharePointConnector(config(), client=client)

    records = list(
        connector.discover(ConnectorQuery(filters={"drive_item_ids": ["file1"]}))
    )
    assert [r.metadata["item_id"] for r in records] == ["file1"]


def test_discover_uses_root_item_id_filter_alias():
    client = FakeGraphClient()
    add_site_and_default_drive(client)
    client.add(
        "drives/drive1/items/folder1/children",
        {"value": [file_item("file1", "A.docx", parent_id="folder1")]},
    )
    connector = SharePointConnector(config(), client=client)

    records = list(
        connector.discover(ConnectorQuery(filters={"folder_item_id": "folder1"}))
    )
    assert [r.metadata["item_id"] for r in records] == ["file1"]


def test_load_by_ids_loads_each_item():
    client = FakeGraphClient()
    add_site_and_default_drive(client)
    # record_for_item_id() fetches the item once to build the record, then
    # load() fetches it again to confirm it's still a file before downloading.
    client.add(
        "drives/drive1/items/file1",
        file_item("file1", "A.docx"),
        file_item("file1", "A.docx"),
    )
    client.downloads["drives/drive1/items/file1/content"] = b"bytes-a"
    connector = SharePointConnector(config(), client=client)

    documents = list(connector.load_by_ids(["file1"]))

    assert [d.content for d in documents] == [b"bytes-a"]
