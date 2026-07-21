from __future__ import annotations

from dataclasses import dataclass, field
from urllib.parse import urlparse

from harborrag_core.errors import URLPolicyError


@dataclass(slots=True)
class URLPolicy:
    allowed_schemes: set[str] = field(default_factory=lambda: {"http", "https"})
    denied_hosts: set[str] = field(default_factory=set)

    def validate(self, url: str) -> None:
        parsed = urlparse(url)
        if parsed.scheme not in self.allowed_schemes:
            raise URLPolicyError(f"URL scheme is not allowed: {parsed.scheme}")
        if parsed.hostname in self.denied_hosts:
            raise URLPolicyError(f"URL host is denied: {parsed.hostname}")
