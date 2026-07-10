from __future__ import annotations

import logging
from abc import ABC, abstractmethod
from collections.abc import Iterator

from harborrag_core.domain.raw_document import RawDocument
from harborrag_core.domain.source import SourceRecord

from .exceptions import AuthenticationError
from .schemas import ConnectorCapabilities, ConnectorQuery

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

    @abstractmethod
    def discover(self, query: ConnectorQuery | None = None) -> Iterator[SourceRecord]:
        """Yield lightweight records that identify loadable source items."""
        raise NotImplementedError

    @abstractmethod
    def load(self, record: SourceRecord) -> RawDocument:
        """Fetch one raw document for a previously discovered source record."""
        raise NotImplementedError

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
        for record in self.discover(query):
            try:
                yield self.load(record)
            except AuthenticationError:
                raise
            except Exception as exc:  # noqa: BLE001 - per-record isolation
                if on_error == "raise":
                    raise
                logger.warning(
                    "Skipping record %s after load failure: %s", record.id, exc
                )
