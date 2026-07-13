from __future__ import annotations

from dataclasses import dataclass, field
from urllib.parse import urlparse

from harborrag_core.contracts.errors import HarborConnectionError


@dataclass(slots=True)
class UrlPolicy:
    allowed_schemes: set[str] = field(default_factory=lambda: {"http", "https"})
    denied_hosts: set[str] = field(default_factory=set)

    def validate(self, url: str) -> None:
        parsed = urlparse(url)
        if parsed.scheme not in self.allowed_schemes:
            raise HarborConnectionError(f"URL scheme is not allowed: {parsed.scheme}")
        if parsed.hostname in self.denied_hosts:
            raise HarborConnectionError(f"URL host is denied: {parsed.hostname}")
