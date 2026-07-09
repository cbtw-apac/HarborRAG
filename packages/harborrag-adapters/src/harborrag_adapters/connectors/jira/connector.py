from __future__ import annotations

import logging
import time
from collections.abc import Iterator
from typing import Any, Protocol

import requests
from harborrag_core.domain.raw_document import RawDocument
from harborrag_core.domain.source import SourceRecord

from harborrag_adapters.connectors.attachments import AttachmentProcessor
from harborrag_adapters.connectors.base import BaseConnector
from harborrag_adapters.connectors.exceptions import (
    AuthenticationError,
    DocumentProcessingError,
    FetchError,
    RateLimitError,
)
from harborrag_adapters.connectors.http_utils import (
    ResponseTooLargeError,
    read_capped_content,
    require_same_origin_url,
    retry_delay_seconds,
    safe_error_detail,
)
from harborrag_adapters.connectors.schemas import ConnectorCapabilities, ConnectorQuery
from harborrag_adapters.connectors.utils import (
    enforce_collection_limit,
    extend_with_limit,
)
from harborrag_adapters.parsers import HarborParser

from .config import JiraDeploymentType, JiraProjectConfig
from .mappers import (
    build_document_metadata,
    build_raw_content,
    build_source_record,
    changelog_histories,
    issue_key_from_record,
    issue_url,
)
from .utils import build_jql, search_body, search_jql_body


logger = logging.getLogger("harborrag.adapters.connectors.jira")
_RETRYABLE_STATUS = {429, 500, 502, 503, 504}


