"""Dependency readiness checks for ``GET /readyz``.

Handles:
- One protocol-level probe per dependency: Postgres, Redis, Temporal, Qdrant and the graph store
- Running every check concurrently under one timeout and caching the report for a few seconds

Does NOT open client libraries or connection pools: each probe speaks just enough of the
dependency's protocol to tell a serving process from a closed port.
"""

from __future__ import annotations

import asyncio
import logging
import socket
import ssl
import struct
import time
from collections.abc import Awaitable, Callable, Mapping
from concurrent.futures import ThreadPoolExecutor
from urllib.parse import urlsplit

from harborrag_runtime.config.settings import RuntimeSettings

from .settings import ApiSettings

logger = logging.getLogger("harborrag.app.api.readiness")

Check = Callable[[], Awaitable[bool]]

_SSL_REQUEST = struct.pack("!ii", 8, 80877103)
_POSTGRES_PORT = 5432
_REDIS_PORT = 6379
_HTTP_OK_UPPER_BOUND = 400
_RESOLVER = ThreadPoolExecutor(max_workers=16, thread_name_prefix="readiness-dns")


class ReadinessChecks:
    """Run named dependency checks concurrently and cache the combined report."""

    def __init__(
        self,
        checks: Mapping[str, Check],
        *,
        ttl_seconds: float = 5.0,
        timeout_seconds: float = 2.0,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._checks = dict(checks)
        self._ttl = ttl_seconds
        self._timeout = timeout_seconds
        self._clock = clock
        self._report: dict[str, str] | None = None
        self._expires_at = 0.0
        self._lock = asyncio.Lock()

    async def report(self) -> dict[str, str]:
        """Return ``ok`` or ``fail`` per dependency, reusing a report younger than the TTL.

        Returns:
            Each check's name mapped to ``ok`` or ``fail``.
        """
        async with self._lock:
            if self._report is None or self._clock() >= self._expires_at:
                names = list(self._checks)
                results = await asyncio.gather(*(self._run(name) for name in names))
                self._report = dict(zip(names, results, strict=True))
                self._expires_at = self._clock() + self._ttl
            return dict(self._report)

    async def _run(self, name: str) -> str:
        """Run one check under the shared timeout."""
        try:
            async with asyncio.timeout(self._timeout):
                ready = await self._checks[name]()
        except Exception:  # noqa: BLE001 - a failed probe is a report, never an error
            logger.warning("Readiness check %s failed", name, exc_info=True)
            return "fail"
        return "ok" if ready else "fail"


async def _addresses(host: str, port: int) -> list[str]:
    """Resolve ``host`` to its addresses, in resolver order, on the probes' own threads.

    The event loop's resolver threads are shared with the whole API, and a lookup of a
    stopped service can hold one for seconds; a probe must neither starve the API's
    lookups nor fail a healthy dependency queued behind a slow one.
    """
    loop = asyncio.get_running_loop()
    infos = await loop.run_in_executor(
        _RESOLVER, socket.getaddrinfo, host, port, 0, socket.SOCK_STREAM
    )
    return list(dict.fromkeys(str(info[4][0]) for info in infos))


async def _connect(
    host: str, port: int, *, tls: bool
) -> tuple[asyncio.StreamReader, asyncio.StreamWriter]:
    """Open a connection to the first of ``host``'s addresses that accepts one.

    Raises:
        OSError: No address accepted the connection.
    """
    context = ssl.create_default_context() if tls else None
    refused: OSError = OSError(f"{host} resolved to no address")
    for address in await _addresses(host, port):
        try:
            return await asyncio.open_connection(
                address, port, ssl=context, server_hostname=host if tls else None
            )
        except OSError as error:
            refused = error
    raise refused


async def _exchange(host: str, port: int, payload: bytes, *, tls: bool = False) -> bytes:
    """Send ``payload`` and return the first bytes of the reply."""
    reader, writer = await _connect(host, port, tls=tls)
    try:
        writer.write(payload)
        await writer.drain()
        return await reader.read(64)
    finally:
        writer.close()
        try:
            await writer.wait_closed()
        except OSError:
            pass


async def postgres_answers(host: str, port: int) -> bool:
    """Whether a Postgres server answers an ``SSLRequest``."""
    try:
        reply = await _exchange(host, port, _SSL_REQUEST)
    except OSError:
        return False
    return reply[:1] in {b"S", b"N"}


async def redis_answers(url: str) -> bool:
    """Whether the Redis server at ``url`` answers ``PING``, even with ``NOAUTH``."""
    parts = urlsplit(url)
    try:
        reply = await _exchange(
            parts.hostname or "localhost",
            parts.port or _REDIS_PORT,
            b"*1\r\n$4\r\nPING\r\n",
            tls=parts.scheme == "rediss",
        )
    except OSError:
        return False
    return reply.startswith((b"+PONG", b"-NOAUTH"))


async def http_ready(url: str) -> bool:
    """Whether ``GET url`` answers with a status below 400."""
    parts = urlsplit(url)
    tls = parts.scheme == "https"
    host = parts.hostname or "localhost"
    request = (
        f"GET {parts.path or '/'} HTTP/1.1\r\nHost: {host}\r\nConnection: close\r\n\r\n"
    ).encode("ascii")
    try:
        reply = await _exchange(host, parts.port or (443 if tls else 80), request, tls=tls)
    except OSError:
        return False
    fields = reply.split(b" ", 2)
    if len(fields) < 2 or not fields[0].startswith(b"HTTP/") or not fields[1].isdigit():
        return False
    return int(fields[1]) < _HTTP_OK_UPPER_BOUND


def _postgres_check(url: str) -> Check | None:
    """Probe the control database when it is Postgres."""
    parts = urlsplit(url)
    if not parts.scheme.startswith("postgres"):
        return None
    host, port = parts.hostname or "localhost", parts.port or _POSTGRES_PORT
    return lambda: postgres_answers(host, port)


def _redis_check(urls: list[str]) -> Check | None:
    """Ping every configured Redis."""
    if not urls:
        return None

    async def check() -> bool:
        results = await asyncio.gather(*(redis_answers(url) for url in urls))
        return all(results)

    return check


def _qdrant_check(runtime: RuntimeSettings) -> Check | None:
    """Probe Qdrant when it serves the vector store."""
    if runtime.vector_provider != "qdrant":
        return None
    url = runtime.qdrant_url.rstrip("/") + "/readyz"
    return lambda: http_ready(url)


def _graph_check(runtime: RuntimeSettings) -> Check | None:
    """Probe FalkorDB when it serves the graph store."""
    if runtime.graph_provider != "falkordb":
        return None
    url = f"redis://{runtime.falkordb_host}:{runtime.falkordb_port}"
    return lambda: redis_answers(url)


def build_readiness_checks(
    runtime: RuntimeSettings,
    api: ApiSettings,
    *,
    temporal: Check,
) -> dict[str, Check]:
    """Build one check per dependency this API process uses.

    Postgres is checked only for a Postgres control database, Redis only when the runtime
    or the API capacity limiter names one, and Qdrant and the graph store only when they
    are the configured providers.

    Args:
        runtime: Runtime settings naming the stores.
        api: API settings naming the capacity Redis.
        temporal: Check that asks the workflow service for its health.

    Returns:
        Checks keyed by dependency name.
    """
    redis_urls = [
        secret.get_secret_value()
        for secret in (api.api_capacity_redis_url, runtime.redis_url)
        if secret is not None
    ]
    checks: dict[str, Check | None] = {
        "postgres": _postgres_check(runtime.control_db_url.get_secret_value()),
        "redis": _redis_check(redis_urls),
        "temporal": temporal,
        "qdrant": _qdrant_check(runtime),
        "graph": _graph_check(runtime),
    }
    return {name: check for name, check in checks.items() if check is not None}
