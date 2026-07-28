from __future__ import annotations

import ipaddress
from dataclasses import dataclass, field
from urllib.parse import urlparse

from harborrag_core.contracts.errors import HarborError

_LOCAL_HOSTNAMES = frozenset({"localhost", "localhost.localdomain", "ip6-localhost"})


class URLPolicyError(HarborError):
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

    def validate(self, url: str) -> None:
        parsed = urlparse(url)
        if parsed.scheme not in self.allowed_schemes:
            raise URLPolicyError(f"URL scheme is not allowed: {parsed.scheme}")
        hostname = parsed.hostname
        if hostname in self.denied_hosts:
            raise URLPolicyError(f"URL host is denied: {hostname}")
        if hostname is None:
            return
        # Reject well-known local-machine hostnames outright: they always
        # resolve to loopback, so this stays a literal-string check (no DNS
        # resolution) while still closing the gap the literal-IP check below
        # can't -- "https://localhost/..." never parses as an IP address.
        # Strip a trailing root-zone dot ("localhost.") first: DNS and HTTP
        # clients treat it identically to the bare name, so it must not slip
        # past this check on that technicality alone.
        normalized_hostname = hostname.lower().rstrip(".")
        if normalized_hostname in _LOCAL_HOSTNAMES:
            raise URLPolicyError(f"URL host is not allowed: {hostname}")
        # Baseline SSRF guard: block literal IPs that target private/
        # loopback/link-local/metadata/reserved ranges by default, on top of
        # the caller-supplied denylist above. This only classifies literal IP
        # hosts (e.g. "https://169.254.169.254/..."); plain hostnames are not
        # DNS-resolved here (the codebase has no existing DNS-resolution
        # convention to reuse -- connectors' same_origin()/
        # require_same_origin_url() in harborrag_adapters compare origins
        # textually too), so a hostname that later resolves to an internal
        # address (including via DNS rebinding) is not caught by this check;
        # closing that gap requires a transport-boundary peer-IP check at the
        # HTTP-client layer in harborrag_adapters, where sockets are actually
        # opened -- URLPolicy itself is not yet wired into any real HTTP call
        # site in this codebase (connectors use same_origin() instead), so
        # that enforcement point does not exist here to extend.
        try:
            ip = ipaddress.ip_address(hostname)
        except ValueError:
            return
        if _is_disallowed_ip(ip):
            raise URLPolicyError(f"URL host is not allowed: {hostname}")
