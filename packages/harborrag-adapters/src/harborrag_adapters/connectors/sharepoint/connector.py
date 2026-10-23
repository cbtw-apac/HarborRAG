"""SharePoint drive and site page discovery and loading orchestration."""

from __future__ import annotations

import hashlib
import logging
import time
import uuid
from collections.abc import Iterator
from typing import Any

from harborrag_adapters.connectors.base import BaseConnector
from harborrag_adapters.connectors.exceptions import (
    AuthorizationError,
    DocumentProcessingError,
    FetchError,
)
from harborrag_adapters.connectors.rate_limiting import ConnectorRateLimiter
from harborrag_adapters.connectors.schemas import (
    ConnectorCapabilities,
    ConnectorQuery,
    ConnectorSkip,
)
from harborrag_core.domain.raw_document import RawDocument
from harborrag_core.domain.source import SourceRecord

from .client import SharePointClient, _RequestsGraphClient
from .config import SharePointSiteConfig
from .drive import SharePointDriveAPI
from .drive_paths import is_drive_file, item_mime_type, item_name, stored_payload
from .mappers import (
    build_document_metadata,
    build_page_document_metadata,
    drive_item_id_from_record,
    is_page_record,
    page_id_from_record,
)
from .pages import SharePointPagesAPI, page_title, render_page_html
from .skips import (
    NOT_IN_ALLOWLIST,
    SharePointItemSkippedError,
    SharePointSkipRecorder,
    item_load_error,
    log_event,
    log_skip,
    skip_reason_for_error,
)

_TIME_FOR_SHAREPOINT_CONNECTOR_TESTS = time


