"""V1 synchronous connector contract and discovery paging helper.

Handles:
- ``BaseConnector`` discover, describe and load contract
- Offset paging by replaying discovery
- Per-record error isolation in ``load_raw_documents``
"""

from __future__ import annotations

import logging
from abc import ABC, abstractmethod
from collections.abc import Callable, Iterator
from dataclasses import replace
from itertools import islice

from harborrag_core.domain.raw_document import RawDocument
from harborrag_core.domain.source import SourceRecord

from .descriptors import (
    ConnectorDocumentDescriptor,
    default_document_descriptor,
)
from .exceptions import AuthenticationError
from .schemas import ConnectorCapabilities, ConnectorPage, ConnectorQuery, ConnectorSkip

logger = logging.getLogger("harborrag.adapters.connectors.base")


def replay_discovery_page(
    discover: Callable[[ConnectorQuery], Iterator[SourceRecord]],
    query: ConnectorQuery | None,
    *,
    cursor: str | None,
    page_size: int,
) -> ConnectorPage:
    """Serve one page by replaying ``discover`` from the start up to a numeric offset.

    Re-runs discovery for every page; the cursor is the count of records already served.

    Args:
        discover: Record generator, called with a query bounded to the page end.
        query: Caller query, or ``None`` for defaults.
        cursor: Numeric offset from a previous page, or ``None`` for the first page.
        page_size: Maximum records in the page.

    Returns:
        Page with the next numeric cursor, or ``None`` when the page is not full.

    Raises:
        ValueError: If ``page_size`` is below 1, or the cursor is non-numeric or negative.
    """
    if page_size < 1:
        raise ValueError("connector page_size must be positive")
    try:
        offset = int(cursor or 0)
    except ValueError as exc:
        raise ValueError("connector does not understand the supplied cursor") from exc
    if offset < 0:
        raise ValueError("connector cursor cannot be negative")
    bounded = replace(query or ConnectorQuery(), limit=offset + page_size)
    records = tuple(islice(discover(bounded), offset, offset + page_size))
    next_cursor = str(offset + len(records)) if len(records) == page_size else None
    return ConnectorPage(records=records, next_cursor=next_cursor)


class BaseConnector(ABC):
    """Base class for synchronous connectors.

    Connectors own source-specific discovery and loading and return core domain
    objects; the runtime owns orchestration and parsing.
    """

    provider_name: str = "base"
    connector_version: str | None = "1.0.0"
    capabilities: ConnectorCapabilities = ConnectorCapabilities()

    def connect(self) -> None:
        """Check the connection eagerly. No-op by default."""
        return None

    def _ensure_connected(self) -> None:
        """Run ``connect`` once; later calls do nothing."""
        if getattr(self, "_connected", False):
            return
        self.connect()
        self._connected = True

    def close(self) -> None:
        """Release connector-owned resources such as HTTP sessions. No-op by default."""
        return None

    def __enter__(self) -> BaseConnector:
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()

    @abstractmethod
    def discover(self, query: ConnectorQuery | None = None) -> Iterator[SourceRecord]:
        """Yield lightweight records that identify loadable source items.

        Args:
            query: Discovery selectors, or ``None`` for the connector defaults.

        Yields:
            One ``SourceRecord`` per loadable item.
        """
        raise NotImplementedError

    @property
    def skipped(self) -> tuple[ConnectorSkip, ...]:
        """Return the items the latest ``discover`` traversal dropped, with reasons.

        Empty by default; providers that report skips override it.
        """
        return ()

    @abstractmethod
    def load(self, record: SourceRecord) -> RawDocument:
        """Fetch the raw document for a discovered source record.

        Args:
            record: Record produced by ``discover``.

        Returns:
            The raw document with source bytes and metadata.
        """
        raise NotImplementedError

    def describe(
        self,
        record: SourceRecord,
    ) -> ConnectorDocumentDescriptor:
        """Build the admission descriptor for a record without fetching source bytes.

        Args:
            record: Record produced by ``discover``.

        Returns:
            Descriptor with a version derived from the record and no bound records.
        """
        return default_document_descriptor(record)

    def discover_page(
        self,
        query: ConnectorQuery | None,
        *,
        cursor: str | None,
        page_size: int,
    ) -> ConnectorPage:
        """Serve one discovery page by replaying ``discover`` from offset 0.

        Override for a native cursor.

        Args:
            query: Discovery selectors, or ``None`` for the connector defaults.
            cursor: Numeric offset from a previous page, or ``None`` for the first page.
            page_size: Maximum records in the page.

        Returns:
            Page of records with the next cursor, or ``None`` on the last page.
        """
        return replay_discovery_page(self.discover, query, cursor=cursor, page_size=page_size)

    def load_raw_documents(
        self,
        query: ConnectorQuery | None = None,
        *,
        on_error: str = "raise",
    ) -> Iterator[RawDocument]:
        """Discover records and load them in order, then close the connector.

        ``AuthenticationError`` always propagates, even with ``on_error="skip"``.

        Args:
            query: Discovery selectors, or ``None`` for the connector defaults.
            on_error: ``"raise"`` to propagate the first load failure, ``"skip"`` to
                log and skip records that fail to load.

        Yields:
            One ``RawDocument`` per loaded record.

        Raises:
            ValueError: If ``on_error`` is not ``"raise"`` or ``"skip"``.
            AuthenticationError: If the source rejects the credentials.
        """
        if on_error not in ("raise", "skip"):
            raise ValueError(f"Unknown on_error policy: {on_error!r}")

        self._ensure_connected()
        try:
            for record in self.discover(query):
                try:
                    yield self.load(record)
                except AuthenticationError:
                    raise
                except Exception as exc:  # noqa: BLE001
                    if on_error == "raise":
                        raise
                    logger.warning(
                        "Skipping record %s after load failure (%s)",
                        record.id,
                        type(exc).__name__,
                    )
        finally:
            self.close()
