"""Keep connector HTTP traffic off private networks unless the operator allows a host.

Handles:
- ``DestinationPolicy``: which peer addresses a connector may reach; public addresses
  always, private, loopback, link-local and reserved ones only when allowed by name
  or network in ``HARBORRAG_CONNECTOR_ALLOWED_HOSTS``
- ``guarded_session``: a ``requests`` session that checks every address a host
  resolves to before connecting, then the address the socket actually reached,
  so neither a closed port nor a DNS answer that changes in between slips through

Proxy variables such as ``HTTPS_PROXY`` are ignored unless the operator sets
``HARBORRAG_CONNECTOR_USE_PROXY``. Through a proxy, only the target host's own
addresses are checked; the proxy then decides where the request goes.
"""

from __future__ import annotations

import os
import socket
from collections.abc import Mapping
from dataclasses import dataclass
from functools import lru_cache
from ipaddress import IPv4Network, IPv6Address, IPv6Network, ip_address, ip_network
from typing import Any, Self
from urllib.parse import urlparse

import requests
from requests.adapters import HTTPAdapter
from requests.utils import select_proxy
from urllib3.connection import HTTPConnection, HTTPSConnection
from urllib3.connectionpool import HTTPConnectionPool, HTTPSConnectionPool

from ..exceptions import FetchError

ALLOWED_HOSTS_VARIABLE = "HARBORRAG_CONNECTOR_ALLOWED_HOSTS"
USE_PROXY_VARIABLE = "HARBORRAG_CONNECTOR_USE_PROXY"
_TRUE = frozenset({"1", "true", "yes", "on"})


class BlockedDestinationError(FetchError):
    """A connector tried to reach a private address no allowed host covers; never retried."""

    retryable = False


@dataclass(frozen=True, slots=True)
class DestinationPolicy:
    """Host names and networks a connector may reach although they are not public."""

    hosts: frozenset[str] = frozenset()
    networks: tuple[IPv4Network | IPv6Network, ...] = ()
    use_proxy: bool = False

    @classmethod
    def from_environ(cls, environ: Mapping[str, str] | None = None) -> Self:
        """Read ``HARBORRAG_CONNECTOR_ALLOWED_HOSTS`` and ``HARBORRAG_CONNECTOR_USE_PROXY``.

        Allowed hosts are comma-separated host names or CIDRs; proxies are used
        only when the second is ``true``.
        """
        variables = os.environ if environ is None else environ
        raw = variables.get(ALLOWED_HOSTS_VARIABLE, "")
        hosts: set[str] = set()
        networks: list[IPv4Network | IPv6Network] = []
        for item in (part.strip() for part in raw.split(",")):
            if not item:
                continue
            try:
                networks.append(ip_network(item, strict=False))
            except ValueError:
                hosts.add(item.casefold().rstrip("."))
        use_proxy = variables.get(USE_PROXY_VARIABLE, "").strip().casefold() in _TRUE
        return cls(frozenset(hosts), tuple(networks), use_proxy)

    def check_host(self, host: str, port: int) -> None:
        """Refuse ``host`` before connecting when any address it resolves to is refused.

        Checking first means a host that resolves to a private address is refused
        before any connection, so it reads the same whether its port is open, closed
        or filtered. A DNS answer that changes after this check can still complete a
        TCP handshake; the peer check then refuses the socket before any request is
        sent. An unresolvable host is left to the connection, which reports it.

        Raises:
            BlockedDestinationError: An address of ``host`` is private and not allowed.
        """
        if host.casefold().rstrip(".") in self.hosts:
            return
        try:
            answers = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
        except OSError:
            return
        for *_, sockaddr in answers:
            self.check(host, str(sockaddr[0]))

    def check(self, host: str, address: str) -> None:
        """Refuse ``address`` unless it is public, or ``host`` or ``address`` is allowed.

        Raises:
            BlockedDestinationError: The address is private and not allowed.
        """
        peer = ip_address(address.split("%", 1)[0])
        if isinstance(peer, IPv6Address) and peer.ipv4_mapped is not None:
            peer = peer.ipv4_mapped
        if peer.is_global and not peer.is_multicast:
            return
        if host.casefold().rstrip(".") in self.hosts:
            return
        if any(peer in network for network in self.networks):
            return
        raise BlockedDestinationError(
            f"{host} resolves to the private address {peer}; list the host or its network "
            f"in {ALLOWED_HOSTS_VARIABLE} to allow it"
        )


def guarded_session(policy: DestinationPolicy | None = None) -> requests.Session:
    """Return a session whose direct connections obey ``policy``, read from the environment by default."""
    session = requests.Session()
    adapter = _GuardedAdapter(policy or DestinationPolicy.from_environ())
    session.mount("http://", adapter)
    session.mount("https://", adapter)
    return session


class _GuardedAdapter(HTTPAdapter):
    """Adapter whose connection pools check each new socket's peer address."""

    def __init__(self, policy: DestinationPolicy) -> None:
        self._policy = policy
        self._pools = _guarded_pools(policy)
        super().__init__()

    def send(  # noqa: PLR0913 - requests' adapter signature
        self,
        request: requests.PreparedRequest,
        stream: bool = False,
        timeout: Any = None,
        verify: bool | str = True,
        cert: Any = None,
        proxies: dict[str, str] | None = None,
    ) -> requests.Response:
        """Send directly through the guarded pools unless the operator opted in to proxies.

        Raises:
            BlockedDestinationError: Through a proxy, the target host resolves to a
                refused address.
        """
        if not self._policy.use_proxy:
            proxies = {}
        elif select_proxy(str(request.url), proxies or {}):
            target = urlparse(str(request.url))
            default_port = 443 if target.scheme == "https" else 80
            self._policy.check_host(target.hostname or "", target.port or default_port)
        return super().send(
            request, stream=stream, timeout=timeout, verify=verify, cert=cert, proxies=proxies
        )

    def init_poolmanager(self, *args: Any, **kwargs: Any) -> None:
        """Build the pool manager with this adapter's guarded pools."""
        super().init_poolmanager(*args, **kwargs)
        self.poolmanager.pool_classes_by_scheme = dict(self._pools)


class _PeerCheck:
    """Mixin that checks the connected socket's peer before anything is sent on it."""

    destination_policy: DestinationPolicy
    host: str

    port: int

    def _new_conn(self) -> Any:
        """Connect only to an allowed host, then refuse a peer address the policy refuses.

        Raises:
            BlockedDestinationError: The host or the address reached is not allowed.
        """
        self.destination_policy.check_host(self.host, self.port)
        sock = super()._new_conn()  # type: ignore[misc]
        try:
            self.destination_policy.check(self.host, sock.getpeername()[0])
        except BlockedDestinationError:
            sock.close()
            raise
        return sock


@lru_cache(maxsize=32)
def _guarded_pools(policy: DestinationPolicy) -> Mapping[str, type[HTTPConnectionPool]]:
    """Return HTTP and HTTPS pool classes whose connections obey ``policy``."""
    plain = type(
        "GuardedHTTPConnection", (_PeerCheck, HTTPConnection), {"destination_policy": policy}
    )
    secure = type(
        "GuardedHTTPSConnection", (_PeerCheck, HTTPSConnection), {"destination_policy": policy}
    )
    return {
        "http": type("GuardedHTTPPool", (HTTPConnectionPool,), {"ConnectionCls": plain}),
        "https": type("GuardedHTTPSPool", (HTTPSConnectionPool,), {"ConnectionCls": secure}),
    }
