from __future__ import annotations

import logging
import time
from collections.abc import Callable, Iterator
from dataclasses import asdict
from urllib.parse import parse_qs, quote, urlparse

from requests.exceptions import HTTPError, RequestException

from harborrag_core.domain.raw_document import RawDocument
from harborrag_core.domain.source import SourceRecord

from harborrag_adapters.connectors.base import BaseConnector
from harborrag_adapters.connectors.exceptions import (
    AuthenticationError,
    DocumentProcessingError,
    FetchError,
    RateLimitError,
)
from harborrag_adapters.connectors.schemas import ConnectorCapabilities, ConnectorQuery
from harborrag_adapters.connectors.shared.http import SyncRateLimiter

from .attachments import AttachmentProcessor
from .client_factory import build_client
from .config import ConfluenceDeploymentType, ConfluenceSpaceConfig
from .content import ConfluenceMarkdownConverter
from .mappers import build_document_metadata
from .pagination import (
    CONTENT_EXPAND,
    DEFAULT_PAGE_SIZE,
    build_cloud_search_params,
    build_dc_search_params,
)

logger = logging.getLogger(">> HarborRAG Connector::Confluence")

_RETRYABLE_STATUS = {429, 500, 502, 503, 504}
_MAX_RETRIES = 3
_BACKOFF_FACTOR = 0.5


