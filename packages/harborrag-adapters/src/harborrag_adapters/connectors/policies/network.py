"""Keep connector HTTP traffic off private networks unless the operator allows a host.

Public addresses are always allowed; private, loopback, link-local and reserved ones only
when named in ``HARBORRAG_CONNECTOR_ALLOWED_HOSTS``. ``guarded_session`` checks every address
a host resolves to before connecting, then the address the socket actually reached, so
neither a closed port nor a DNS answer that changes in between slips through. Its redirect
targets keep bytes that are not UTF-8 as percent escapes, so following one never fails.

Proxy variables such as ``HTTPS_PROXY`` are ignored unless the operator sets
``HARBORRAG_CONNECTOR_USE_PROXY``. Through a proxy, only the target host's own
addresses are checked; the proxy then decides where the request goes.

Only ``DestinationPolicy.from_environ`` reads the environment, and the runtime calls
it. A client built without a policy gets the default one, public addresses only. A policy's
response observer sees each request's connector, method, host and status code, never its
path or query.
"""

from __future__ import annotations

import os
import socket
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from functools import lru_cache
from ipaddress import IPv4Address, IPv4Network, IPv6Address, IPv6Network, ip_address, ip_network
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
_NAT64_PREFIX = IPv6Network("64:ff9b::/96")
_IPV4_COMPATIBLE_PREFIX = IPv6Network("::/96")
_IPV4_TRANSLATED_PREFIX = IPv6Network("::ffff:0:0:0/96")
_UNSPECIFIED_AND_LOOPBACK = IPv6Network("::/127")
_NON_PUBLIC_IPV6 = (
    IPv6Network("fec0::/10"),
    IPv6Network("3fff::/20"),
    IPv6Network("5f00::/16"),
)
_IPV4_MASK = 0xFFFFFFFF


@dataclass(frozen=True, slots=True)
class ResponseObservation:
    """What a response observer learns about one connector request.

    Attributes:
        connector: The connector type that sent the request, such as ``jira``.
        method: The HTTP method.
        host: The host the request went to; never the path or the query.
        status_code: The HTTP status, or None when no response arrived.
    """

    connector: str
    method: str
    host: str
    status_code: int | None


type ResponseObserver = Callable[[ResponseObservation], None]
"""Called after each connector request with its ``ResponseObservation``."""


class BlockedDestinationError(FetchError):
    """A connector tried to reach a private address no allowed host covers; never retried."""

    retryable = False


