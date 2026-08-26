"""Reusable policy for service endpoints that may carry credentials."""

from __future__ import annotations

from dataclasses import dataclass
from ipaddress import ip_address
from urllib.parse import SplitResult, urlsplit

_LOOPBACK_NAMES = frozenset({"localhost"})


def is_loopback_host(host: str) -> bool:
    """Return whether *host* is an explicit loopback name or address.

    Hostnames are deliberately not resolved here. Treating an arbitrary DNS name as
    local would make the transport decision vulnerable to DNS changes and rebinding.
    """

    normalized = host.rstrip(".").casefold()
    if normalized in _LOOPBACK_NAMES:
        return True
    try:
        return ip_address(normalized).is_loopback
    except ValueError:
        return False


@dataclass(frozen=True, slots=True)
class RemoteTransportPolicy:
    """Validate absolute service URLs and require encryption off loopback."""

    service: str
    allowed_schemes: frozenset[str]
    secure_schemes: frozenset[str]

    def __post_init__(self) -> None:
        if not self.service.strip():
            raise ValueError("remote transport service name must be non-empty")
        if not self.allowed_schemes:
            raise ValueError("remote transport policy requires an allowed scheme")
        if not self.secure_schemes <= self.allowed_schemes:
            raise ValueError("secure schemes must be a subset of allowed schemes")

    def validate(
        self,
        url: str,
        *,
        allow_insecure_remote: bool = False,
    ) -> SplitResult:
        """Return the parsed URL after enforcing the configured transport policy."""

        if not url or url != url.strip():
            raise ValueError(f"{self.service} URL must be non-empty without outer whitespace")
        try:
            parsed = urlsplit(url)
            # Accessing port performs urllib's range and syntax validation.
            _ = parsed.port
        except ValueError as exc:
            raise ValueError(f"{self.service} URL is malformed") from exc
        scheme = parsed.scheme.casefold()
        if scheme not in self.allowed_schemes or parsed.hostname is None:
            choices = ", ".join(f"{item}://" for item in sorted(self.allowed_schemes))
            raise ValueError(f"{self.service} URL must be absolute and use one of: {choices}")
        if (
            scheme not in self.secure_schemes
            and not is_loopback_host(parsed.hostname)
            and not allow_insecure_remote
        ):
            secure_choices = ", ".join(f"{item}://" for item in sorted(self.secure_schemes))
            raise ValueError(
                f"remote {self.service} requires an encrypted transport ({secure_choices})"
            )
        return parsed


__all__ = ["RemoteTransportPolicy", "is_loopback_host"]
