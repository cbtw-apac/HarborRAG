from __future__ import annotations

import ipaddress
import socket
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from urllib.parse import urlparse

from harborrag_core.contracts.errors import HarborSecurityError

_LOCAL_HOSTNAMES = frozenset({"localhost", "localhost.localdomain", "ip6-localhost"})

type _IPAddress = ipaddress.IPv4Address | ipaddress.IPv6Address

# RFC 6052 well-known NAT64 prefix: the last 32 bits are an IPv4 address.
_NAT64_PREFIX = ipaddress.ip_network("64:ff9b::/96")


class URLPolicyError(HarborSecurityError):
    """Raised when a URL violates the configured outbound-access policy."""


def _embedded_ipv4(ip: _IPAddress) -> ipaddress.IPv4Address | None:
    """The IPv4 address an IPv4-mapped or NAT64 address routes to, if any."""

    if isinstance(ip, ipaddress.IPv6Address):
        if ip.ipv4_mapped is not None:
            return ip.ipv4_mapped
        if ip in _NAT64_PREFIX:
            return ipaddress.IPv4Address(int(ip) & 0xFFFF_FFFF)
    return None


def _is_non_public(ip: _IPAddress) -> bool:
    return (
        not ip.is_global
        or ip.is_private
        or ip.is_loopback
        or ip.is_link_local
        or ip.is_reserved
        or ip.is_multicast
        or ip.is_unspecified
    )


def _is_disallowed_ip(ip: _IPAddress) -> bool:
    """Classify an IP as unsafe for server-initiated outbound requests.

    Anything not globally routable is refused: loopback, RFC1918/ULA, link-local
    (including the 169.254.169.254 metadata address), CGNAT 100.64.0.0/10, the IETF
    and benchmarking blocks, documentation ranges and multicast (which ``is_global``
    counts as global). The narrower predicates are kept explicit so the intent does
    not silently regress if a Python release changes ``is_global``.

    An IPv4-mapped or NAT64 address is classified by the IPv4 address it embeds,
    because that is where it routes. The outer ``::/8`` block is flagged reserved,
    which would otherwise refuse every public NAT64 destination.
    """

    embedded = _embedded_ipv4(ip)
    return _is_non_public(ip if embedded is None else embedded)


@dataclass(slots=True)
class URLPolicy:
    allowed_schemes: set[str] = field(default_factory=lambda: {"http", "https"})
    denied_hosts: set[str] = field(default_factory=set)
    resolver: Callable[[str, int], Iterable[str]] | None = None

    def validate(self, url: str) -> None:
        # urlparse and .port both raise a bare ValueError on malformed input
        # ("http://h:99999/", "http://[::1/"). Letting that escape breaks the
        # contract callers compile against: one mapping URLPolicyError to a 400
        # gets an unhandled 500, and one using `except URLPolicyError` as its
        # deny path treats the URL as acceptable.
        try:
            parsed = urlparse(url)
            port = parsed.port
        except ValueError as exc:
            raise URLPolicyError(f"URL is malformed: {exc}") from exc
        if parsed.scheme not in self.allowed_schemes:
            raise URLPolicyError(f"URL scheme is not allowed: {parsed.scheme}")
        hostname = parsed.hostname
        if hostname is None:
            raise URLPolicyError("URL must include a host")
        # Reject well-known local-machine hostnames outright: they always
        # resolve to loopback, so this stays a literal-string check (no DNS
        # resolution) while still closing the gap the literal-IP check below
        # can't -- "https://localhost/..." never parses as an IP address.
        # Strip a trailing root-zone dot ("localhost.") first: DNS and HTTP
        # clients treat it identically to the bare name, so it must not slip
        # past this check on that technicality alone.
        normalized_hostname = hostname.lower().rstrip(".")
        normalized_denied_hosts = {host.lower().rstrip(".") for host in self.denied_hosts}
        if normalized_hostname in normalized_denied_hosts:
            raise URLPolicyError(f"URL host is denied: {hostname}")
        if normalized_hostname in _LOCAL_HOSTNAMES:
            raise URLPolicyError(f"URL host is not allowed: {hostname}")
        # Resolve every candidate here, including legacy numeric host syntax
        # understood by the system resolver. This is a preflight policy only:
        # the HTTP transport must additionally compare the connected peer IP
        # with this policy to close the DNS-rebinding/TOCTOU window.
        port = port or (443 if parsed.scheme == "https" else 80)
        addresses = tuple(self._resolve(normalized_hostname, port))
        if not addresses:
            raise URLPolicyError(f"URL host could not be resolved: {hostname}")
        for address in addresses:
            try:
                ip = ipaddress.ip_address(address)
            except ValueError as exc:
                raise URLPolicyError(
                    f"URL resolver returned an invalid address: {address}"
                ) from exc
            if _is_disallowed_ip(ip):
                raise URLPolicyError(f"URL host is not allowed: {hostname}")

    def _resolve(self, hostname: str, port: int) -> Iterable[str]:
        """Resolve every candidate address; transports must still verify the peer IP."""

        if self.resolver is not None:
            return self.resolver(hostname, port)
        try:
            return {
                str(result[4][0])
                for result in socket.getaddrinfo(
                    hostname,
                    port,
                    family=socket.AF_UNSPEC,
                    type=socket.SOCK_STREAM,
                )
            }
        except socket.gaierror as exc:
            raise URLPolicyError(f"URL host could not be resolved: {hostname}") from exc