@dataclass(frozen=True, slots=True)
class DestinationPolicy:
    """Host names and networks a connector may reach although they are not public.

    The default allows public addresses only. The runtime builds the operator's
    policy once with ``from_environ`` and passes it to every connector.

    Args:
        hosts: Lower-case host names that are reachable although they are not public.
        networks: Address ranges that are reachable although they are not public.
        use_proxy: Whether requests may go through the proxies named in the process
            environment.
        on_response: Sees every request's connector, method, host and HTTP status.

    Attributes:
        hosts: Lower-case host names that are reachable although they are not public.
        networks: Address ranges that are reachable although they are not public.
        use_proxy: Whether requests may go through the proxies named in the process
            environment.
        on_response: Sees every request's connector, method, host and HTTP status.
    """

    hosts: frozenset[str] = frozenset()
    networks: tuple[IPv4Network | IPv6Network, ...] = ()
    use_proxy: bool = False
    on_response: ResponseObserver | None = field(default=None, compare=False)

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

        Args:
            host: Host name the request targets.
            port: Port the request targets, used for name resolution.

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

        An IPv6 address that carries an IPv4 address (IPv4-mapped, IPv4-translated,
        NAT64 or IPv4-compatible) is judged by the IPv4 address it carries. Site-local
        ``fec0::/10``, documentation ``3fff::/20`` and SRv6 ``5f00::/16`` addresses
        are refused, although some Python versions count them as global.

        Args:
            host: Host name the address was resolved from.
            address: IP address to check, with an optional IPv6 zone suffix.

        Raises:
            BlockedDestinationError: The address is private and not allowed.
        """
        reached = ip_address(address.split("%", 1)[0])
        peer = _embedded_ipv4(reached) or reached
        if peer.is_global and not peer.is_multicast and not _is_listed_non_public(peer):
            return
        if host.casefold().rstrip(".") in self.hosts:
            return
        if any(peer in network or reached in network for network in self.networks):
            return
        raise BlockedDestinationError(
            f"{host} resolves to the private address {peer}; list the host or its network "
            f"in {ALLOWED_HOSTS_VARIABLE} to allow it"
        )


def _is_listed_non_public(address: IPv4Address | IPv6Address) -> bool:
    """Return whether an IPv6 address is in a range ``is_global`` may wrongly accept."""
    return isinstance(address, IPv6Address) and any(
        address in network for network in _NON_PUBLIC_IPV6
    )


def _embedded_ipv4(address: IPv4Address | IPv6Address) -> IPv4Address | None:
    """Return the IPv4 address an IPv6 address carries as IPv4-mapped, IPv4-translated, NAT64 or IPv4-compatible."""
    if not isinstance(address, IPv6Address):
        return None
    if address.ipv4_mapped is not None:
        return address.ipv4_mapped
    if (
        address in _NAT64_PREFIX
        or address in _IPV4_TRANSLATED_PREFIX
        or (address in _IPV4_COMPATIBLE_PREFIX and address not in _UNSPECIFIED_AND_LOOPBACK)
    ):
        return IPv4Address(int(address) & _IPV4_MASK)
    return None


def guarded_session(policy: DestinationPolicy, *, connector: str) -> requests.Session:
    """Return a session whose direct connections obey ``policy``.

    Args:
        policy: The destinations the session may reach and its response observer.
        connector: The connector type reported to the policy's response observer.

    Returns:
        A session with the guarded adapter mounted for HTTP and HTTPS.
    """
    session = _GuardedSession()
    adapter = _GuardedAdapter(policy, connector=connector)
    session.mount("http://", adapter)
    session.mount("https://", adapter)
    return session


def redirect_location(header: str) -> str:
    """Return a redirect ``Location`` header as the URL the server meant.

    http.client hands header bytes over as Latin-1 characters. Bytes that form UTF-8
    are decoded as UTF-8, as requests does. Other bytes are kept as percent escapes,
    so the redirect reaches exactly the bytes the server sent instead of failing to
    decode. A header that already holds characters beyond Latin-1 is returned as is.
    """
    try:
        raw = header.encode("latin-1")
    except UnicodeEncodeError:
        return header
    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError:
        return "".join(chr(byte) if byte < 0x80 else f"%{byte:02X}" for byte in raw)


class _GuardedSession(requests.Session):
    """Session whose redirect targets keep bytes that are not UTF-8 as percent escapes."""

    def get_redirect_target(self, resp: requests.Response) -> str | None:
        """Return the redirect target of a response, or None when it is not a redirect.

        Requests calls this while sending, even when it does not follow redirects, and
        its own version raises for a ``Location`` whose bytes are not UTF-8.
        """
        if not resp.is_redirect:
            return None
        return redirect_location(resp.headers["location"])


class _GuardedAdapter(HTTPAdapter):
    """Adapter whose connection pools check each new socket's peer address.

    Args:
        policy: The destinations this adapter may reach and its response observer.
        connector: The connector type reported with each response.
    """

    def __init__(self, policy: DestinationPolicy, *, connector: str) -> None:
        self._policy = policy
        self._connector = connector
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

        Args:
            request: Prepared request to send.
            stream: Whether to stream the response body, as in requests.
            timeout: Connect and read timeout, as in requests.
            verify: TLS verification setting, as in requests.
            cert: Client certificate, as in requests.
            proxies: Proxies for the request, ignored unless the operator allows them.

        Returns:
            The response from the target host.

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
        try:
            response = super().send(
                request, stream=stream, timeout=timeout, verify=verify, cert=cert, proxies=proxies
            )
        except requests.RequestException:
            self._observe(request, None)
            raise
        self._observe(request, response.status_code)
        return response

    def _observe(self, request: requests.PreparedRequest, status_code: int | None) -> None:
        """Report one request's outcome to the policy's response observer, if any."""
        if self._policy.on_response is not None:
            self._policy.on_response(
                ResponseObservation(
                    connector=self._connector,
                    method=request.method or "GET",
                    host=urlparse(str(request.url)).hostname or "",
                    status_code=status_code,
                )
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

        Returns:
            The connected socket.

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
