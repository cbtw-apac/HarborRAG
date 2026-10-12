"""The source connector port and the ports a connector is built from.

A connector reads the window it is given and reports checkpoints; only the
runtime moves the watermark. Connectors receive their HTTP client and auth
provider through :meth:`SourceConnector.create`, so retries, rate limits,
refresh, and the SSRF guard are written once in the shared kit.
"""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator, Mapping, Sequence
from contextlib import AbstractAsyncContextManager
from dataclasses import dataclass, field
from datetime import timedelta
from enum import StrEnum
from typing import Any, ClassVar, Protocol, Self

from pydantic import BaseModel

from .config import ConnectorConfig
from .events import BlobRef, ConnectorEvent, DocKey, SyncWindow


class DiscoveryMode(StrEnum):
    """How a connector finds changes."""

    TIMESTAMP = "timestamp"
    """Reads ``window.start``/``window.end`` against a source-side updated time."""
    TOKEN = "token"
    """Reads committed tokens in ``window.state`` (delta links, head SHAs)."""
    SCAN = "scan"
    """Enumerates everything every run; the scan is the listing."""


class DeletionMode(StrEnum):
    """Where a connector's deletions come from."""

    NATIVE = "native"
    """The source reports deletes (tombstones, tree diffs, delta facets)."""
    CHILD_DIFF = "child_diff"
    """A parent lists its complete children; the runtime diffs them."""
    RECONCILE = "reconcile"
    """The runtime compares ``list_keys`` against stored keys on a schedule."""


@dataclass(frozen=True, slots=True)
class SyncPolicy:
    """Runtime defaults for building this connector's windows."""

    overlap: timedelta = timedelta(0)
    lag: timedelta = timedelta(0)
    reconcile_every: timedelta | None = None

    def __post_init__(self) -> None:
        if self.overlap < timedelta(0) or self.lag < timedelta(0):
            raise ValueError("overlap and lag must not be negative")
        if self.reconcile_every is not None and self.reconcile_every <= timedelta(0):
            raise ValueError("reconcile interval must be positive")


@dataclass(frozen=True, slots=True)
class Capabilities:
    """What a connector supports; the registry validates it at startup."""

    discovery: DiscoveryMode
    kinds: frozenset[str]
    deletions: frozenset[DeletionMode] = frozenset()
    list_keys: bool = True
    fetch_keys: bool = True
    blobs: bool = False
    policy: SyncPolicy = field(default_factory=SyncPolicy)

    def __post_init__(self) -> None:
        if not self.kinds:
            raise ValueError("a connector must declare at least one record kind")
        if DeletionMode.RECONCILE in self.deletions and not self.list_keys:
            raise ValueError("reconcile deletions need list_keys")


@dataclass(frozen=True, slots=True)
class PreflightCheck:
    """One probe run before a sync, such as "project PTC is browsable"."""

    name: str
    ok: bool
    message: str = ""


@dataclass(frozen=True, slots=True)
class PreflightResult:
    """All probes of one preflight run."""

    checks: tuple[PreflightCheck, ...]

    @property
    def ok(self) -> bool:
        return all(check.ok for check in self.checks)

    @property
    def failures(self) -> tuple[PreflightCheck, ...]:
        return tuple(check for check in self.checks if not check.ok)


class SourceResponse(Protocol):
    """A fully read HTTP response."""

    @property
    def status_code(self) -> int: ...
    @property
    def headers(self) -> Mapping[str, str]: ...
    @property
    def url(self) -> str: ...
    @property
    def content(self) -> bytes: ...
    def json(self) -> Any: ...


class SourceStream(Protocol):
    """An HTTP response whose body is read incrementally."""

    @property
    def status_code(self) -> int: ...
    @property
    def headers(self) -> Mapping[str, str]: ...
    def aiter_bytes(self) -> AsyncIterator[bytes]: ...


class SourceHttpClient(Protocol):
    """HTTP access with the kit's retry, rate-limit, auth, and safety policies."""

    async def request(  # noqa: PLR0913 - keyword-only request options
        self,
        method: str,
        url: str,
        *,
        params: Mapping[str, Any] | None = None,
        json: Any = None,
        data: Mapping[str, str] | None = None,
        headers: Mapping[str, str] | None = None,
        read_only: bool = False,
        cost: int = 1,
    ) -> SourceResponse:
        """Send a request; raise the run-level errors in ``sources.errors``."""
        ...

    def stream(
        self,
        method: str,
        url: str,
        *,
        params: Mapping[str, Any] | None = None,
        headers: Mapping[str, str] | None = None,
        allow_cross_origin_redirect: bool = False,
    ) -> AbstractAsyncContextManager[SourceStream]:
        """Open a streamed response; a cross-origin hop never carries credentials."""
        ...


class SourceAuthProvider(Protocol):
    """Supplies request credentials and, when it can, refreshes them."""

    @property
    def supports_refresh(self) -> bool: ...

    async def headers(self) -> Mapping[str, str]:
        """Return the auth headers for the next request."""
        ...

    async def refresh(self) -> bool:
        """Force new credentials; return whether anything changed."""
        ...


class SourceConnector[CT: BaseModel](Protocol):
    """Reads one source. It never persists anything and never moves the watermark."""

    capabilities: ClassVar[Capabilities]

    @classmethod
    async def create(
        cls,
        *,
        source_id: str,
        auth: SourceAuthProvider,
        http: SourceHttpClient,
        config: ConnectorConfig,
        logger: logging.Logger,
    ) -> Self:
        """Build a connector for one connection."""
        ...

    async def preflight(self) -> PreflightResult:
        """Probe each operation once with the configured credential."""
        ...

    def parse_checkpoint(self, raw: object) -> CT | None:
        """Parse a heartbeat checkpoint; ``None`` discards an unreadable or foreign one."""
        ...

    def fetch(self, window: SyncWindow, checkpoint: CT | None) -> AsyncIterator[ConnectorEvent[CT]]:
        """Read every change in ``window``, resuming after ``checkpoint``."""
        ...

    def fetch_keys(self, keys: Sequence[DocKey]) -> AsyncIterator[ConnectorEvent[CT]]:
        """Re-read specific items: each key yields a record, tombstone, or failure."""
        ...

    def list_keys(self) -> AsyncIterator[DocKey]:
        """List every key currently in scope, IDs only, for reconciliation."""
        ...

    def open_blob(self, ref: BlobRef) -> AsyncIterator[bytes]:
        """Stream the bytes behind a blob reference this connector produced."""
        ...