class JiraClient(Protocol):
    """Small API surface needed by ``JiraConnector`` for tests and clients."""

    def get_json(
        self,
        endpoint: str,
        *,
        params: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        ...

    def post_json(
        self,
        endpoint: str,
        *,
        json: dict[str, Any],
    ) -> dict[str, Any]:
        ...

    def download_bytes(self, url: str) -> bytes | None:
        ...


class JiraConnector(BaseConnector):
    """Connector for JIRA Cloud and Data Center REST APIs.

    Discovery searches issues by JQL or direct issue keys. Loading a record
    fetches the full issue fields plus optional comments, changelog, and
    attachment text into one Markdown-ish raw document.
    """

    provider_name = "jira"
    capabilities = ConnectorCapabilities(
        pagination=True,
        attachments=True,
        comments=True,
        incremental_sync=True,
        full_sync=True,
        relationships=True,
        labels=True,
        changelog=True,
    )

    def __init__(
        self,
        config: JiraProjectConfig,
        *,
        client: JiraClient | None = None,
        parser: HarborParser | None = None,
    ) -> None:
        self.config = config
        self.base_url = config.base_url.rstrip("/")
        self.client = client or _RequestsJiraClient(config)
        self._attachments = AttachmentProcessor(
            download_fn=self.client.download_bytes,
            base_url=self.base_url,
            parser=parser,
            custom_parsers=config.custom_parsers,
            process_attachment_callback=config.process_attachment_callback,
            max_attachment_size_bytes=config.max_attachment_size_bytes,
            fail_on_error=config.fail_on_error,
            logger_=logger,
        )

    def discover(self, query: ConnectorQuery | None = None) -> Iterator[SourceRecord]:
        """Search JIRA issues or materialize explicitly requested issue keys."""
        query = query or ConnectorQuery()
        issue_keys = self._issue_keys_from_query(query)
        if issue_keys:
            for issue_key in issue_keys[: query.limit]:
                yield self._record_for_key(issue_key, query)
            return

        jql = self._jql_from_query(query)
        yielded = 0
        for issue in self._search(jql):
            record = build_source_record(issue, base_url=self.base_url)
            record.metadata["include_attachments"] = query.include_attachments
            yield record
            yielded += 1
            if query.limit is not None and yielded >= query.limit:
                return

    def load(self, record: SourceRecord) -> RawDocument:
        """Load one JIRA issue as a text/markdown raw document."""
        issue_key = issue_key_from_record(record)
        issue = self._get_issue(issue_key)
        self._validate_issue(issue, issue_key)

        comments = self._fetch_comments(issue_key) if self.config.include_comments else []
        changelog = (
            self._fetch_changelog(issue_key)
            if self.config.include_changelog
            else []
        )
        attachments = []
        include_attachments = bool(
            self.config.include_attachments
            and record.metadata.get("include_attachments", True)
        )
        if include_attachments:
            raw_attachments = issue.get("fields", {}).get("attachment") or []
            enforce_collection_limit(
                count=len(raw_attachments),
                limit=self.config.max_attachments,
                label=f"JIRA attachments for {issue_key}",
                setting_name="max_attachments",
            )
            attachments = self._attachments.process(raw_attachments)

        content = build_raw_content(
            issue,
            comments=comments,
            attachments=attachments,
            include_attachment_text=self.config.include_attachment_text_in_content,
        )
        metadata = build_document_metadata(
            issue,
            base_url=self.base_url,
            comments=comments,
            attachments=attachments,
            changelog=changelog,
        )

        return RawDocument(
            id=record.id,
            source=metadata["url"],
            content=content,
            content_type="text/markdown",
            metadata=metadata,
            raw=issue,
        )

    def load_by_keys(self, issue_keys: list[str]) -> Iterator[RawDocument]:
        """Convenience loader for callers that already have issue keys."""
        for issue_key in issue_keys:
            yield self.load(self._record_for_key(issue_key, ConnectorQuery()))

    def _search(self, jql: str) -> Iterator[dict[str, Any]]:
        """Iterate search results using the endpoint appropriate to deployment."""
        if self.config.deployment_type == JiraDeploymentType.CLOUD:
            yield from self._search_cloud(jql)
        else:
            yield from self._search_datacenter(jql)

    def _search_cloud(self, jql: str) -> Iterator[dict[str, Any]]:
        """Paginate Jira Cloud's token-based ``/search/jql`` endpoint."""
        next_page_token: str | None = None
        while True:
            response = self.client.post_json(
                "search/jql",
                json=search_jql_body(
                    jql=jql,
                    max_results=self.config.page_size,
                    fields=self.config.fields,
                    next_page_token=next_page_token,
                    expand=self._issue_expand(),
                ),
            )
            issues = response.get("issues", [])
            yield from issues

            next_page_token = response.get("nextPageToken")
            # The endpoint returns no total; stop on isLast or an absent token.
            if response.get("isLast") or not next_page_token:
                return

    def _search_datacenter(self, jql: str) -> Iterator[dict[str, Any]]:
        """Paginate Jira Data Center's offset-based ``/search`` endpoint."""
        start_at = 0
        while True:
            response = self.client.post_json(
                "search",
                json=search_body(
                    jql=jql,
                    start_at=start_at,
                    max_results=self.config.page_size,
                    fields=self.config.fields,
                    expand=self._issue_expand(),
                ),
            )
            issues = response.get("issues", [])
            if not issues:
                return
            yield from issues

            start_at = int(response.get("startAt", start_at)) + len(issues)
            total = response.get("total")
            if total is not None and start_at >= int(total):
                return
            if len(issues) < self.config.page_size:
                return

    def _get_issue(self, issue_key: str) -> dict[str, Any]:
        """Fetch one issue with configured fields and expansion settings."""
        return self.client.get_json(
            f"issue/{issue_key}",
            params={
                "fields": ",".join(self.config.fields),
                "expand": ",".join(self._issue_expand()),
            },
        )

    def _fetch_comments(self, issue_key: str) -> list[dict[str, Any]]:
        """Fetch all comments for one issue while enforcing configured caps."""
        comments: list[dict[str, Any]] = []
        start_at = 0
        while True:
            response = self.client.get_json(
                f"issue/{issue_key}/comment",
                params={"startAt": start_at, "maxResults": self.config.page_size},
            )
            values = response.get("comments", [])
            extend_with_limit(
                comments,
                values,
                limit=self.config.max_comments,
                label=f"JIRA comments for {issue_key}",
                setting_name="max_comments",
            )
            start_at = int(response.get("startAt", start_at)) + len(values)
            total = response.get("total")
            if total is not None and start_at >= int(total):
                return comments
            if len(values) < self.config.page_size:
                return comments

    def _fetch_changelog(self, issue_key: str) -> list[dict[str, Any]]:
        """Fetch issue changelog pages and normalize histories for metadata."""
        histories: list[dict[str, Any]] = []
        start_at = 0
        while True:
            response = self.client.get_json(
                f"issue/{issue_key}/changelog",
                params={"startAt": start_at, "maxResults": self.config.page_size},
            )
            values = response.get("values") or response.get("histories") or []
            extend_with_limit(
                histories,
                changelog_histories(response),
                limit=self.config.max_changelog_items,
                label=f"JIRA changelog for {issue_key}",
                setting_name="max_changelog_items",
            )
            start_at = int(response.get("startAt", start_at)) + len(values)
            total = response.get("total")
            if total is not None and start_at >= int(total):
                return histories
            if len(values) < self.config.page_size:
                return histories

    def _jql_from_query(self, query: ConnectorQuery) -> str:
        """Translate shared connector filters into safe JIRA JQL."""
        filters = query.filters
        return build_jql(
            project_keys=self._list_filter(
                filters.get("project_keys") or filters.get("project_key") or query.path,
                default=self.config.project_keys,
            ),
            issue_types=self._list_filter(
                filters.get("issue_types") or filters.get("issue_type"),
                default=self.config.issue_types,
            ),
            statuses=self._list_filter(
                filters.get("statuses") or filters.get("status"),
                default=self.config.statuses,
            ),
            labels=self._list_filter(
                filters.get("labels") or filters.get("label"),
                default=self.config.labels,
            ),
            updated_after=query.updated_after,
            raw_jql=filters.get("jql") or query.pattern,
        )

    def _issue_expand(self) -> tuple[str, ...]:
        """Return the JIRA expand list required by the current config."""
        values = ["renderedFields"]
        if self.config.include_changelog:
            values.append("changelog")
        return tuple(values)

    @staticmethod
    def _issue_keys_from_query(query: ConnectorQuery) -> list[str]:
        values = query.filters.get("issue_keys") or query.filters.get("keys")
        if values is None:
            return []
        if isinstance(values, str):
            return [values]
        return [str(value) for value in values]

    @staticmethod
    def _list_filter(value: Any, *, default: list[str]) -> list[str]:
        if value is None:
            return list(default)
        if isinstance(value, str):
            return [value]
        return [str(item) for item in value]

    def _record_for_key(self, issue_key: str, query: ConnectorQuery) -> SourceRecord:
        """Build a direct-load record when discovery is driven by issue keys."""
        project_key = issue_key.split("-", 1)[0]
        return SourceRecord(
            id=f"jira://{project_key}/{issue_key}",
            source_type="application/vnd.atlassian.jira.issue+json",
            locator=issue_key,
            metadata={
                "issue_key": issue_key,
                "project_key": project_key,
                "url": issue_url(self.base_url, issue_key),
                "include_attachments": query.include_attachments,
            },
        )

    @staticmethod
    def _validate_issue(issue: dict[str, Any], issue_key: str) -> None:
        """Fail fast when JIRA omits fields required by mappers."""
        missing = [
            name
            for name, value in (
                ("id", issue.get("id")),
                ("key", issue.get("key")),
                ("fields.summary", issue.get("fields", {}).get("summary")),
            )
            if not value
        ]
        if missing:
            raise DocumentProcessingError(
                f"JIRA issue {issue_key} missing required fields: "
                f"{', '.join(missing)}"
            )


class _RequestsJiraClient:
    """Authenticated, rate-limited JIRA REST client."""

    def __init__(self, config: JiraProjectConfig) -> None:
        self.config = config
        self.base_url = config.base_url.rstrip("/")
        self.api_version = "3" if config.deployment_type == JiraDeploymentType.CLOUD else "2"
        self.session = requests.Session()
        self.session.headers.update({"Accept": "application/json"})
        if config.deployment_type == JiraDeploymentType.CLOUD:
            self.session.auth = (config.email, config.token)
        else:
            self.session.headers.update({"Authorization": f"Bearer {config.token}"})
        self._min_interval = 60.0 / config.requests_per_minute
        self._last_request_at = 0.0

    def get_json(
        self,
        endpoint: str,
        *,
        params: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """GET a JIRA REST endpoint and decode its JSON body."""
        response = self._request("GET", self._api_url(endpoint), params=params)
        try:
            return response.json()
        except ValueError as exc:
            raise FetchError(f"JIRA returned non-JSON response for {endpoint}") from exc

    def post_json(
        self,
        endpoint: str,
        *,
        json: dict[str, Any],
    ) -> dict[str, Any]:
        """POST a JSON body to a JIRA REST endpoint and decode the response."""
        response = self._request("POST", self._api_url(endpoint), json=json)
        try:
            return response.json()
        except ValueError as exc:
            raise FetchError(f"JIRA returned non-JSON response for {endpoint}") from exc

    def download_bytes(self, url: str) -> bytes | None:
        """Download attachment bytes only from the configured JIRA origin."""
        try:
            safe_url = require_same_origin_url(url, self.base_url, label="JIRA download")
        except ValueError as exc:
            raise FetchError(str(exc)) from exc
        response = self._request(
            "GET", safe_url, headers={"Accept": "*/*"}, stream=True
        )
        try:
            content = read_capped_content(
                response, self.config.max_attachment_size_bytes
            )
        except ResponseTooLargeError as exc:
            raise FetchError(str(exc)) from exc
        return content or None

    def _request(self, method: str, url: str, **kwargs: Any) -> requests.Response:
        """Send one HTTP request with local rate limiting and retry handling."""
        last_error: Exception | None = None
        for attempt in range(self.config.max_retries + 1):
            self._acquire()
            try:
                response = self.session.request(
                    method,
                    url,
                    timeout=self.config.request_timeout_seconds,
                    **kwargs,
                )
            except requests.RequestException as exc:
                last_error = exc
                if attempt == self.config.max_retries:
                    raise FetchError(str(exc)) from exc
                self._sleep(attempt, exc)
                continue

            if response.status_code in (401, 403):
                raise AuthenticationError(safe_error_detail(response.text))
            if response.status_code == 429 and attempt == self.config.max_retries:
                raise RateLimitError(safe_error_detail(response.text))
            if (
                response.status_code not in _RETRYABLE_STATUS
                or attempt == self.config.max_retries
            ):
                if response.status_code >= 400:
                    raise FetchError(
                        f"JIRA request failed with HTTP "
                        f"{response.status_code}: {safe_error_detail(response.text)}"
                    )
                return response

            last_error = FetchError(
                f"JIRA request returned HTTP {response.status_code}"
            )
            self._sleep(attempt, last_error, response.headers)

        raise FetchError(str(last_error))

    def _api_url(self, endpoint: str) -> str:
        """Build a JIRA REST API URL from a relative endpoint."""
        return f"{self.base_url}/rest/api/{self.api_version}/{endpoint.lstrip('/')}"

    def _acquire(self) -> None:
        """Throttle requests according to the configured per-minute budget."""
        now = time.monotonic()
        wait = self._min_interval - (now - self._last_request_at)
        if wait > 0:
            time.sleep(wait)
        self._last_request_at = time.monotonic()

    def _sleep(self, attempt: int, error: Exception, headers: Any = None) -> None:
        """Sleep before retrying, honoring provider retry headers when present."""
        fallback_delay = self.config.backoff_factor * (2**attempt)
        delay = retry_delay_seconds(headers, fallback_delay)
        logger.warning(
            "Retrying JIRA request after error, attempt %d/%d: %s",
            attempt + 1,
            self.config.max_retries,
            error,
        )
        if delay > 0:
            time.sleep(delay)