class ConfluenceConnector(BaseConnector):
    """Connector for Atlassian Confluence (Cloud and Data Center/Server).

    Built on `atlassian-python-api` for auth/session/raw HTTP. Page bodies
    are converted from Confluence's rendered HTML (`body.export_view`) to
    Markdown; attachments are optionally downloaded and text-extracted
    (PDF/OCR, Office formats, images, etc.) with per-attachment metadata
    kept separate from the page's own metadata.
    """

    provider_name = "confluence"
    capabilities = ConnectorCapabilities(
        pagination=True,
        labels=True,
        comments=True,
        incremental_sync=True,
        full_sync=True,
        relationships=True,
    )

    def __init__(self, config: ConfluenceSpaceConfig) -> None:
        self.config = config
        self.base_url = str(config.base_url).rstrip("/")
        self.client = build_client(config)
        self._rate_limiter = SyncRateLimiter.per_minute(config.requests_per_minute)
        self._markdown = ConfluenceMarkdownConverter()
        self._attachments = AttachmentProcessor(
            download_fn=self._download,
            base_url=self.base_url,
            custom_parsers=config.custom_parsers,
            process_attachment_callback=config.process_attachment_callback,
            max_attachment_size_bytes=config.max_attachment_size_bytes,
            fail_on_error=config.fail_on_error,
            logger_=logger,
        )

    def discover(self, query: ConnectorQuery | None = None) -> Iterator[SourceRecord]:
        """Yield a lightweight `SourceRecord` per processable content item.

        Uses a minimal `expand` (labels only, no page bodies) so listing a
        whole space stays cheap; `load()` does the full fetch per item.
        """
        query = query or ConnectorQuery()
        records = (
            self._discover_cloud(query.updated_after)
            if self.config.deployment_type == ConfluenceDeploymentType.CLOUD
            else self._discover_datacenter(query.updated_after)
        )
        for index, record in enumerate(records):
            if query.limit is not None and index >= query.limit:
                return
            yield record

    def load(self, record: SourceRecord) -> RawDocument:
        """Fetch the full content item for `record` and return a `RawDocument`.

        Content comes from `body.export_view` converted to Markdown, not
        `body.storage` -- storage format is Confluence's internal macro/XML
        representation and converts to mostly noise. `raw` still carries
        the storage-format body for anyone who needs macro-aware
        reprocessing later.
        """
        content_id = record.locator
        if not content_id:
            raise DocumentProcessingError(
                f"SourceRecord {record.id!r} is missing a locator (content id)"
            )

        content = self._request(
            f"content/{quote(str(content_id), safe='')}",
            params={"expand": CONTENT_EXPAND},
        )

        if not self._should_process_content(content):
            raise DocumentProcessingError(
                f"Confluence content {content_id} no longer matches label filters"
            )

        missing = [
            name
            for name, value in (
                ("id", content.get("id")),
                ("title", content.get("title")),
                ("space", content.get("space", {}).get("key")),
            )
            if not value
        ]
        if missing:
            raise DocumentProcessingError(
                f"Confluence content {content_id} is missing required fields: {', '.join(missing)}"
            )

        metadata = build_document_metadata(
            content,
            base_url=self.base_url,
            deployment_type=self.config.deployment_type,
            comments=self._fetch_comments(content_id) if self.config.include_comments else [],
        )

        if self.config.include_attachments:
            self._attach_attachment_metadata(metadata, content_id)

        body_html = content.get("body", {}).get("export_view", {}).get("value") or ""
        markdown_body = self._markdown.convert(body_html)

        return RawDocument(
            id=record.id,
            source=metadata["canonical_url"],
            content=markdown_body,
            content_type="text/markdown",
            metadata=metadata,
            raw=content,
        )

    def _attach_attachment_metadata(self, metadata: dict, content_id: str) -> None:
        """Populate metadata["attachments"]/["attachments_summary"].

        Kept as a dict of plain metadata rather than a separate return value
        from load() -- consistent with how comments are already embedded in
        metadata by build_document_metadata(). For very large or numerous
        attachments (e.g. big OCR'd PDFs) this can make metadata sizeable;
        if that becomes a problem, consider having load() return one
        RawDocument per attachment instead of embedding extracted text
        here, once the core RawDocument/indexing contract has a place for
        that.
        """
        raw_attachments = self._list_attachments(content_id)
        results = self._attachments.process(raw_attachments)
        metadata["attachments"] = [asdict(a) for a in results]
        metadata["attachments_summary"] = {
            status: sum(1 for a in results if a.status == status)
            for status in ("processed", "skipped", "unsupported", "failed")
        }

    def _fetch_comments(self, content_id: str) -> list[dict]:
        """Full comment thread for a page, including nested replies.

        The `children.comment` expand on `content/{id}` (used in earlier
        versions of this connector) only returns one level of comments and
        silently drops replies-to-replies. `depth=all` on the dedicated
        child/comment endpoint is the only way to get the full thread.
        """
        response = self._request(
            f"content/{content_id}/child/comment",
            params={"depth": "all", "expand": "body.storage,history"},
        )
        return response.get("results", [])

    def load_by_ids(self, content_ids: list[str]) -> Iterator[RawDocument]:
        """Load specific known page/blogpost ids directly, bypassing discover().

        Useful when you already know which pages changed (e.g. from a
        webhook payload or an external changelog) and don't want to list
        the whole space just to find them.
        """
        for content_id in content_ids:
            record = SourceRecord(
                id=f"confluence://{self.config.space_key}/{content_id}",
                source_type="text/html",
                locator=str(content_id),
                metadata={"content_id": content_id, "space_key": self.config.space_key},
            )
            yield self.load(record)

    def _discover_cloud(self, updated_after) -> Iterator[SourceRecord]:
        cursor = None
        while True:
            params = build_cloud_search_params(
                self.config.space_key,
                self.config.content_types,
                cursor,
                light=True,
                updated_after=updated_after,
            )
            response = self._request("content/search", params=params)
            results = response.get("results", [])
            if not results:
                return

            for content in results:
                if self._should_process_content(content):
                    yield self._to_source_record(content)

            # Cloud's documented pagination mechanism: keep following
            # _links.next (it carries the cursor) until it's absent.
            next_url = response.get("_links", {}).get("next")
            if not next_url:
                return
            cursor = self._extract_cursor(next_url)
            if not cursor:
                return

    def _discover_datacenter(self, updated_after) -> Iterator[SourceRecord]:
        start = 0
        while True:
            params = build_dc_search_params(
                self.config.space_key,
                self.config.content_types,
                start,
                light=True,
                updated_after=updated_after,
            )
            response = self._request("content/search", params=params)
            results = response.get("results", [])
            if not results:
                return

            for content in results:
                if self._should_process_content(content):
                    yield self._to_source_record(content)

            # `content/search` does not reliably return a total-count field.
            # The only safe "last page" signal is getting back fewer
            # results than we asked for.
            page_limit = response.get("limit", DEFAULT_PAGE_SIZE)
            if len(results) < page_limit:
                return
            start += len(results)

    def _to_source_record(self, content: dict) -> SourceRecord:
        content_id = content["id"]
        labels = [
            label["name"]
            for label in content.get("metadata", {}).get("labels", {}).get("results", [])
        ]
        return SourceRecord(
            id=f"confluence://{self.config.space_key}/{content_id}",
            source_type="text/html",
            # Plain content id, not a rebuilt URL: the one thing load()
            # needs to refetch the item, kept as a single source of truth.
            locator=str(content_id),
            metadata={
                "content_id": content_id,
                "title": content.get("title"),
                "content_type": content.get("type"),
                "space_key": self.config.space_key,
                "labels": labels,
            },
        )

    def _should_process_content(self, content: dict) -> bool:
        labels = {
            label["name"]
            for label in content.get("metadata", {}).get("labels", {}).get("results", [])
        }
        if self.config.exclude_labels and any(
            label in labels for label in self.config.exclude_labels
        ):
            return False
        if self.config.include_labels:
            return any(label in labels for label in self.config.include_labels)
        return True

    def _with_retry(self, call: Callable[[], dict]) -> dict:
        """Retry/backoff/exception-translation wrapper for JSON endpoints
        (`self.client.get(...)`-style calls that raise on non-2xx).

        NOTE: verify `self._rate_limiter`'s actual interface -- this assumes
        a blocking `.acquire()`; adjust if
        `harborrag_adapters.connectors.shared.http.SyncRateLimiter` is a
        context manager instead. Also verify `self.client.get()` actually
        raises `requests.exceptions.HTTPError` on non-2xx for your pinned
        atlassian-python-api version -- some versions/paths raise
        library-specific error classes instead (check `atlassian.errors`).
        """
        last_error: Exception | None = None
        for attempt in range(_MAX_RETRIES + 1):
            self._rate_limiter.acquire()
            try:
                return call()
            except HTTPError as exc:
                status = exc.response.status_code if exc.response is not None else None
                if status in (401, 403):
                    raise AuthenticationError(str(exc)) from exc
                if status not in _RETRYABLE_STATUS or attempt == _MAX_RETRIES:
                    if status == 429:
                        raise RateLimitError(str(exc)) from exc
                    raise FetchError(str(exc)) from exc
                last_error = exc
            except RequestException as exc:
                if attempt == _MAX_RETRIES:
                    raise FetchError(str(exc)) from exc
                last_error = exc

            delay = _BACKOFF_FACTOR * (2**attempt)
            logger.warning(
                "Retrying after error (attempt %d/%d, sleeping %.1fs): %s",
                attempt + 1, _MAX_RETRIES, delay, last_error,
            )
            time.sleep(delay)

        raise FetchError(str(last_error))

    def _request(self, endpoint: str, **kwargs) -> dict:
        return self._with_retry(lambda: self.client.get(f"rest/api/{endpoint}", **kwargs))

    def _list_attachments(self, content_id: str) -> list[dict]:
        response = self._with_retry(lambda: self.client.get_attachments_from_content(content_id))
        return response.get("results", [])

    def _download(self, url: str) -> bytes | None:
        """Download raw bytes (e.g. an attachment), through the same rate
        limiter/backoff as `_request()`.

        Written as an explicit status-code check rather than relying on an
        exception, since it's unclear whether `atlassian-python-api`'s
        generic `.request()` raises on non-2xx across all versions the way
        `.get()` does -- this is correct either way.
        """
        for attempt in range(_MAX_RETRIES + 1):
            self._rate_limiter.acquire()
            try:
                response = self.client.request(path=url, absolute=True)
            except (HTTPError, RequestException) as exc:
                if attempt == _MAX_RETRIES:
                    logger.warning("Attachment download failed for %s: %s", url, exc)
                    return None
                delay = _BACKOFF_FACTOR * (2**attempt)
                logger.warning(
                    "Retrying download (attempt %d/%d, sleeping %.1fs) for %s: %s",
                    attempt + 1, _MAX_RETRIES, delay, url, exc,
                )
                time.sleep(delay)
                continue

            if response.status_code == 200 and response.content:
                return response.content
            if response.status_code not in _RETRYABLE_STATUS or attempt == _MAX_RETRIES:
                logger.warning(
                    "Attachment download for %s returned HTTP %s", url, response.status_code
                )
                return None
            delay = _BACKOFF_FACTOR * (2**attempt)
            logger.warning(
                "Retrying download (attempt %d/%d, sleeping %.1fs) for %s: HTTP %s",
                attempt + 1, _MAX_RETRIES, delay, url, response.status_code,
            )
            time.sleep(delay)

        return None

    @staticmethod
    def _extract_cursor(next_url: str) -> str | None:
        values = parse_qs(urlparse(next_url).query).get("cursor")
        return values[0] if values else None