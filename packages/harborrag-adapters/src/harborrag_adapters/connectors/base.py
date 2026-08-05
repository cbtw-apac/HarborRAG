from __future__ import annotations

import logging
from abc import ABC, abstractmethod
from collections.abc import Iterator
from dataclasses import replace
from itertools import islice

from harborrag_core.domain.raw_document import RawDocument
from harborrag_core.domain.source import SourceRecord

from .exceptions import AuthenticationError
from .schemas import ConnectorCapabilities, ConnectorPage, ConnectorQuery, ConnectorSkip

logger = logging.getLogger("harborrag.adapters.connectors.base")


class BaseConnector(ABC):
    """Common sync connector contract.

    Connectors are responsible for source-specific discovery and loading only.
    They return core domain objects so runtime can handle orchestration,
    concurrency, scheduling, checkpointing, and parsing independently.
    """

    provider_name: str = "base"
    connector_version: str | None = "1.0.0"
    capabilities: ConnectorCapabilities = ConnectorCapabilities()

    def connect(self) -> None:
        """Perform an optional eager connection check.

        Most providers authenticate lazily during the first API request. Override
        this only when a connector needs an explicit session setup or health
        check before discovery starts.
        """
        return None

    def close(self) -> None:
        """Release any connector-owned resources (HTTP sessions, handles, etc.).

        Most providers hold nothing beyond a lazily-created HTTP session.
        Override this when a connector needs deterministic teardown so callers
        can dispose of it via ``with`` or an explicit ``close()`` call.
        """
        return None

    def __enter__(self) -> BaseConnector:
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()

    @abstractmethod
    def discover(self, query: ConnectorQuery | None = None) -> Iterator[SourceRecord]:
        """Yield lightweight records that identify loadable source items."""
        raise NotImplementedError

    @property
    def skipped(self) -> tuple[ConnectorSkip, ...]:
        """Items the most recent ``discover`` traversal dropped, with reasons.

        Discovery must not silently omit an item it declined for a reportable
        reason such as a configured size limit. Providers that enforce those
        limits override this so callers can distinguish a reported skip from a
        silent omission; the default is empty for providers that do not report
        skips yet.
        """
        return ()

    @abstractmethod
    def load(self, record: SourceRecord) -> RawDocument:
        """Fetch one raw document for a previously discovered source record."""
        raise NotImplementedError

    def discover_page(
        self,
        query: ConnectorQuery | None,
        *,
        cursor: str | None,
        page_size: int,
    ) -> ConnectorPage:
        """Compatibility pagination for connectors without a native cursor.

        Provider connectors should override this method. The fallback preserves
        existing third-party connectors while making its numeric replay cursor
        explicit instead of leaking offset logic into runtime.
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
        records = tuple(islice(self.discover(bounded), offset, offset + page_size))
        next_cursor = str(offset + len(records)) if len(records) == page_size else None
        return ConnectorPage(records=records, next_cursor=next_cursor)

    def load_raw_documents(
        self,
        query: ConnectorQuery | None = None,
        *,
        on_error: str = "raise",
    ) -> Iterator[RawDocument]:
        """Discover records and load them in order.

        ``on_error`` controls per-record failure isolation, essential when
        crawling large sources where a single restricted/deleted item must not
        abort the whole sync:

        * ``"raise"`` (default): propagate the first load failure.
        * ``"skip"``: log and skip records that fail to load, but still
          propagate :class:`AuthenticationError` (a bad credential is fatal for
          the whole run, not a per-record condition).
        """
        if on_error not in ("raise", "skip"):
            raise ValueError(f"Unknown on_error policy: {on_error!r}")

        self.connect()
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
