"""Network safety: which hosts a connector may reach and which redirects it may follow.

Connector base URLs come from admin configuration, so a typo or a hostile
value must not turn a worker into a proxy for the internal network. The
guard resolves each host once and refuses private, loopback, link-local,
and otherwise non-global addresses unless the host is allowlisted (a Data
Center instance on the corporate network).

The check runs before the request, and the HTTP library resolves the name
again when it connects, so a DNS answer that changes in between (rebinding)
is not covered; network egress policy remains the outer fence.
"""

from __future__ import annotations

import asyncio
import ipaddress
import re
import socket
from collections.abc import Awaitable, Callable, Iterable

import httpx

from harborrag_core.sources import ConfigError, Transient

type Resolver = Callable[[str, int], Awaitable[list[str]]]

_DEFAULT_PORTS = {"http": 80, "https": 443}
_USERINFO = re.compile(r"^([A-Za-z][A-Za-z0-9+.-]*://)[^/?#]*@")


async def system_resolver(host: str, port: int) -> list[str]:
    """Resolve ``host`` with the event loop's resolver."""

    infos = await asyncio.get_running_loop().getaddrinfo(host, port, type=socket.SOCK_STREAM)
    return [str(info[4][0]) for info in infos]


def _is_public(address: str) -> bool:
    ip = ipaddress.ip_address(address.split("%", 1)[0])
    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped is not None:
        ip = ip.ipv4_mapped
    return ip.is_global and not ip.is_multicast


def _literal_ip(host: str) -> str | None:
    try:
        return str(ipaddress.ip_address(host.strip("[]")))
    except ValueError:
        return None


def port_of(url: httpx.URL) -> int | None:
    return url.port or _DEFAULT_PORTS.get(url.scheme)


def redact_url(url: str | httpx.URL) -> str:
    """``url`` without userinfo, query, or fragment, for error messages and logs.

    Tokens and signatures travel in query strings (pre-signed downloads) and
    in userinfo, so neither may reach an exception message. Never raises: a
    URL httpx cannot parse is cut by hand.
    """

    try:
        parsed = url if isinstance(url, httpx.URL) else httpx.URL(url)
    except httpx.InvalidURL:
        text = str(url).split("#", 1)[0].split("?", 1)[0]
        return _USERINFO.sub(r"\1", text)
    return str(parsed.copy_with(userinfo=b"", query=None, fragment=None))


def same_origin(url: httpx.URL, other: httpx.URL) -> bool:
    """Scheme, host, and effective port all match."""

    return (
        url.scheme == other.scheme
        and url.host.lower() == other.host.lower()
        and port_of(url) == port_of(other)
    )


class SsrfGuard:
    """Refuses URLs that resolve to non-public addresses unless allowlisted."""

    def __init__(
        self,
        allow_hosts: Iterable[str] = (),
        *,
        resolver: Resolver = system_resolver,
        allow_http: bool = False,
    ) -> None:
        self._allow = frozenset(host.lower() for host in allow_hosts)
        self._resolver = resolver
        self._allow_http = allow_http
        self._verified: set[tuple[str, int]] = set()

    async def check(self, url: httpx.URL) -> None:
        """Raise :class:`ConfigError` when ``url`` may not be requested."""

        scheme = url.scheme
        if scheme != "https" and not (scheme == "http" and self._allow_http):
            raise ConfigError(f"refusing {scheme or 'schemeless'} URL; only https is allowed")
        host = url.host.lower()
        if not host:
            raise ConfigError("refusing a URL without a host")
        if host in self._allow:
            return
        port = port_of(url) or 443
        if (host, port) in self._verified:
            return
        literal = _literal_ip(host)
        addresses = [literal] if literal else await self._resolve(host, port)
        blocked = [address for address in addresses if not _is_public(address)]
        if blocked or not addresses:
            raise ConfigError(
                f"refusing {host}: it resolves to a non-public address; "
                "allowlist the host to reach an internal instance"
            )
        self._verified.add((host, port))

    async def _resolve(self, host: str, port: int) -> list[str]:
        try:
            return await self._resolver(host, port)
        except OSError as exc:
            # Resolver outages are transient; a name that never resolves is
            # caught by preflight before the first sync.
            raise Transient(f"cannot resolve {host}: {exc}") from exc
