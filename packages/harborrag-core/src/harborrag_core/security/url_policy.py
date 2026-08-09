from __future__ import annotations

import ipaddress
import socket
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from urllib.parse import urlparse

from harborrag_core.contracts.errors import HarborSecurityError

_LOCAL_HOSTNAMES = frozenset({"localhost", "localhost.localdomain", "ip6-localhost"})


class URLPolicyError(HarborSecurityError):
    """Raised when a URL violates the configured outbound-access policy."""


def _is_disallowed_ip(ip: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
    """Classify an IP as unsafe for server-initiated outbound requests.

    Covers loopback (127.0.0.0/8, ::1), RFC1918/ULA private ranges
    (10.0.0.0/8, 172.16.0.0/12, 192.168.0.0/16, fc00::/7), link-local ranges
    including the cloud-metadata address 169.254.169.254 (169.254.0.0/16,
    fe80::/10), multicast, and other IANA-reserved blocks. `is_private`
    already subsumes loopback/link-local for both address families, but the
    other predicates are kept explicit so the intent doesn't silently regress
    if a future Python release narrows `is_private`.
    """
    return (
        ip.is_private
        or ip.is_loopback
        or ip.is_link_local
        or ip.is_reserved
        or ip.is_multicast
        or ip.is_unspecified
    )


@dataclass(slots=True)
class URLPolicy:
    allowed_schemes: set[str] = field(default_factory=lambda: {"http", "https"})
    denied_hosts: set[str] = field(default_factory=set)
    resolver: Callable[[str, int], Iterable[str]] | None = None

    def validate(self, url: str) -> None:
        parsed = urlparse(url)
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
        port = parsed.port or (443 if parsed.scheme == "https" else 80)
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
