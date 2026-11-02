"""Keep connector HTTP traffic off private networks unless the operator allows a host.

Handles:
- ``DestinationPolicy``: which peer addresses a connector may reach; public addresses
  always, private, loopback, link-local and reserved ones only when allowed by name
  or network in ``HARBORRAG_CONNECTOR_ALLOWED_HOSTS``
- ``guarded_session``: a ``requests`` session that checks every address a host
  resolves to before connecting, then the address the socket actually reached,
  so neither a closed port nor a DNS answer that changes in between slips through

Does NOT guard traffic sent through a proxy; the proxy decides where it goes.
"""

from __future__ import annotations

import os
import socket
from collections.abc import Mapping
from dataclasses import dataclass
from functools import lru_cache
from ipaddress import IPv4Network, IPv6Address, IPv6Network, ip_address, ip_network
from typing import Any, Self

import requests
from requests.adapters import HTTPAdapter
from urllib3.connection import HTTPConnection, HTTPSConnection
from urllib3.connectionpool import HTTPConnectionPool, HTTPSConnectionPool

from ..exceptions import FetchError

ALLOWED_HOSTS_VARIABLE = "HARBORRAG_CONNECTOR_ALLOWED_HOSTS"


class BlockedDestinationError(FetchError):
    """A connector tried to reach a private address no allowed host covers; never retried."""

    retryable = False


@dataclass(frozen=True, slots=True)
class DestinationPolicy:
    """Host names and networks a connector may reach although they are not public."""

    hosts: frozenset[str] = frozenset()
    networks: tuple[IPv4Network | IPv6Network, ...] = ()

    @classmethod
    def from_environ(cls, environ: Mapping[str, str] | None = None) -> Self:
        """Read ``HARBORRAG_CONNECTOR_ALLOWED_HOSTS``: comma-separated host names or CIDRs."""
        raw = (os.environ if environ is None else environ).get(ALLOWED_HOSTS_VARIABLE, "")
        hosts: set[str] = set()
        networks: list[IPv4Network | IPv6Network] = []
        for item in (part.strip() for part in raw.split(",")):
            if not item:
                continue
            try:
                networks.append(ip_network(item, strict=False))
            except ValueError:
                hosts.add(item.casefold().rstrip("."))
        return cls(frozenset(hosts), tuple(networks))

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
        self._pools = _guarded_pools(policy)
        super().__init__()

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