class SharePointConnector(BaseConnector):
    """Connector for SharePoint document libraries through Microsoft Graph.

    Discovery resolves a site and its drives in scope, then walks drive items
    from the configured root or explicit item IDs. Loading downloads file
    content through Graph and preserves drive metadata for downstream
    provenance.

    With ``include_pages`` on, discovery also lists the site's modern pages and
    loading renders each page's text web parts as one HTML document.

    Items declined for a standard reason (allowlist, size, extension, deleted,
    access denied, retries exhausted) are reported through ``skipped`` and
    logged; they never stop the run. Only authentication failure, a site the
    app cannot reach, and invalid config are fatal. On a direct load, a deleted
    item fails as a missing source item and exhausted retries as a retryable
    source-unavailable failure; the other reasons are non-retryable declines.

    Every structured log entry carries ``run_id``. It defaults to a fresh ID
    per connector instance; the ingestion runtime binds its task ID through
    :meth:`bind_run_id` so connector logs join the task's own.
    """

    provider_name = "sharepoint"
    # No native discover_page: the inherited fallback re-walks the tree from
    # the start for every page, so the runtime streams discover() once instead.
    capabilities = ConnectorCapabilities(
        pagination=False,
        incremental_sync=True,
        full_sync=True,
        relationships=True,
    )

    def __init__(
        self,
        config: SharePointSiteConfig,
        *,
        client: SharePointClient | None = None,
        rate_limiter: ConnectorRateLimiter | None = None,
    ) -> None:
        """Initialize drive operations with an optional client override.

        ``rate_limiter`` is the runtime's shared limiter, so every worker
        throttles one Graph app registration together and reports its waits.
        """
        self.config = config
        self.client = client or _RequestsGraphClient(config, rate_limiter=rate_limiter)
        self.run_id = uuid.uuid4().hex
        self._skips = SharePointSkipRecorder(self.run_id)
        self._drive = SharePointDriveAPI(self.client, config, skips=self._skips)
        self._pages = SharePointPagesAPI(self.client, config)

    def bind_run_id(self, run_id: str) -> None:
        """Tag this connector's structured logs with the caller's run ID."""
        self.run_id = run_id
        self._skips.run_id = run_id

    def connect(self) -> None:
        """Authenticate and resolve the site up front so bad credentials fail fast.

        A configured ``site_id`` needs no lookup to resolve, so the site is
        read once anyway: that both obtains a token and proves the app is
        granted the site before any traversal starts.
        """
        self._drive.verify_site_access(self._drive.resolve_site())

    def close(self) -> None:
        """Release the client session when the connector owns one."""

        close = getattr(self.client, "close", None)
        if callable(close):
            close()

    @property
    def skipped(self) -> tuple[ConnectorSkip, ...]:
        """Items the most recent discovery declined, with standard reasons."""
        return self._skips.skipped

    def discover(self, query: ConnectorQuery | None = None) -> Iterator[SourceRecord]:
        """Discover SharePoint records from IDs, folder traversal, and site pages.

        Pages are yielded before files. They are one flat list per site while a
        drive can be a very large tree, so a limited discovery still samples
        both kinds. Selectors that point into a drive (item IDs, a root item, or
        a path) target files only, so they skip pages. A record is yielded at
        most once per discovery, even when two selectors reach the same item.
        """
        query = query or ConnectorQuery()
        self._ensure_connected()
        self._skips.reset()
        site = self._drive.resolve_site()
        if not self._site_allowed(site):
            self._log_discovery_summary(0)
            return
        drives = self._drive.resolve_drives(site)
        log_event(
            logging.INFO,
            "sharepoint.discovery_started",
            run_id=self.run_id,
            site_id=site.get("id"),
            drive_ids=[drive.get("id") for drive in drives],
            include_pages=self.config.include_pages,
        )

        seen: set[str] = set()
        for record in self._iter_records(site, drives, query):
            if record.id in seen:
                continue
            seen.add(record.id)
            yield record
            if query.limit is not None and len(seen) >= query.limit:
                break
        self._log_discovery_summary(len(seen))

    def _log_discovery_summary(self, discovered: int) -> None:
        """Log what one completed traversal found and declined, by reason."""
        skips = self._skips.counts_by_reason()
        log_event(
            logging.INFO,
            "sharepoint.discovery_summary",
            run_id=self.run_id,
            discovered=discovered,
            skipped=sum(skips.values()),
            skipped_by_reason=skips,
        )

    def _iter_records(
        self,
        site: dict[str, Any],
        drives: list[dict[str, Any]],
        query: ConnectorQuery,
    ) -> Iterator[SourceRecord]:
        item_ids = self._item_ids_from_query(query) or list(self.config.item_ids or ())
        root_item_id = self._root_item_id_from_query(query)
        if (item_ids or root_item_id) and len(drives) != 1:
            raise ValueError("SharePoint item_ids and root_item_id need a single-drive scope")
        if self.config.include_pages and not (item_ids or root_item_id or query.path):
            yield from self._pages.walk_pages(site=site, query=query)

        for drive in drives:
            if item_ids:
                for item_id in item_ids:
                    yield from self._drive.records_from_item_id(
                        item_id,
                        site=site,
                        drive=drive,
                        query=query,
                    )
                continue
            yield from self._drive.walk_children(
                site=site,
                drive=drive,
                query=query,
                item_id=root_item_id,
                path=None if root_item_id else (query.path or self.config.root_path),
            )

    def _site_allowed(self, site: dict[str, Any]) -> bool:
        """Check the tenant and site allowlists, recording a skip when rejected."""
        rejected = self._drive.allowlist.rejected_tenant()
        if rejected is not None:
            self._skips.record(
                NOT_IN_ALLOWLIST,
                f"tenant {rejected!r} is not in allowed_tenant_ids",
                path=rejected,
            )
            return False
        rejected = self._drive.allowlist.rejected_site(site)
        if rejected is not None:
            self._skips.record(
                NOT_IN_ALLOWLIST,
                f"site {rejected!r} is not in allowed_sites",
                url=rejected if "://" in rejected else None,
                path=rejected,
            )
            return False
        return True

    def load(self, record: SourceRecord) -> RawDocument:
        """Load one SharePoint record: a site page as HTML, or a drive file."""
        page = is_page_record(record)
        if page and not self.config.include_pages:
            raise DocumentProcessingError("SharePoint page loading is disabled")
        site = self._drive.resolve_site()
        self._require_allowed_site(site, record)
        if page:
            return self._load_page(record, site)
        return self._load_file(record, site)

    def _require_allowed_site(self, site: dict[str, Any], record: SourceRecord) -> None:
        allowlist = self._drive.allowlist
        rejected = allowlist.rejected_tenant()
        detail = f"tenant {rejected!r} is not in allowed_tenant_ids"
        if rejected is None:
            rejected = allowlist.rejected_site(site)
            detail = f"site {rejected!r} is not in allowed_sites"
        if rejected is not None:
            log_skip(NOT_IN_ALLOWLIST, detail, item_id=record.locator, run_id=self.run_id)
            raise SharePointItemSkippedError(NOT_IN_ALLOWLIST, detail, item_id=record.locator)

    def _load_page(self, record: SourceRecord, site: dict[str, Any]) -> RawDocument:
        """Render one site page's text web parts as a single HTML document.

        A page with no text web parts still loads, as its title alone. The page
        exists and its title is searchable, and failing the load would report
        an error for a page that is simply image- or link-only.
        """
        site_id = str(site.get("id"))
        record_site_id = record.metadata.get("site_id")
        if record_site_id is not None and str(record_site_id) != site_id:
            raise DocumentProcessingError(
                f"SharePoint page {record.id} belongs to site {record_site_id!r}, "
                f"outside configured site {site_id!r}"
            )
        page_id = page_id_from_record(record)
        page = self._fetch_or_skip(
            lambda: self._pages.get_page(site_id, page_id),
            item_id=page_id,
        )
        html, has_text = render_page_html(page)
        title = page_title(page)
        if not has_text:
            log_event(
                logging.INFO,
                "sharepoint.page_title_only",
                run_id=self.run_id,
                page_id=page_id,
                detail="page has no text web parts; loading its title only",
            )
        log_event(
            logging.INFO,
            "sharepoint.page_loading",
            run_id=self.run_id,
            page_id=page_id,
            site_id=site_id,
            title=title,
            url=page.get("webUrl"),
        )
        metadata = build_page_document_metadata(
            page,
            site=site,
            title=title,
            checksum=hashlib.sha256(html.encode("utf-8")).hexdigest(),
            has_text_content=has_text,
        )
        return RawDocument(
            id=record.id,
            source=str(page.get("webUrl") or record.id),
            content=html,
            content_type="text/html",
            metadata=metadata.to_dict(),
            raw=stored_payload(page),
        )

    def _load_file(self, record: SourceRecord, site: dict[str, Any]) -> RawDocument:
        """Download one SharePoint drive file as a raw document.

        Every allowlist, size, and extension check runs on the item's metadata
        before its content is requested, so an out-of-policy file is never
        downloaded.
        """
        item_id = drive_item_id_from_record(record)
        drive = self._record_drive(record, site, item_id)
        drive_id = str(drive.get("id"))
        item = self._fetch_or_skip(
            lambda: self._drive.get_item(drive_id, item_id),
            item_id=item_id,
        )

        if not is_drive_file(item):
            raise DocumentProcessingError(
                f"SharePoint drive item {item_id} is not a downloadable file"
            )
        declined = self._drive.declined_reason(item)
        if declined is not None:
            reason, detail = declined
            url = item.get("webUrl")
            log_skip(reason, detail, item_id=item_id, url=url, run_id=self.run_id)
            raise SharePointItemSkippedError(reason, detail, item_id=item_id, url=url)

        log_event(
            logging.INFO,
            "sharepoint.file_loading",
            run_id=self.run_id,
            item_id=item_id,
            drive_id=drive_id,
            name=item_name(item),
            url=item.get("webUrl"),
        )
        content = self._fetch_or_skip(
            lambda: self.client.get_bytes(f"drives/{drive_id}/items/{item_id}/content"),
            item_id=item_id,
            url=item.get("webUrl"),
        )
        metadata = build_document_metadata(
            item,
            site=site,
            drive=drive,
            checksum=hashlib.sha256(content).hexdigest(),
        )

        return RawDocument(
            id=record.id,
            source=str(item.get("webUrl") or item_id),
            content=content,
            content_type=item_mime_type(item),
            metadata=metadata.to_dict(),
            raw=stored_payload(item),
        )

    def _record_drive(
        self,
        record: SourceRecord,
        site: dict[str, Any],
        item_id: str,
    ) -> dict[str, Any]:
        """Return the in-scope drive a file record belongs to, or refuse it."""
        drives = self._drive.resolve_drives(site)
        record_drive_id = record.metadata.get("drive_id")
        if record_drive_id is None:
            if len(drives) != 1:
                raise DocumentProcessingError(
                    f"SharePoint item {item_id} has no drive_id and the scope has several drives"
                )
            return drives[0]
        drive = self._drive.drive_in_scope(site, str(record_drive_id))
        if drive is None:
            expected = [str(candidate.get("id")) for candidate in drives]
            raise DocumentProcessingError(
                f"SharePoint item {item_id} belongs to drive {str(record_drive_id)!r}, "
                f"outside configured drive {', '.join(repr(value) for value in expected)}"
            )
        return drive

    def _fetch_or_skip(self, fetch: Any, *, item_id: str, url: str | None = None) -> Any:
        """Run one item-level Graph call, turning item failures into skips."""
        try:
            return fetch()
        except (AuthorizationError, FetchError) as exc:
            reason = skip_reason_for_error(exc)
            if reason is None:
                raise
            log_skip(reason, str(exc), item_id=item_id, url=url, run_id=self.run_id)
            raise item_load_error(reason, exc, item_id=item_id, url=url) from exc

    def load_by_ids(self, item_ids: list[str]) -> Iterator[RawDocument]:
        """Load files for callers that already have drive item IDs."""
        for item_id in item_ids:
            yield self.load(self._drive.record_for_item_id(item_id))

    @staticmethod
    def _item_ids_from_query(query: ConnectorQuery) -> list[str]:
        values = (
            query.filters.get("item_ids")
            or query.filters.get("drive_item_ids")
            or query.filters.get("file_ids")
        )
        if values is None:
            return []
        if isinstance(values, str):
            return [values]
        return [str(value) for value in values]

    @staticmethod
    def _root_item_id_from_query(query: ConnectorQuery) -> str | None:
        value = query.filters.get("root_item_id") or query.filters.get("folder_item_id")
        return str(value) if value else None
